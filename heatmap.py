#!/usr/bin/env python3
"""
RF HotScan — RF activity heatmap (direct RTL-SDR, 2026).

A 2026 remake of the classic rtl_power + heatmap.py: sweep a contiguous
frequency range over a time window and build a time x frequency heatmap whose
purpose is to *identify localized transmission activity over time*.

Design (see /Users/dk/.claude/plans/concurrent-scribbling-owl.md):
  * Direct dongle control is harnessed onto rtl_backend.RtlBackend by
    composition (RtlSweepSource); a synthetic FakeSweepSource drives the whole
    pipeline with no hardware so it is testable while the dongle is busy.
  * Every sweep is persisted to SQLite (one quantised power row per sweep) so a
    heatmap can be re-created offline, exactly.
  * Live rendering is pure-Tk (HeatmapView, see the GUI half of this module);
    offline re-render / PNG export uses matplotlib (HeatmapRenderer).
  * A headless CLI (`python3 -m heatmap scan ...`) + run_scan() Python API let an
    agent run a scan and parse JSON results.

Heavy deps (numpy/scipy/rtlsdr/matplotlib) are imported lazily so importing this
module — and the stdlib-only GQRX scanner that hosts the Heatmap tab — stays cheap.
"""

import os
import sys
import json
import math
import time
import queue
import sqlite3
import logging
import threading
import traceback

import clock     # shared time base (UTC epoch + ISO), see clock.py

# ----- lazy heavy imports (numpy/scipy) -----------------------------------
np = None
_signal = None


def _lazy_np():
    global np, _signal
    if np is None:
        import numpy as _np
        from scipy import signal as _sig
        np, _signal = _np, _sig
    return np


# ----- paths / logging ----------------------------------------------------
# App keeps its data next to itself, not in ~/.config/gqrx (direct SDR is primary).
APPDIR = os.path.dirname(os.path.abspath(__file__))
DBPATH = os.path.join(APPDIR, "heatmap.sqlite")
IQDIR = os.path.join(APPDIR, "iq")
LOGFILE = os.path.join(APPDIR, "heatmap.log")
EVENTS = os.path.join(APPDIR, "heatmap.events.jsonl")

os.makedirs(APPDIR, exist_ok=True)

logger = logging.getLogger("heatmap")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _fh = logging.FileHandler(LOGFILE, mode="a")
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s", "%H:%M:%S"))
    logger.addHandler(_fh)


def emit_event(kind, **fields):
    """Append one machine-readable JSONL event (for agents) + log it."""
    t = clock.now_unix()
    rec = {"t": round(t, 3), "iso": clock.utc_iso(t), "event": kind}
    rec.update(fields)
    try:
        with open(EVENTS, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    logger.debug("event %s", rec)
    return rec


# ----- DSP / calibration constants ----------------------------------------
SAMPLE_RATE = 2_400_000
USABLE_FRAC = None           # derived from crop instead
DC_GUARD = 30_000            # null bins within this of a window centre (DC spike)
DBFS_CAL = 0.0               # absolute offset; detection is relative so this only
                             # shifts the colourbar labels.
APP_VERSION = "heatmap-1.0"


# ==========================================================================
# Sweep configuration
# ==========================================================================
def next_pow2(n):
    return 1 << (max(1, int(n)) - 1).bit_length()


def _opt_float(v):
    """Parse a float, or None for 'auto'/blank (auto-range sentinel)."""
    if v is None:
        return None
    if isinstance(v, str) and v.strip().lower() in ("", "auto"):
        return None
    return float(v)


def auto_range(finite, lo_pct=55.0, hi_pct=99.7):
    """Robust colour range: dark floor (>= median) up to near-peak. Narrow
    transmissions are sparse, so the low end sits at the median to push the
    noise floor dark and let signals saturate bright."""
    if finite is None or finite.size == 0:
        return -100.0, -20.0
    lo = float(np.percentile(finite, lo_pct))
    hi = float(np.percentile(finite, hi_pct))
    if hi - lo < 6.0:
        hi = lo + 6.0
    return lo, hi


class SweepConfig:
    """All parameters of one capture session (immutable for its duration)."""

    def __init__(self, f_start, f_stop, samp_rate=SAMPLE_RATE, bin_hz=3000.0,
                 gain="auto", ppm=0, dwell_s=0.05, n_avg=8, crop=0.20,
                 overlap_hz=0, device="0", duration_s=30.0, max_sweeps=0,
                 dmin=-100.0, dmax=-20.0, colormap="inferno", margin_db=8.0,
                 iq_mode="off", label=""):
        _lazy_np()
        self.f_start = int(f_start)
        self.f_stop = int(f_stop)
        if self.f_stop <= self.f_start:
            raise ValueError("f_stop must be > f_start")
        self.samp_rate = int(samp_rate)
        self.gain = gain
        self.ppm = int(ppm)
        self.crop = float(crop)
        self.overlap_hz = int(overlap_hz)
        self.device = device
        self.duration_s = float(duration_s)
        self.max_sweeps = int(max_sweeps)
        self.dmin = _opt_float(dmin)     # None => auto-range
        self.dmax = _opt_float(dmax)
        self.colormap = colormap
        self.margin_db = float(margin_db)
        self.iq_mode = iq_mode            # "off" | "manual" | "activity"
        self.label = label
        self.dc_guard = DC_GUARD

        # derived FFT geometry
        self.fft_size = max(256, next_pow2(self.samp_rate / max(1.0, bin_hz)))
        self.bin_hz = self.samp_rate / self.fft_size
        self.n_avg = max(1, int(n_avg))
        self.dwell_s = float(dwell_s)
        # samples captured per window = n_avg full FFT frames
        self.win_nsamp = self.n_avg * self.fft_size

        # usable bandwidth per window after edge crop
        self.usable = self.samp_rate * (1.0 - 2.0 * self.crop)
        self.step = max(self.bin_hz, self.usable - self.overlap_hz)

        # global frequency grid (heatmap columns)
        self.n_bins = max(1, int(round((self.f_stop - self.f_start) / self.bin_hz)))
        self.f_bin0 = self.f_start + 0.5 * self.bin_hz   # centre of column 0
        self.freq_grid = self.f_start + (np.arange(self.n_bins) + 0.5) * self.bin_hz

        # window centre plan
        self.hops = plan_range_windows(self.f_start, self.f_stop,
                                       self.usable, self.step)

    def as_dict(self):
        return {
            "f_start": self.f_start, "f_stop": self.f_stop,
            "samp_rate": self.samp_rate, "fft_size": self.fft_size,
            "bin_hz": self.bin_hz, "n_bins": self.n_bins, "f_bin0": self.f_bin0,
            "crop": self.crop, "overlap_hz": self.overlap_hz,
            "gain": str(self.gain), "ppm": self.ppm, "dwell_s": self.dwell_s,
            "n_avg": self.n_avg, "device": str(self.device),
            "duration_s": self.duration_s, "max_sweeps": self.max_sweeps,
            "margin_db": self.margin_db, "iq_mode": self.iq_mode,
            "colormap": self.colormap, "dmin": self.dmin, "dmax": self.dmax,
            "label": self.label, "n_hops": len(self.hops),
        }


def plan_range_windows(f_start, f_stop, usable, step):
    """Tile [f_start, f_stop] into window centre frequencies.

    The first window's lower usable edge sits at f_start; centres advance by
    `step` until the whole range is covered. Pure / unit-testable."""
    half = usable / 2.0
    centres = []
    c = f_start + half
    # guard against pathological tiny step
    step = max(step, 1.0)
    while (c - half) < f_stop:
        centres.append(int(round(c)))
        c += step
        if len(centres) > 100000:           # sanity backstop
            break
    if not centres:
        centres = [int(round((f_start + f_stop) / 2.0))]
    return centres


# ==========================================================================
# Per-window power + stitching
# ==========================================================================
def window_power_dbfs(iq, fs, fft_size):
    """Welch periodogram of one IQ block -> (offset_hz[], dbfs[]) two-sided,
    ascending in frequency offset from the capture centre.

    SCALE NOTE (AGENTS.md "dBFS scale convention"): this is a *per-FFT-bin* PSD
    (`scaling="spectrum"`), a DIFFERENT scale from rtl_backend.channel_power_dbfs
    (mean power in a `bw`-wide channel) that the scanner's squelch/noise-floor use.
    A scanner squelch threshold does NOT transfer to these values and vice-versa.
    The heatmap only ever compares these to *itself* (per-bin floor + margin, and
    the auto colour range), so the absolute offset is irrelevant here; do not feed
    a scanner-calibrated threshold into the heatmap or compare the two numerically."""
    _lazy_np()
    nper = min(fft_size, len(iq))
    f, pxx = _signal.welch(iq, fs=fs, nperseg=nper, return_onesided=False,
                           detrend=False, scaling="spectrum")
    f = np.fft.fftshift(f)
    pxx = np.fft.fftshift(pxx)
    dbfs = 10.0 * np.log10(pxx + 1e-12) + DBFS_CAL
    return f, dbfs


def _interp_nans(a):
    nans = np.isnan(a)
    if nans.all():
        a[:] = -160.0
        return a
    if nans.any():
        x = np.arange(len(a))
        a[nans] = np.interp(x[nans], x[~nans], a[~nans])
    return a


# ==========================================================================
# Sweep sources
# ==========================================================================
class SweepSource:
    """Abstract: produces one stitched dBFS row across [f_start, f_stop]."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._last_iq = None

    def open(self):
        pass

    def close(self):
        pass

    @property
    def connected(self):
        return True

    def _capture_window(self, center_hz):
        raise NotImplementedError

    def last_iq(self):
        return self._last_iq

    def sweep_once(self):
        cfg = self.cfg
        n = cfg.n_bins
        acc_lin = np.zeros(n, dtype=np.float64)
        cnt = np.zeros(n, dtype=np.int32)
        half = cfg.usable / 2.0
        for center in cfg.hops:
            iq = self._capture_window(center)
            self._last_iq = iq
            offs, dbfs = window_power_dbfs(iq, cfg.samp_rate, cfg.fft_size)
            keep = (np.abs(offs) <= half) & (np.abs(offs) >= cfg.dc_guard)
            absf = center + offs[keep]
            vals = dbfs[keep]
            idx = np.floor((absf - cfg.f_start) / cfg.bin_hz).astype(int)
            valid = (idx >= 0) & (idx < n)
            idx = idx[valid]
            lin = np.power(10.0, vals[valid] / 10.0)
            np.add.at(acc_lin, idx, lin)
            np.add.at(cnt, idx, 1)
        row = np.full(n, np.nan, dtype=np.float64)
        hit = cnt > 0
        row[hit] = 10.0 * np.log10(acc_lin[hit] / cnt[hit] + 1e-30)
        row = _interp_nans(row)
        return row.astype(np.float32)


class FakeSweepSource(SweepSource):
    """TESTING ONLY — a synthetic spectrum, never an operational source.

    Noise floor + scheduled keyed transmitters + a DC spike + edge roll-off,
    deterministic (seeded). It exists so the test suite (`test_heatmap.py`) and
    the explicit `--device fake` CLI dry-run can exercise the full pipeline with
    no dongle. It is NOT offered in the GUI Device dropdown; selecting it produces
    fabricated signals, not real RF."""

    def __init__(self, cfg, seed=1234):
        super().__init__(cfg)
        self.rng = np.random.default_rng(seed)
        self.sweep_idx = 0
        span = cfg.f_stop - cfg.f_start
        # transmitters: (freq, base_amp, keying(sweep)->bool)
        self.txs = [
            (cfg.f_start + 0.30 * span, 0.25, lambda s: True),               # always on
            (cfg.f_start + 0.55 * span, 0.18, lambda s: (s // 5) % 2 == 0),  # bursty
            (cfg.f_start + 0.80 * span, 0.30, lambda s: (s % 7) < 2),        # intermittent
        ]

    def _capture_window(self, center_hz):
        cfg = self.cfg
        n = cfg.win_nsamp
        fs = cfg.samp_rate
        t = np.arange(n)
        iq = (self.rng.standard_normal(n) + 1j * self.rng.standard_normal(n))
        iq *= 0.01                                   # noise floor
        iq += 0.02 + 0.0j                            # DC offset -> centre spike
        half = cfg.usable / 2.0
        for f0, amp, key in self.txs:
            if not key(self.sweep_idx):
                continue
            off = f0 - center_hz
            if abs(off) > half:
                continue                              # not in this window
            # edge roll-off: attenuate toward the band edge
            roll = max(0.2, 1.0 - (abs(off) / (fs / 2.0)))
            phase = 2j * np.pi * (off / fs) * t
            iq += amp * roll * np.exp(phase)
        return iq.astype(np.complex64)

    def sweep_once(self):
        row = super().sweep_once()
        self.sweep_idx += 1
        return row


class RtlSweepSource(SweepSource):
    """Wraps rtl_backend.RtlBackend (composition) for real range sweeps.

    If `backend` is supplied (borrow mode), the heatmap reuses that
    already-connected RtlBackend (owned by the Scanner) instead of opening a
    second one — the caller is responsible for having paused the Scanner's SDR
    use first. Otherwise this source opens and owns its own dongle."""

    def __init__(self, cfg, backend=None):
        super().__init__(cfg)
        self.backend = backend
        self._borrowed = backend is not None

    def open(self):
        if self._borrowed:
            if not (self.backend is not None and self.backend.connected):
                raise RuntimeError("borrowed RTL backend is not connected")
            self.backend.sweep_nsamp = self.cfg.win_nsamp
            emit_event("device_borrow", samp_rate=self.cfg.samp_rate)
            logger.info("heatmap borrowing the Scanner's RTL backend")
            return
        # Release the dongle from GQRX first (single-owner USB device).
        try:
            import rf_hotscan
            if rf_hotscan.gqrx_is_running():
                logger.info("GQRX is running; quitting it to free the dongle")
                emit_event("gqrx_quit")
                rf_hotscan.gqrx_quit()
        except Exception:
            logger.warning("gqrx handoff skipped: %s", traceback.format_exc())
        from rtl_backend import RtlBackend
        gain = 40.0
        try:
            gain = float(self.cfg.gain)
        except (TypeError, ValueError):
            gain = 40.0                               # "auto" handled below
        dev_idx = 0
        try:
            dev_idx = int(self.cfg.device)
        except (TypeError, ValueError):
            dev_idx = 0
        self.backend = RtlBackend(sample_rate=self.cfg.samp_rate, gain=gain,
                                  ppm=self.cfg.ppm,
                                  sweep_nsamp=self.cfg.win_nsamp)
        self.backend.owner_label = "heatmap"      # single-owner coordination label
        self.backend.sweep_nsamp = self.cfg.win_nsamp
        # connect() enforces the single-owner dongle rule (AGENTS.md): if the
        # Scanner tab already holds the RTL backend, this raises DongleBusy with
        # a clear message instead of corrupting the device.
        self.backend.connect()
        if str(self.cfg.gain).lower() == "auto":
            try:
                self.backend.sdr.set_manual_gain_enabled(False)
            except Exception:
                logger.warning("AGC enable failed; using fixed gain")
        emit_event("device_open", device=dev_idx, samp_rate=self.cfg.samp_rate)
        _ = dev_idx

    def close(self):
        if self._borrowed:
            # Don't close a borrowed backend — it belongs to the Scanner.
            self.backend = None
            emit_event("device_return")
            return
        if self.backend is not None:
            try:
                self.backend.close()
            finally:
                self.backend = None
                emit_event("device_close")

    @property
    def connected(self):
        return self.backend is not None and self.backend.connected

    def _capture_window(self, center_hz):
        return self.backend.capture_iq(center_hz, nsamp=self.cfg.win_nsamp)


def make_source(cfg, backend=None):
    if str(cfg.device).lower() in ("fake", "sim", "synthetic"):
        return FakeSweepSource(cfg)
    return RtlSweepSource(cfg, backend=backend)


# ==========================================================================
# Quantisation (uint8 BLOB, per-sweep ref/scale) + activity accumulator
# ==========================================================================
def quantize_row(row):
    """Adaptive per-sweep quantisation to uint8. Returns (codes, ref, scale).
    Reconstruct with: dbfs = ref + scale * code."""
    lo = float(np.nanmin(row))
    hi = float(np.nanmax(row))
    if not math.isfinite(lo):
        lo = -160.0
    if not math.isfinite(hi) or hi <= lo:
        hi = lo + 1.0
    ref = lo
    scale = max((hi - lo) / 255.0, 1e-3)
    filled = np.nan_to_num(row, nan=lo)
    codes = np.clip(np.round((filled - ref) / scale), 0, 255).astype(np.uint8)
    return codes, ref, scale


def dequantize(codes, ref, scale):
    return (ref + scale * codes.astype(np.float32)).astype(np.float32)


class Accumulator:
    """Per-bin noise-floor (min-hold-with-leak), activity hit counts, peaks."""

    def __init__(self, n_bins, margin_db, leak_db=0.02):
        self.n_bins = n_bins
        self.margin = float(margin_db)
        self.leak = float(leak_db)
        self.floor = None
        self.hit = np.zeros(n_bins, dtype=np.int64)
        self.peak = np.full(n_bins, -300.0, dtype=np.float64)
        self.n = 0

    def add(self, row):
        if self.floor is None:
            self.floor = row.astype(np.float64).copy()
        else:
            self.floor += self.leak
            np.minimum(self.floor, row, out=self.floor)
        mask = row > (self.floor + self.margin)
        self.hit += mask
        np.maximum(self.peak, row, out=self.peak)
        self.n += 1
        return mask

    def summary(self):
        n = max(1, self.n)
        duty = self.hit.astype(np.float64) / n
        floor = self.floor if self.floor is not None else np.full(self.n_bins, -160.0)
        return floor, self.peak.copy(), self.hit.copy(), duty


def cluster_active(freq_grid, bin_hz, floor, peak, hit, duty, duty_thresh=0.02,
                   gap_bins=2):
    """Group contiguous active bins into detected frequency ranges."""
    idx = np.where(duty > duty_thresh)[0]
    ranges = []
    if len(idx) == 0:
        return ranges
    groups = [[idx[0]]]
    for k in idx[1:]:
        if k - groups[-1][-1] <= gap_bins:
            groups[-1].append(k)
        else:
            groups.append([k])
    for g in groups:
        g0, g1 = g[0], g[-1]
        ranges.append({
            "f_lo": float(freq_grid[g0] - bin_hz / 2.0),
            "f_hi": float(freq_grid[g1] + bin_hz / 2.0),
            "f_peak": float(freq_grid[g[int(np.argmax(peak[g]))]]),
            "peak_dbfs": float(np.max(peak[g])),
            "floor_dbfs": float(np.median(floor[g])),
            "duty": float(np.max(duty[g])),
            "bin_lo": int(g0), "bin_hi": int(g1),
        })
    ranges.sort(key=lambda r: -r["peak_dbfs"])
    return ranges


# ==========================================================================
# SQLite persistence
# ==========================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT,
  created_at REAL, ended_at REAL,
  f_start INTEGER, f_stop INTEGER, samp_rate INTEGER, fft_size INTEGER,
  bin_hz REAL, n_bins INTEGER, f_bin0 REAL, crop REAL, overlap_hz INTEGER,
  gain TEXT, ppm INTEGER, dwell_s REAL, n_avg INTEGER, device_index TEXT,
  dtype TEXT DEFAULT 'uint8', n_sweeps INTEGER DEFAULT 0, app_version TEXT);

CREATE TABLE IF NOT EXISTS power(
  session_id INTEGER, seq INTEGER, t_unix REAL, ref_dbm REAL, scale REAL,
  n_bins INTEGER, data BLOB, PRIMARY KEY(session_id, seq));

CREATE TABLE IF NOT EXISTS activity(
  session_id INTEGER, bin_idx INTEGER, floor_dbfs REAL, peak_dbfs REAL,
  hit_count INTEGER, duty REAL, PRIMARY KEY(session_id, bin_idx));

CREATE TABLE IF NOT EXISTS iq_dumps(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, seq INTEGER,
  center_hz INTEGER, samp_rate INTEGER, t_unix REAL, path TEXT,
  n_samples INTEGER, reason TEXT);

CREATE INDEX IF NOT EXISTS ix_power_session_t ON power(session_id, t_unix);
"""


class HeatmapDB:
    def __init__(self, path=DBPATH):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        # additive migration: per-frame capture duration (documents the time span
        # each frame [t_unix, t_unix + t_dur_ms] represents)
        try:
            self.conn.execute("ALTER TABLE power ADD COLUMN t_dur_ms REAL")
        except sqlite3.OperationalError:
            pass    # column already exists
        self.conn.commit()
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            self.conn.close()

    def create_session(self, cfg):
        d = cfg.as_dict()
        with self.lock:
            cur = self.conn.execute(
                """INSERT INTO sessions(label, created_at, ended_at, f_start,
                   f_stop, samp_rate, fft_size, bin_hz, n_bins, f_bin0, crop,
                   overlap_hz, gain, ppm, dwell_s, n_avg, device_index, dtype,
                   n_sweeps, app_version)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d["label"], clock.now_unix(), None, d["f_start"], d["f_stop"],
                 d["samp_rate"], d["fft_size"], d["bin_hz"], d["n_bins"],
                 d["f_bin0"], d["crop"], d["overlap_hz"], d["gain"], d["ppm"],
                 d["dwell_s"], d["n_avg"], d["device"], "uint8", 0, APP_VERSION))
            self.conn.commit()
            return cur.lastrowid

    def append_sweep(self, sid, seq, t_unix, ref, scale, blob, n_bins,
                     t_dur_ms=None):
        with self.lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO power(session_id, seq, t_unix, ref_dbm,
                   scale, n_bins, data, t_dur_ms) VALUES(?,?,?,?,?,?,?,?)""",
                (sid, seq, t_unix, ref, scale, n_bins, blob, t_dur_ms))
            self.conn.commit()

    def add_iq_dump(self, sid, seq, center_hz, samp_rate, path, n_samples, reason):
        with self.lock:
            self.conn.execute(
                """INSERT INTO iq_dumps(session_id, seq, center_hz, samp_rate,
                   t_unix, path, n_samples, reason) VALUES(?,?,?,?,?,?,?,?)""",
                (sid, seq, int(center_hz), int(samp_rate), clock.now_unix(), path,
                 int(n_samples), reason))
            self.conn.commit()

    def finalize_session(self, sid, n_sweeps, floor, peak, hit, duty):
        with self.lock:
            self.conn.execute(
                "UPDATE sessions SET ended_at=?, n_sweeps=? WHERE id=?",
                (clock.now_unix(), int(n_sweeps), sid))
            self.conn.execute("DELETE FROM activity WHERE session_id=?", (sid,))
            self.conn.executemany(
                """INSERT INTO activity(session_id, bin_idx, floor_dbfs,
                   peak_dbfs, hit_count, duty) VALUES(?,?,?,?,?,?)""",
                [(sid, i, float(floor[i]), float(peak[i]), int(hit[i]),
                  float(duty[i])) for i in range(len(duty)) if duty[i] > 0])
            self.conn.commit()

    # ---- reads (safe from any thread; SELECT under WAL) ----
    def list_sessions(self):
        cur = self.conn.execute(
            """SELECT id, label, created_at, ended_at, f_start, f_stop, n_bins,
               n_sweeps FROM sessions ORDER BY id DESC""")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def session_meta(self, sid):
        cur = self.conn.execute("SELECT * FROM sessions WHERE id=?", (sid,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))

    def load_matrix(self, sid):
        _lazy_np()
        meta = self.session_meta(sid)
        if meta is None:
            return None, None
        cur = self.conn.execute(
            """SELECT seq, t_unix, ref_dbm, scale, data FROM power
               WHERE session_id=? ORDER BY seq""", (sid,))
        rows, times = [], []
        for seq, t, ref, scale, blob in cur.fetchall():
            codes = np.frombuffer(blob, dtype=np.uint8)
            rows.append(dequantize(codes, ref, scale))
            times.append(t)
        if not rows:
            return np.zeros((0, meta["n_bins"]), dtype=np.float32), meta
        meta["times"] = times
        return np.vstack(rows), meta

    def activity_ranges(self, sid):
        meta = self.session_meta(sid)
        if meta is None:
            return []
        _lazy_np()
        cur = self.conn.execute(
            """SELECT bin_idx, floor_dbfs, peak_dbfs, hit_count, duty
               FROM activity WHERE session_id=? ORDER BY bin_idx""", (sid,))
        recs = cur.fetchall()
        if not recs:
            return []
        n = meta["n_bins"]
        floor = np.full(n, -160.0)
        peak = np.full(n, -300.0)
        hit = np.zeros(n, dtype=np.int64)
        duty = np.zeros(n)
        for bi, fl, pk, hc, du in recs:
            floor[bi], peak[bi], hit[bi], duty[bi] = fl, pk, hc, du
        grid = meta["f_bin0"] + np.arange(n) * meta["bin_hz"]
        return cluster_active(grid, meta["bin_hz"], floor, peak, hit, duty)


# ==========================================================================
# Capture loop (shared by CLI sync run and the GUI engine thread)
# ==========================================================================
def _dump_iq(db, sid, seq, source, cfg, reason):
    try:
        iq = source.last_iq()
        if iq is None:
            return
        os.makedirs(IQDIR, exist_ok=True)
        center = cfg.hops[-1] if cfg.hops else cfg.f_start
        # UTC file stamp matches the recorder's WAV naming convention (clock.py).
        path = os.path.join(IQDIR, "%s_s%d_%d_%d.cf32" % (
            clock.file_stamp(clock.now_unix()), sid, seq, int(center)))
        iq.astype(np.complex64).tofile(path)
        db.add_iq_dump(sid, seq, center, cfg.samp_rate, path, len(iq), reason)
        emit_event("iq_dump", session=sid, seq=seq, path=path,
                   n=len(iq), reason=reason)
    except Exception:
        logger.error("iq dump failed: %s", traceback.format_exc())


def run_capture(cfg, source, db, on_row=None, should_stop=None,
                should_pause=None, want_dump=None):
    """Run a full capture session. Returns (session_id, ranges, n_sweeps).

    on_row(seq, row_float, mask)   live callback (optional)
    should_stop() -> bool          cooperative stop
    should_pause() -> bool         cooperative pause (blocks while True)
    want_dump() -> bool            one-shot manual IQ dump request
    """
    _lazy_np()
    source.open()
    sid = db.create_session(cfg)
    acc = Accumulator(cfg.n_bins, cfg.margin_db)
    emit_event("session_start", session=sid, **cfg.as_dict())
    logger.info("session %d START %s", sid, cfg.as_dict())
    t0 = time.time()
    seq = 0
    try:
        while True:
            if should_stop and should_stop():
                break
            if should_pause and should_pause():
                time.sleep(0.05)
                continue
            if cfg.duration_s and (time.time() - t0) >= cfg.duration_s:
                break
            if cfg.max_sweeps and seq >= cfg.max_sweeps:
                break
            ts = clock.now_unix()           # UTC epoch at capture start
            row = source.sweep_once()
            dur_ms = (clock.now_unix() - ts) * 1000.0
            mask = acc.add(row)
            codes, ref, scale = quantize_row(row)
            db.append_sweep(sid, seq, ts, ref, scale, codes.tobytes(),
                            cfg.n_bins, dur_ms)
            # IQ dumps
            if cfg.iq_mode == "activity" and bool(mask.any()):
                _dump_iq(db, sid, seq, source, cfg, "activity")
            if want_dump and want_dump():
                _dump_iq(db, sid, seq, source, cfg, "manual")
            if on_row:
                on_row(seq, row, mask)
            dt = time.time() - ts
            logger.debug("session %d sweep %d dt=%.1fms active=%d "
                         "min/med/max=%.1f/%.1f/%.1f dBFS", sid, seq, dt * 1000,
                         int(mask.sum()), float(np.min(row)),
                         float(np.median(row)), float(np.max(row)))
            if seq % 25 == 0:
                logger.info("session %d sweep %d (%d active bins)", sid, seq,
                            int(mask.sum()))
                emit_event("progress", session=sid, seq=seq,
                           active=int(mask.sum()), elapsed=round(time.time() - t0, 1))
            seq += 1
    finally:
        floor, peak, hit, duty = acc.summary()
        db.finalize_session(sid, seq, floor, peak, hit, duty)
        grid = cfg.freq_grid
        ranges = cluster_active(grid, cfg.bin_hz, floor, peak, hit, duty)
        emit_event("session_end", session=sid, n_sweeps=seq,
                   detected=len(ranges))
        logger.info("session %d END %d sweeps, %d detected ranges",
                    sid, seq, len(ranges))
        try:
            source.close()
        except Exception:
            logger.error("source close: %s", traceback.format_exc())
    return sid, ranges, seq


# ==========================================================================
# Engine thread (GUI) — mirrors rf_hotscan.Scanner
# ==========================================================================
class HeatmapRecorder:
    def __init__(self, db=None):
        self.db = db or HeatmapDB()
        self.lock = threading.Lock()
        self.ui = {"state": "IDLE", "session": None, "seq": 0, "elapsed": 0.0,
                   "active": 0, "msg": "", "n_hops": 0, "n_bins": 0}
        self.rowq = queue.Queue()
        self.logq = queue.Queue()
        self.actions = queue.Queue()
        self.run = threading.Event()
        self.alive = True
        self._stop_session = threading.Event()
        self._dump_once = threading.Event()
        self._start_backend = None          # borrowed RtlBackend for next start
        self.cfg = None
        self.last_ranges = []
        self.last_session = None
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def request(self, name, **kw):
        self.actions.put((name, kw))

    def snapshot_ui(self):
        with self.lock:
            return dict(self.ui)

    def _set_ui(self, **kw):
        with self.lock:
            self.ui.update(kw)

    def _drain_actions(self):
        start_cfg = None
        while True:
            try:
                name, kw = self.actions.get_nowait()
            except queue.Empty:
                break
            if name == "start":
                start_cfg = kw.get("cfg")
                self._start_backend = kw.get("backend")   # borrowed RtlBackend or None
            elif name == "stop":
                self._stop_session.set()
                self.run.clear()
            elif name == "pause":
                self.run.clear()
            elif name == "resume":
                self.run.set()
            elif name == "dump":
                self._dump_once.set()
            elif name == "shutdown":
                self.alive = False
        return start_cfg

    def _loop(self):
        while self.alive:
            start_cfg = self._drain_actions()
            if start_cfg is None:
                time.sleep(0.05)
                continue
            self.cfg = start_cfg
            self._stop_session.clear()
            self.run.set()
            self._set_ui(state="RUNNING", session=None, seq=0, active=0,
                         n_hops=len(start_cfg.hops), n_bins=start_cfg.n_bins,
                         msg="opening source")
            source = make_source(start_cfg, backend=self._start_backend)
            self._start_backend = None
            t0 = time.time()

            def on_row(seq, row, mask):
                self.rowq.put((seq, row, mask))
                self._set_ui(seq=seq, active=int(mask.sum()),
                             elapsed=round(time.time() - t0, 1), state="RUNNING")

            def should_stop():
                self._drain_actions()
                return self._stop_session.is_set() or not self.alive

            def should_pause():
                return not self.run.is_set() and not self._stop_session.is_set()

            def want_dump():
                if self._dump_once.is_set():
                    self._dump_once.clear()
                    return True
                return False

            try:
                sid, ranges, seq = run_capture(
                    start_cfg, source, self.db, on_row=on_row,
                    should_stop=should_stop, should_pause=should_pause,
                    want_dump=want_dump)
                self.last_ranges = ranges
                self.last_session = sid
                self._set_ui(state="DONE", session=sid, seq=seq,
                             msg="%d detected ranges" % len(ranges))
                self.logq.put("session %d done: %d sweeps, %d detected ranges"
                              % (sid, seq, len(ranges)))
            except Exception:
                tb = traceback.format_exc()
                logger.error("recorder loop: %s", tb)
                self.logq.put("ERROR: " + tb.splitlines()[-1])
                self._set_ui(state="ERROR", msg=tb.splitlines()[-1])
            finally:
                self.run.clear()

    def shutdown(self):
        self.alive = False
        self._stop_session.set()
        self.request("shutdown")


# ==========================================================================
# Colormaps (anchor-interpolated 256-entry LUTs of #rrggbb hex strings).
# Pure-Python so the live Tk view needs no matplotlib.
# ==========================================================================
_CMAP_ANCHORS = {
    "inferno": [(0, 0, 4), (40, 11, 84), (101, 21, 110), (159, 42, 99),
                (212, 72, 66), (245, 125, 21), (250, 193, 39), (252, 255, 164)],
    "viridis": [(68, 1, 84), (59, 82, 139), (33, 145, 140), (94, 201, 98),
                (253, 231, 37)],
    "magma": [(0, 0, 4), (40, 11, 84), (101, 21, 110), (183, 55, 121),
              (251, 136, 97), (252, 253, 191)],
    "turbo": [(48, 18, 59), (65, 69, 171), (57, 131, 232), (44, 189, 196),
              (95, 221, 110), (170, 220, 50), (231, 173, 38), (231, 93, 29),
              (122, 4, 3)],
    "grey": [(0, 0, 0), (255, 255, 255)],
}


def _build_lut(anchors):
    lut = []
    m = len(anchors) - 1
    for i in range(256):
        x = i / 255.0 * m
        lo = int(math.floor(x))
        hi = min(lo + 1, m)
        fr = x - lo
        r = round(anchors[lo][0] + (anchors[hi][0] - anchors[lo][0]) * fr)
        g = round(anchors[lo][1] + (anchors[hi][1] - anchors[lo][1]) * fr)
        b = round(anchors[lo][2] + (anchors[hi][2] - anchors[lo][2]) * fr)
        lut.append("#%02x%02x%02x" % (r, g, b))
    return lut


COLORMAPS = {name: _build_lut(a) for name, a in _CMAP_ANCHORS.items()}


# ==========================================================================
# matplotlib offline renderer (DB re-render + PNG export) — lazy import.
# ==========================================================================
def render_session_png(db, sid, path, colormap="inferno", dmin=None, dmax=None,
                       dpi=130):
    """Re-render a stored session from SQLite to a PNG. Returns path or None."""
    matrix, meta = db.load_matrix(sid)
    if meta is None or matrix is None or matrix.shape[0] == 0:
        logger.warning("render: session %s has no data", sid)
        return None
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = _draw_heatmap_fig(plt, matrix, meta, colormap, dmin, dmax)
    fig.savefig(path, dpi=dpi, bbox_inches="tight", facecolor="#1e1e1e")
    plt.close(fig)
    emit_event("render_png", session=sid, path=path)
    logger.info("rendered session %d -> %s", sid, path)
    return path


def _draw_heatmap_fig(plt, matrix, meta, colormap, dmin, dmax):
    cmap = colormap if colormap in plt.colormaps() else "inferno"
    f0 = meta["f_start"] / 1e6
    f1 = meta["f_stop"] / 1e6
    n = matrix.shape[0]
    finite = matrix[np.isfinite(matrix)]
    a_lo, a_hi = auto_range(finite)
    if dmin is None:
        dmin = a_lo
    if dmax is None:
        dmax = a_hi
    if dmax - dmin < 6:
        dmax = dmin + 6.0
    fig, ax = plt.subplots(figsize=(11, 6))
    fig.patch.set_facecolor("#1e1e1e")
    ax.set_facecolor("#1e1e1e")
    im = ax.imshow(matrix, aspect="auto", origin="lower", cmap=cmap,
                   vmin=dmin, vmax=dmax, extent=[f0, f1, 0, n],
                   interpolation="nearest")
    ax.set_xlabel("Frequency (MHz)", color="#e6e6e6")
    ax.set_ylabel("Sweep #", color="#e6e6e6")
    label = meta.get("label") or ""
    ax.set_title("RF activity heatmap — session %s  %s" % (meta["id"], label),
                 color="#e6e6e6")
    ax.tick_params(colors="#9a9a9a")
    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("dBFS", color="#e6e6e6")
    cbar.ax.yaxis.set_tick_params(color="#9a9a9a")
    plt.setp(plt.getp(cbar.ax.axes, "yticklabels"), color="#9a9a9a")
    return fig, ax


# ==========================================================================
# GUI — palette (mirrors rf_hotscan) + live Tk waterfall + the Heatmap tab.
# ==========================================================================
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

BG = "#1e1e1e"
PANEL = "#2a2a2a"
PANEL2 = "#333333"
FG = "#e6e6e6"
MUTED = "#9a9a9a"
ACCENT = "#1e90ff"
ACTIVE = "#3ad13a"
HOT = "#ff5252"
GOLD = "#ffd24a"

SAMPLE_RATES = ["3200000", "2880000", "2400000", "2048000", "1800000", "1024000"]


class HeatmapView(tk.Frame):
    """Pure-Tk live waterfall: time (rows, newest at bottom) x frequency."""

    def __init__(self, parent, width=900, height=420):
        super().__init__(parent, bg=BG)
        self.W = width
        self.H = height
        self.colormap = "inferno"
        self.lut = COLORMAPS[self.colormap]
        self.dmin = None            # None => auto-range from the buffer
        self.dmax = None
        self._eff_lo = -100.0       # cached effective range (for auto)
        self._eff_hi = -20.0
        self._push_count = 0
        self.cfg = None
        self.starts = None          # bin->display-column reduceat indices
        self.buf = None             # (H, W) float32 ring of pooled dBFS
        self.have = 0

        top = tk.Frame(self, bg=BG)
        top.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(top, width=self.W, height=self.H, bg=BG,
                                highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=False)
        self.cbar = tk.Canvas(top, width=58, height=self.H, bg=BG,
                              highlightthickness=0)
        self.cbar.pack(side="left", fill="y")
        self.faxis = tk.Canvas(self, width=self.W, height=26, bg=BG,
                               highlightthickness=0)
        self.faxis.pack(side="top", anchor="w")

        self.photo = tk.PhotoImage(width=self.W, height=self.H)
        self.img_id = self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self._blank()
        self._draw_colorbar()

    # ---- setup ----
    def configure_session(self, cfg):
        _lazy_np()
        self.cfg = cfg
        n = cfg.n_bins
        W = min(self.W, n)
        # group bins -> display columns (max-pool preserves narrow signals)
        groups = np.array_split(np.arange(n), W)
        self.starts = np.array([g[0] for g in groups if len(g)], dtype=np.int64)
        self.Wd = len(self.starts)
        self.buf = np.full((self.H, self.Wd), np.nan, dtype=np.float32)
        self.have = 0
        self._blank()
        self._draw_faxis()
        self._draw_colorbar()

    def set_colormap(self, name):
        self.colormap = name if name in COLORMAPS else "inferno"
        self.lut = COLORMAPS[self.colormap]
        self._draw_colorbar()
        self._rebuild()

    def set_range(self, dmin, dmax):
        self.dmin = None if dmin is None else float(dmin)
        self.dmax = None if dmax is None else float(dmax)
        self._recompute_auto()
        self._draw_colorbar()
        self._rebuild()

    def _eff(self):
        lo = self.dmin if self.dmin is not None else self._eff_lo
        hi = self.dmax if self.dmax is not None else self._eff_hi
        if hi - lo < 1e-6:
            hi = lo + 6.0
        return lo, hi

    def _recompute_auto(self):
        if self.dmin is not None and self.dmax is not None:
            return
        if self.buf is None:
            return
        valid = self.buf[np.isfinite(self.buf)]
        if valid.size == 0:
            return
        self._eff_lo, self._eff_hi = auto_range(valid)

    # ---- live update ----
    def push_row(self, seq, row, mask=None):
        if self.buf is None:
            return
        pooled = np.maximum.reduceat(row, self.starts)
        # scroll ring buffer up by one, newest at bottom
        self.buf[:-1] = self.buf[1:]
        self.buf[-1] = pooled[:self.Wd]
        self.have = min(self.have + 1, self.H)
        if (self.dmin is None or self.dmax is None) and self._push_count % 12 == 0:
            self._recompute_auto()
            self._draw_colorbar()
        self._push_count += 1
        colors = self._row_colors(pooled[:self.Wd])
        try:
            self.photo.tk.call(self.photo, "copy", self.photo, "-from",
                               0, 1, self.Wd, self.H, "-to", 0, 0)
            self.photo.put("{" + " ".join(colors) + "}", to=(0, self.H - 1))
        except tk.TclError:
            self._rebuild()

    # ---- helpers ----
    def _row_colors(self, vals):
        lo, hi = self._eff()
        span = max(1e-6, hi - lo)
        idx = np.clip((vals - lo) / span * 255.0, 0, 255).astype(np.int32)
        lut = self.lut
        return [lut[i] for i in idx]

    def _rebuild(self):
        if self.buf is None:
            return
        rows = []
        for y in range(self.H):
            vals = self.buf[y]
            if np.isnan(vals).all():
                rows.append("{" + " ".join([BG] * self.Wd) + "}")
            else:
                v = np.nan_to_num(vals, nan=self.dmin)
                rows.append("{" + " ".join(self._row_colors(v)) + "}")
        try:
            self.photo.put(" ".join(rows), to=(0, 0))
        except tk.TclError:
            pass

    def _blank(self):
        try:
            self.photo.put("{" + " ".join([BG] * self.W) + "}",
                           to=(0, 0, self.W, self.H))
        except tk.TclError:
            pass

    def _draw_faxis(self):
        c = self.faxis
        c.delete("all")
        if not self.cfg:
            return
        f0, f1 = self.cfg.f_start, self.cfg.f_stop
        span = f1 - f0
        # choose ~6 round ticks
        step = _nice_step(span / 6.0)
        c.create_rectangle(0, 0, self.W, 26, fill=BG, outline="")
        t = math.ceil(f0 / step) * step
        while t <= f1:
            x = (t - f0) / span * self.Wd
            c.create_line(x, 0, x, 6, fill=MUTED)
            c.create_text(x, 15, text="%.3f" % (t / 1e6), fill=MUTED,
                          font=("Helvetica", 8))
            t += step

    def _draw_colorbar(self):
        c = self.cbar
        c.delete("all")
        h = self.H
        lo, hi = self._eff()
        for y in range(h):
            frac = 1.0 - y / max(1, h - 1)
            c.create_line(2, y, 16, y, fill=self.lut[int(frac * 255)])
        c.create_text(20, 6, text="%.0f" % hi, fill=MUTED, anchor="w",
                      font=("Helvetica", 8))
        c.create_text(20, h - 6, text="%.0f" % lo, fill=MUTED, anchor="w",
                      font=("Helvetica", 8))
        c.create_text(20, h // 2, text="dBFS", fill=MUTED, anchor="w",
                      font=("Helvetica", 8))


def _nice_step(x):
    if x <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(x))
    for m in (1, 2, 2.5, 5, 10):
        if x <= m * mag:
            return m * mag
    return 10 * mag


class HeatmapTab(tk.Frame):
    """The Heatmap tab: controls + live waterfall + detected-activity + DB load."""

    def __init__(self, parent, recorder=None, sdr_coordinator=None):
        super().__init__(parent, bg=BG)
        self.recorder = recorder or HeatmapRecorder()
        # Optional coordinator to share the one dongle with the Scanner tab:
        #   begin_external_use(label) -> connected RtlBackend to borrow, or None
        #   end_external_use()        -> resume the Scanner's SDR use
        # Pausing/resuming is tied to ACTUAL capture execution, not tab switching.
        self.coord = sdr_coordinator
        self._coord_active = False
        self._done_session = None       # last session shown after completion
        self.vars = {}
        self._build()
        self.after(150, self._refresh)

    # ---- layout ----
    def _build(self):
        ctrl = tk.Frame(self, bg=PANEL, width=270)
        ctrl.pack(side="left", fill="y")
        ctrl.pack_propagate(False)
        right = tk.Frame(self, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        self.view = HeatmapView(right)
        self.view.pack(side="top", fill="both", expand=False, padx=4, pady=4)

        bottom = tk.Frame(right, bg=BG)
        bottom.pack(side="top", fill="both", expand=True, padx=4)

        # detected-activity table
        cols = ("range", "peak", "duty")
        self.tree = ttk.Treeview(bottom, columns=cols, show="headings", height=7)
        self.tree.heading("range", text="Active range (MHz)")
        self.tree.heading("peak", text="Peak dBFS")
        self.tree.heading("duty", text="Duty %")
        self.tree.column("range", width=220)
        self.tree.column("peak", width=90, anchor="e")
        self.tree.column("duty", width=70, anchor="e")
        self.tree.pack(side="left", fill="both", expand=True)

        logf = tk.Frame(bottom, bg=BG)
        logf.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.log = tk.Text(logf, height=8, bg=PANEL2, fg=FG, bd=0,
                           font=("Menlo", 9), state="disabled")
        self.log.pack(fill="both", expand=True)

        self._build_controls(ctrl)

    def _row(self, parent, label, var, values=None, width=12):
        f = tk.Frame(parent, bg=PANEL)
        f.pack(fill="x", padx=8, pady=2)
        tk.Label(f, text=label, bg=PANEL, fg=MUTED, width=12, anchor="w",
                 font=("Helvetica", 10)).pack(side="left")
        if values:
            w = ttk.Combobox(f, textvariable=var, values=values, width=width - 2,
                             state="readonly")
        else:
            w = tk.Entry(f, textvariable=var, width=width, bg=PANEL2, fg=FG,
                         insertbackground=FG, bd=0)
        w.pack(side="right")
        return w

    def _build_controls(self, p):
        tk.Label(p, text="HEATMAP CAPTURE", bg=PANEL, fg=FG,
                 font=("Helvetica", 12, "bold")).pack(anchor="w", padx=8, pady=(8, 4))
        V = self.vars
        defs = {
            "start_mhz": "460.0", "stop_mhz": "466.0", "samp_rate": "2400000",
            "bin_hz": "3000", "gain": "auto", "ppm": "0", "dwell": "0.05",
            "n_avg": "8", "crop": "0.20", "overlap_hz": "0", "duration": "30",
            "margin_db": "8", "label": "",
        }
        for k, v in defs.items():
            V[k] = tk.StringVar(value=v)
        # Default to the real dongle (#0) — direct SDR is the primary path;
        # "fake" is the synthetic test source, chosen explicitly.
        V["device"] = tk.StringVar(value="0")
        V["colormap"] = tk.StringVar(value="inferno")
        V["iq_mode"] = tk.StringVar(value="off")
        V["dmin"] = tk.StringVar(value="auto")
        V["dmax"] = tk.StringVar(value="auto")

        self._row(p, "Start (MHz)", V["start_mhz"])
        self._row(p, "Stop (MHz)", V["stop_mhz"])
        self._row(p, "Sample rate", V["samp_rate"], SAMPLE_RATES)
        self._row(p, "Bin (Hz)", V["bin_hz"])
        self._row(p, "Gain", V["gain"])
        self._row(p, "PPM", V["ppm"])
        self._row(p, "Dwell (s)", V["dwell"])
        self._row(p, "Avg blocks", V["n_avg"])
        self._row(p, "Crop", V["crop"])
        self._row(p, "Overlap (Hz)", V["overlap_hz"])
        self._row(p, "Duration (s)", V["duration"])
        # Real dongle only in the GUI; the synthetic source is testing-only
        # (CLI `--device fake` / the test suite), never an operational choice.
        self._row(p, "Device", V["device"], ["0", "1"])
        self._row(p, "IQ dumps", V["iq_mode"], ["off", "manual", "activity"])
        self._row(p, "Margin dB", V["margin_db"])
        self._row(p, "Colormap", V["colormap"], list(COLORMAPS.keys()))
        self._row(p, "Color min", V["dmin"])
        self._row(p, "Color max", V["dmax"])
        self._row(p, "Label", V["label"])

        for var in ("colormap", "dmin", "dmax"):
            V[var].trace_add("write", lambda *a: self._apply_view_opts())

        btns = tk.Frame(p, bg=PANEL)
        btns.pack(fill="x", padx=8, pady=6)
        self.b_start = tk.Button(btns, text="▶ Start", command=self._start,
                                 bg=ACCENT, fg="#0a0a0a", bd=0,
                                 font=("Helvetica", 11, "bold"))
        self.b_start.pack(side="left", expand=True, fill="x", padx=2)
        self.b_stop = tk.Button(btns, text="■ Stop", command=self._stop,
                                bg=HOT, fg="#0a0a0a", bd=0,
                                font=("Helvetica", 11, "bold"))
        self.b_stop.pack(side="left", expand=True, fill="x", padx=2)

        btns2 = tk.Frame(p, bg=PANEL)
        btns2.pack(fill="x", padx=8, pady=2)
        tk.Button(btns2, text="Dump IQ", command=lambda: self.recorder.request("dump"),
                  bg=PANEL2, fg=FG, bd=0).pack(side="left", expand=True, fill="x", padx=2)
        tk.Button(btns2, text="Pause", command=lambda: self.recorder.request("pause"),
                  bg=PANEL2, fg=FG, bd=0).pack(side="left", expand=True, fill="x", padx=2)
        tk.Button(btns2, text="Resume", command=lambda: self.recorder.request("resume"),
                  bg=PANEL2, fg=FG, bd=0).pack(side="left", expand=True, fill="x", padx=2)

        tk.Label(p, text="SESSIONS (re-render)", bg=PANEL, fg=FG,
                 font=("Helvetica", 11, "bold")).pack(anchor="w", padx=8, pady=(10, 2))
        self.sess_var = tk.StringVar()
        self.sess_combo = ttk.Combobox(p, textvariable=self.sess_var, width=26,
                                       state="readonly")
        self.sess_combo.pack(padx=8, pady=2)
        srow = tk.Frame(p, bg=PANEL)
        srow.pack(fill="x", padx=8)
        tk.Button(srow, text="Render (matplotlib)", command=self._render_db,
                  bg=PANEL2, fg=FG, bd=0).pack(side="left", expand=True, fill="x", padx=2)
        tk.Button(srow, text="Export PNG", command=self._export_png,
                  bg=PANEL2, fg=FG, bd=0).pack(side="left", expand=True, fill="x", padx=2)

        self.status = tk.Label(p, text="IDLE", bg=PANEL, fg=MUTED, anchor="w",
                               font=("Helvetica", 10))
        self.status.pack(fill="x", padx=8, pady=(8, 4))
        self._refresh_sessions()

    # ---- actions ----
    def _build_cfg(self):
        V = self.vars
        return SweepConfig(
            f_start=float(V["start_mhz"].get()) * 1e6,
            f_stop=float(V["stop_mhz"].get()) * 1e6,
            samp_rate=int(V["samp_rate"].get()),
            bin_hz=float(V["bin_hz"].get()),
            gain=V["gain"].get().strip(),
            ppm=int(V["ppm"].get()),
            dwell_s=float(V["dwell"].get()),
            n_avg=int(V["n_avg"].get()),
            crop=float(V["crop"].get()),
            overlap_hz=int(V["overlap_hz"].get()),
            device=V["device"].get().strip(),
            duration_s=float(V["duration"].get()),
            margin_db=float(V["margin_db"].get()),
            iq_mode=V["iq_mode"].get(),
            colormap=V["colormap"].get(),
            dmin=V["dmin"].get(),
            dmax=V["dmax"].get(),
            label=V["label"].get(),
        )

    def _start(self):
        try:
            cfg = self._build_cfg()
        except (ValueError, KeyError) as e:
            messagebox.showerror("Invalid settings", str(e))
            return
        self._apply_view_opts()
        self.view.configure_session(cfg)
        for i in self.tree.get_children():
            self.tree.delete(i)
        # Coordinate the shared dongle for REAL captures: pause the Scanner's SDR
        # use and borrow its connected backend (no second open). Fake source and
        # the GQRX case (coordinator returns None) just open their own path.
        borrowed = None
        is_real = str(cfg.device).lower() not in ("fake", "sim", "synthetic")
        if not is_real:
            self._logline("NOTE: device='fake' — SYNTHETIC test source, NOT real "
                          "RF. Set Device to 0 to use the RTL-SDR dongle.")
        if is_real and self.coord is not None:
            try:
                borrowed = self.coord.begin_external_use("heatmap")
                self._coord_active = True
                if borrowed is not None:
                    self._logline("paused Scanner; borrowing its RTL backend")
                    # The borrowed dongle runs at ITS sample rate; the heatmap's
                    # FFT geometry must match or the stitch is wrong. Re-derive cfg
                    # at the backend's true rate if the field disagrees.
                    bfs = int(getattr(borrowed, "sample_rate", cfg.samp_rate))
                    if bfs != cfg.samp_rate:
                        self._logline("sample rate -> %d (borrowed backend)" % bfs)
                        self.vars["samp_rate"].set(str(bfs))
                        cfg = self._build_cfg()
                        self.view.configure_session(cfg)
            except Exception as e:
                self._coord_active = False
                self._logline("SDR coordination failed: %s" % e)
        self.recorder.request("start", cfg=cfg, backend=borrowed)
        self._logline("starting: %.3f-%.3f MHz, %d hops, %d bins"
                      % (cfg.f_start / 1e6, cfg.f_stop / 1e6, len(cfg.hops),
                         cfg.n_bins))

    def _release_coord(self):
        if self._coord_active and self.coord is not None:
            try:
                self.coord.end_external_use()
                self._logline("resumed Scanner SDR use")
            except Exception as e:
                self._logline("resume failed: %s" % e)
        self._coord_active = False

    def _stop(self):
        self.recorder.request("stop")

    def _apply_view_opts(self):
        try:
            self.view.set_colormap(self.vars["colormap"].get())
            self.view.set_range(_opt_float(self.vars["dmin"].get()),
                                _opt_float(self.vars["dmax"].get()))
        except (ValueError, tk.TclError):
            pass

    def _selected_session_id(self):
        s = self.sess_var.get()
        if not s:
            return None
        try:
            return int(s.split(":")[0].replace("#", "").strip())
        except ValueError:
            return None

    def _render_db(self):
        sid = self._selected_session_id()
        if sid is None:
            messagebox.showinfo("Render", "Select a session first.")
            return
        self._open_render_window(sid)

    def _export_png(self):
        sid = self._selected_session_id()
        if sid is None:
            messagebox.showinfo("Export", "Select a session first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".png",
                                            filetypes=[("PNG", "*.png")])
        if not path:
            return
        out = render_session_png(self.recorder.db, sid, path,
                                 colormap=self.vars["colormap"].get())
        self._logline("exported PNG: %s" % out if out else "export: no data")

    def _open_render_window(self, sid):
        matrix, meta = self.recorder.db.load_matrix(sid)
        if meta is None or matrix is None or matrix.shape[0] == 0:
            messagebox.showinfo("Render", "Session has no data.")
            return
        import matplotlib
        matplotlib.use("TkAgg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_tkagg import (
            FigureCanvasTkAgg, NavigationToolbar2Tk)
        win = tk.Toplevel(self)
        win.title("Heatmap — session %d" % sid)
        win.configure(bg=BG)
        fig, ax = _draw_heatmap_fig(plt, matrix, meta,
                                    self.vars["colormap"].get(), None, None)
        canvas = FigureCanvasTkAgg(fig, master=win)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(canvas, win)
        ranges = self.recorder.db.activity_ranges(sid)
        self._fill_tree(ranges)

    # ---- periodic refresh ----
    def _refresh(self):
        # drain live rows
        n = 0
        while n < 50:
            try:
                seq, row, mask = self.recorder.rowq.get_nowait()
            except queue.Empty:
                break
            self.view.push_row(seq, row, mask)
            n += 1
        # drain log
        while True:
            try:
                msg = self.recorder.logq.get_nowait()
            except queue.Empty:
                break
            self._logline(msg)
        ui = self.recorder.snapshot_ui()
        col = {"RUNNING": ACTIVE, "DONE": ACCENT, "ERROR": HOT,
               "IDLE": MUTED}.get(ui["state"], MUTED)
        self.status.config(
            text="%s  sweep %d  active %d  %.0fs  %s"
            % (ui["state"], ui["seq"], ui["active"], ui["elapsed"], ui["msg"]),
            fg=col)
        # Capture finished (or errored) -> resume the Scanner's SDR use exactly
        # once. Tied to capture execution, not tab switching.
        if ui["state"] in ("DONE", "ERROR", "IDLE") and self._coord_active:
            self._release_coord()
        # On the edge into DONE: show this capture's detected ranges and select
        # the just-finished session in the dropdown (so Render/Export use it, not
        # a stale older session).
        if ui["state"] == "DONE" and self.recorder.last_session != self._done_session:
            self._done_session = self.recorder.last_session
            self._fill_tree(self.recorder.last_ranges or [])
            self._refresh_sessions(select=self.recorder.last_session)
        self.after(150, self._refresh)

    def _fill_tree(self, ranges):
        for i in self.tree.get_children():
            self.tree.delete(i)
        for r in ranges:
            self.tree.insert("", "end", values=(
                "%.4f – %.4f" % (r["f_lo"] / 1e6, r["f_hi"] / 1e6),
                "%.1f" % r["peak_dbfs"], "%.0f" % (r["duty"] * 100)))

    def _refresh_sessions(self, select=None):
        try:
            sess = self.recorder.db.list_sessions()
        except Exception:
            sess = []
        items = ["#%d: %s (%d sw)" % (s["id"], s.get("label") or "—",
                                      s.get("n_sweeps") or 0) for s in sess]
        self.sess_combo["values"] = items
        if select is not None:
            for it in items:
                if it.startswith("#%d:" % select):
                    self.sess_var.set(it)
                    break
        elif items and not self.sess_var.get():
            self.sess_var.set(items[0])

    def _logline(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S ") + msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")


# ==========================================================================
# Headless CLI / agent API
# ==========================================================================
def run_scan(f_start, f_stop, device="0", db_path=DBPATH, png=None,
             on_event=None, **kw):
    """Run one capture synchronously and return a JSON-able result dict.

    Intended for agents: deterministic, no GUI. `**kw` accepts any SweepConfig
    parameter (samp_rate, bin_hz, gain, dwell_s, n_avg, crop, overlap_hz,
    duration_s, max_sweeps, margin_db, iq_mode, label, colormap, dmin, dmax)."""
    cfg = SweepConfig(f_start=f_start, f_stop=f_stop, device=device, **kw)
    db = HeatmapDB(db_path)
    source = make_source(cfg)
    try:
        sid, ranges, n = run_capture(cfg, source, db)
    except Exception as e:
        # Structured error for agents (e.g. DongleBusy / device unavailable)
        # instead of a raw traceback on stdout.
        db.close()
        logger.error("run_scan failed: %s", traceback.format_exc())
        return {"error": str(e), "error_type": type(e).__name__,
                "config": cfg.as_dict(), "session_id": None}
    png_path = None
    if png:
        png_path = render_session_png(db, sid, png, colormap=cfg.colormap)
    result = {"session_id": sid, "db_path": db_path, "n_sweeps": n,
              "config": cfg.as_dict(),
              "detected": [{"f_lo": r["f_lo"], "f_hi": r["f_hi"],
                            "f_peak": r["f_peak"], "peak_dbfs": r["peak_dbfs"],
                            "duty": r["duty"]} for r in ranges],
              "png_path": png_path}
    db.close()
    return result


def _cli(argv):
    import argparse
    p = argparse.ArgumentParser(prog="heatmap", description="RF activity heatmap")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="capture a heatmap")
    s.add_argument("--start", type=float, required=True, help="start freq Hz (e.g. 460e6)")
    s.add_argument("--stop", type=float, required=True, help="stop freq Hz")
    s.add_argument("--samp-rate", type=float, default=SAMPLE_RATE)
    s.add_argument("--bin-hz", type=float, default=3000.0)
    s.add_argument("--gain", default="auto")
    s.add_argument("--ppm", type=int, default=0)
    s.add_argument("--dwell", type=float, default=0.05)
    s.add_argument("--n-avg", type=int, default=8)
    s.add_argument("--crop", type=float, default=0.20)
    s.add_argument("--overlap-hz", type=int, default=0)
    s.add_argument("--device", default="0",
                   help="RTL device index (0,1,...). 'fake' = synthetic TEST "
                        "source, no hardware (testing/CI only).")
    s.add_argument("--duration", type=float, default=10.0)
    s.add_argument("--max-sweeps", type=int, default=0)
    s.add_argument("--margin-db", type=float, default=8.0)
    s.add_argument("--iq-mode", default="off", choices=["off", "manual", "activity"])
    s.add_argument("--colormap", default="inferno")
    s.add_argument("--label", default="cli")
    s.add_argument("--db", default=DBPATH)
    s.add_argument("--png", default=None)
    s.add_argument("--json", action="store_true")

    r = sub.add_parser("render", help="re-render a stored session to PNG")
    r.add_argument("session_id", type=int)
    r.add_argument("--db", default=DBPATH)
    r.add_argument("--png", required=True)
    r.add_argument("--colormap", default="inferno")

    li = sub.add_parser("list", help="list sessions as JSON")
    li.add_argument("--db", default=DBPATH)

    inf = sub.add_parser("info", help="session params + detected activity as JSON")
    inf.add_argument("session_id", type=int)
    inf.add_argument("--db", default=DBPATH)

    a = p.parse_args(argv)
    if a.cmd == "scan":
        res = run_scan(a.start, a.stop, device=a.device, db_path=a.db, png=a.png,
                       samp_rate=a.samp_rate, bin_hz=a.bin_hz, gain=a.gain,
                       ppm=a.ppm, dwell_s=a.dwell, n_avg=a.n_avg, crop=a.crop,
                       overlap_hz=a.overlap_hz, duration_s=a.duration,
                       max_sweeps=a.max_sweeps, margin_db=a.margin_db,
                       iq_mode=a.iq_mode, colormap=a.colormap, label=a.label)
        if a.json:
            print(json.dumps(res, indent=2))
        elif res.get("error"):
            print("ERROR (%s): %s" % (res["error_type"], res["error"]))
        else:
            print("session %d: %d sweeps -> %d detected ranges"
                  % (res["session_id"], res["n_sweeps"], len(res["detected"])))
            for d in res["detected"]:
                print("  %.4f-%.4f MHz  peak %.1f dBFS  duty %.0f%%"
                      % (d["f_lo"] / 1e6, d["f_hi"] / 1e6, d["peak_dbfs"],
                         d["duty"] * 100))
            if res["png_path"]:
                print("PNG:", res["png_path"])
        return 2 if res.get("error") else 0
    if a.cmd == "render":
        db = HeatmapDB(a.db)
        out = render_session_png(db, a.session_id, a.png, colormap=a.colormap)
        db.close()
        print(json.dumps({"png_path": out}))
        return 0 if out else 2
    if a.cmd == "list":
        db = HeatmapDB(a.db)
        print(json.dumps(db.list_sessions(), indent=2))
        db.close()
        return 0
    if a.cmd == "info":
        db = HeatmapDB(a.db)
        meta = db.session_meta(a.session_id)
        ranges = db.activity_ranges(a.session_id)
        db.close()
        print(json.dumps({"session": meta, "detected": ranges}, indent=2,
                         default=float))
        return 0 if meta else 2
    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
