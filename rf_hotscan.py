#!/usr/bin/env python3
"""
RF HotScan — a tag-aware bookmark scanner GUI for GQRX.

Reads ~/.config/gqrx/bookmarks.csv (the tag colours and channels we built),
drives GQRX over its remote-control TCP interface (Tools -> Remote control,
127.0.0.1:7356), and scans the bookmarks for activity above a squelch
threshold.

Features
  - Tag filtering that also shows/hides channels in the list
  - Hold-after-loss: park on an active channel, resume N seconds after it drops
  - Lockout: skip a noisy/constant channel for the session
  - Priority channels: tick the ★ column on any channel(s); they are checked
    periodically while scanning and pre-empt the held channel
  - Live dBFS signal meter with the active threshold marked
  - Auto-noise-floor: sample empty in-band frequencies and set per-band squelch
  - Read-back verification of every GQRX command, written to a verbose log that
    can be tailed:  ~/.config/gqrx/scanner.log

Run:  /opt/homebrew/bin/python3 rf_hotscan.py
Tail: tail -f ~/.config/gqrx/scanner.log
"""

import os
import json
import time
import queue
import socket
import logging
import threading
import traceback
import tkinter as tk
from tkinter import ttk

BOOKMARKS = os.path.expanduser("~/.config/gqrx/bookmarks.csv")
SETTINGS = os.path.expanduser("~/.config/gqrx/scanner_settings.json")
LOGFILE = os.path.expanduser("~/.config/gqrx/scanner.log")
HOST, PORT = "127.0.0.1", 7356

# Dark palette
BG = "#1e1e1e"
PANEL = "#2a2a2a"
PANEL2 = "#333333"
FG = "#e6e6e6"
MUTED = "#9a9a9a"
ACCENT = "#1e90ff"
ACTIVE = "#3ad13a"
HOT = "#ff5252"
GOLD = "#ffd24a"

METER_MIN, METER_MAX = -100.0, 0.0  # dBFS range for the meter

# --------------------------------------------------------------------------
# Logging (verbose, tailable). File gets DEBUG; GUI log gets INFO events.
# --------------------------------------------------------------------------
logger = logging.getLogger("gqrxscan")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _fh = logging.FileHandler(LOGFILE, mode="a")
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s", "%H:%M:%S"))
    logger.addHandler(_fh)
logger.info("=" * 60)
logger.info("==== scanner session start ====")


# --------------------------------------------------------------------------
# Bookmark parsing
# --------------------------------------------------------------------------
def map_mode(text):
    t = text.strip().lower()
    if "wfm" in t:
        return "WFM"
    if "am" in t and "fm" not in t:
        return "AM"
    return "FM"


def load_bookmarks(path):
    tags, chans, section = {}, [], None
    with open(path, newline="") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                if "Tag name" in line:
                    section = "tags"
                elif "Frequency" in line:
                    section = "chans"
                continue
            parts = [p.strip() for p in line.split(";")]
            if section == "tags" and len(parts) >= 2:
                tags[parts[0]] = parts[1]
            elif section == "chans" and len(parts) >= 5:
                try:
                    freq = int(parts[0])
                except ValueError:
                    continue
                bw = int(parts[3]) if parts[3].isdigit() else 10000
                chans.append({"freq": freq, "name": parts[1],
                              "mode": map_mode(parts[2]), "bw": bw,
                              "tag": parts[4]})
    return tags, chans


def cluster_bands(chans, gap=5_000_000):
    freqs = sorted({c["freq"] for c in chans})
    bands, cur = [], []
    for fr in freqs:
        if cur and fr - cur[-1] > gap:
            bands.append((cur[0], cur[-1]))
            cur = []
        cur.append(fr)
    if cur:
        bands.append((cur[0], cur[-1]))
    return bands


def band_index(freq, bands):
    for i, (lo, hi) in enumerate(bands):
        if lo - 1_000_000 <= freq <= hi + 1_000_000:
            return i
    return -1


def luminance(hexcolor):
    try:
        h = hexcolor.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    except Exception:
        return 0.5


def contrast_fg(hexcolor):
    return "#000000" if luminance(hexcolor) > 0.55 else "#ffffff"


# --------------------------------------------------------------------------
# GQRX remote-control client
# --------------------------------------------------------------------------
class GqrxClient:
    def __init__(self, host=HOST, port=PORT):
        self.host, self.port = host, port
        self.sock = None
        self.buf = b""
        self.lock = threading.Lock()

    def connect(self, timeout=3.0):
        s = socket.create_connection((self.host, self.port), timeout=timeout)
        s.settimeout(2.5)
        self.sock = s
        self.buf = b""

    def close(self):
        with self.lock:
            try:
                if self.sock:
                    self.sock.sendall(b"c\n")
                    self.sock.close()
            except Exception:
                pass
            self.sock = None

    @property
    def connected(self):
        return self.sock is not None

    def _readline(self):
        while b"\n" not in self.buf:
            data = self.sock.recv(1024)
            if not data:
                raise ConnectionError("connection closed")
            self.buf += data
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode(errors="replace").strip()

    def _cmd(self, text, nlines=1):
        with self.lock:
            if not self.sock:
                raise ConnectionError("not connected")
            self.sock.sendall((text + "\n").encode())
            return [self._readline() for _ in range(nlines)]

    def set_freq(self, hz):
        return self._cmd(f"F {int(hz)}")[0]

    def get_freq(self):
        return int(self._cmd("f")[0])

    def set_mode(self, mode, bw):
        return self._cmd(f"M {mode} {int(bw)}")[0]

    def get_mode(self):
        r = self._cmd("m", 2)
        return r[0], r[1]

    def strength(self):
        return float(self._cmd("l STRENGTH")[0])

    def get_sql(self):
        return float(self._cmd("l SQL")[0])

    def set_sql(self, dbfs):
        return self._cmd(f"L SQL {dbfs:.1f}")[0]

    def get_af(self):
        return float(self._cmd("l AF")[0])

    def set_af(self, db):
        return self._cmd(f"L AF {db:.1f}")[0]


# --------------------------------------------------------------------------
# Scanner engine (background thread)
# --------------------------------------------------------------------------
class Scanner:
    def __init__(self, client, tags, chans, bands):
        self.client = client
        self.tags = tags
        self.chans = chans
        self.bands = bands
        self.chan_freqs = sorted(c["freq"] for c in chans)

        self.lock = threading.Lock()
        self.cfg = {
            "enabled_tags": set(tags.keys()),
            "lockout": set(),
            "disabled_cids": set(),       # channels the user unticked (by cid)
            "priority_freqs": set(),
            "squelch_mode": "auto",       # "auto" | "global"
            "global_sql": -50.0,
            "auto_margin": 8.0,
            "settle_ms": 80,
            "hold_s": 3.0,
            "priority_interval": 6.0,
        }
        self.band_floor = {}
        self.last_active = {}

        self.ui = {"state": "STOPPED", "cur": None, "strength": -120.0,
                   "thresh": -50.0, "msg": "", "gqrx_sql": None, "af": None}
        self.logq = queue.Queue()
        self.actions = queue.Queue()

        self.alive = True
        self.run = threading.Event()
        self.skip = threading.Event()
        self._last_mode = None
        self._last_band = None
        self._last_sql = None
        self._last_sqlpoll = 0.0
        self._hops = 0

        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    # ---- config / state ----
    def set_cfg(self, **kw):
        with self.lock:
            self.cfg.update(kw)
        logger.debug("cfg <- %s", {k: (sorted(v) if isinstance(v, set) else v)
                                    for k, v in kw.items()})

    def get_cfg(self, key):
        with self.lock:
            v = self.cfg[key]
            return set(v) if isinstance(v, set) else v

    def snapshot_ui(self):
        with self.lock:
            return dict(self.ui)

    def log(self, msg, level=logging.INFO):
        """Event log: goes to the GUI panel AND the file."""
        self.logq.put(time.strftime("%H:%M:%S  ") + msg)
        logger.log(level, msg)

    def _set_ui(self, **kw):
        with self.lock:
            prev = self.ui.get("state")
            self.ui.update(kw)
            new = self.ui.get("state")
        if "state" in kw and new != prev:
            logger.info("STATE %s -> %s", prev, new)

    # ---- threshold logic ----
    def effective_threshold(self, freq):
        with self.lock:
            mode = self.cfg["squelch_mode"]
            gsql = self.cfg["global_sql"]
            margin = self.cfg["auto_margin"]
            floor = self.band_floor.get(band_index(freq, self.bands))
        if mode == "global" or floor is None:
            return gsql
        return floor + margin

    def active_list(self):
        with self.lock:
            tags = set(self.cfg["enabled_tags"])
            lock = set(self.cfg["lockout"])
            disabled = set(self.cfg["disabled_cids"])
        # Dedupe by frequency: a level-only scanner can't distinguish two
        # bookmarks that share a frequency (e.g. 153.800 / 935.225), so visit
        # each frequency once per sweep. Skip tag-filtered, locked-out and
        # individually-unticked channels.
        out, seen = [], set()
        for c in sorted(self.chans, key=lambda c: c["freq"]):
            if (c["tag"] in tags and c["freq"] not in lock
                    and c["cid"] not in disabled and c["freq"] not in seen):
                seen.add(c["freq"])
                out.append(c)
        return out

    def _channel_by_freq(self, freq):
        for c in self.chans:
            if c["freq"] == freq:
                return c
        return None

    # ---- gqrx helpers (lean in the hot path; verify only on demand) ----
    def _tune(self, ch, force_sql=False):
        settle = self.get_cfg("settle_ms") / 1000.0
        bi = band_index(ch["freq"], self.bands)
        if self._last_mode != (ch["mode"], ch["bw"]):
            self.client.set_mode(ch["mode"], ch["bw"])
            self._last_mode = (ch["mode"], ch["bw"])
            logger.debug("set_mode %s %d", ch["mode"], ch["bw"])
        self.client.set_freq(ch["freq"])
        thr = self.effective_threshold(ch["freq"])
        if force_sql or self._last_sql is None or abs(thr - self._last_sql) > 0.1:
            self.client.set_sql(thr)
            self._last_sql = thr
            logger.debug("set_sql %.1f dBFS", thr)
        extra = 0.15 if bi != self._last_band else 0.0
        self._last_band = bi
        time.sleep(settle + extra)
        return thr

    def _verify(self, ch):
        """Read GQRX back to confirm the tune/squelch took effect (debug only)."""
        try:
            f = self.client.get_freq()
            sql = self.client.get_sql()
        except Exception as e:
            logger.warning("verify failed: %s", e)
            return
        ok = (f == ch["freq"])
        logger.info("VERIFY %s: gqrx freq=%d (want %d) %s, sql=%.1f",
                    ch["name"], f, ch["freq"], "OK" if ok else "MISMATCH", sql)

    # ---- main loop ----
    def _loop(self):
        last_prio = 0.0
        while self.alive:
            try:
                self._drain_actions()
                if not self.client.connected:
                    self._set_ui(state="DISCONNECTED", cur=None)
                    time.sleep(0.25)
                    continue
                if not self.run.is_set():
                    self._set_ui(state="STOPPED")
                    self._maybe_poll_sql()
                    time.sleep(0.1)
                    continue

                lst = self.active_list()
                if not lst:
                    self._set_ui(state="SCANNING",
                                 msg="No channels match the tag filter")
                    time.sleep(0.3)
                    continue

                for ch in lst:
                    if not self.run.is_set() or not self.alive:
                        break
                    self._drain_actions()
                    self._maybe_poll_sql()

                    pf = self.get_cfg("priority_freqs")
                    if (pf and time.time() - last_prio
                            >= self.get_cfg("priority_interval")):
                        last_prio = time.time()
                        if self._check_priority(pf):
                            last_prio = time.time()
                            continue

                    thr = self._tune(ch)
                    s = self.client.strength()
                    self._hops += 1
                    self._set_ui(state="SCANNING", cur=ch, strength=s, thresh=thr)
                    logger.debug("HOP #%d %.4f %s  s=%.1f thr=%.1f %s",
                                 self._hops, ch["freq"] / 1e6, ch["tag"], s, thr,
                                 "** ACTIVE **" if s >= thr else "")
                    if s >= thr:
                        self._hold(ch)
                        last_prio = time.time()
            except (ConnectionError, OSError) as e:
                logger.warning("socket error in loop: %s", e)
                self._handle_disconnect()
            except Exception:
                # Never let the scan thread die silently.
                logger.error("UNEXPECTED in scan loop:\n%s", traceback.format_exc())
                self.log("Internal error (see scanner.log) — continuing",
                         level=logging.ERROR)
                time.sleep(0.3)

    def _check_priority(self, pf):
        for freq in sorted(pf):
            ch = self._channel_by_freq(freq)
            if not ch:
                continue
            thr = self._tune(ch)
            s = self.client.strength()
            self._set_ui(cur=ch, strength=s, thresh=thr)
            logger.debug("PRIO-CHECK %.4f s=%.1f thr=%.1f", freq / 1e6, s, thr)
            if s >= thr:
                self.log(f"PRIORITY active: {ch['name']}")
                self._hold(ch, priority=True)
                return True
        return False

    def _hold(self, ch, priority=False):
        thr = self.effective_threshold(ch["freq"])
        now = time.time()
        self.last_active[ch["freq"]] = now
        with self.lock:
            self.last_active = dict(self.last_active)
        self.log(f"{'PRIORITY ' if priority else ''}HOLD {ch['name']} "
                 f"({ch['freq']/1e6:.4f} MHz)")
        self._verify(ch)
        last_sig = now
        last_prio = now
        dbg = 0
        while self.alive and self.run.is_set():
            self._drain_actions()
            if self.skip.is_set():
                self.skip.clear()
                self.log("Skip -> resume scan")
                break
            s = self.client.strength()
            self._set_ui(state="HOLDING", cur=ch, strength=s, thresh=thr)
            now = time.time()
            dbg += 1
            if dbg % 6 == 0:   # ~ every 0.5s, avoid flooding
                logger.debug("HOLD %.4f s=%.1f thr=%.1f held=%.1fs",
                             ch["freq"] / 1e6, s, thr, now - last_sig)
            if s >= thr:
                last_sig = now
            elif now - last_sig >= self.get_cfg("hold_s"):
                logger.debug("HOLD release (%.1fs silence)", now - last_sig)
                break
            pf = self.get_cfg("priority_freqs")
            if (not priority and pf
                    and now - last_prio >= self.get_cfg("priority_interval")):
                last_prio = now
                others = sorted(f for f in pf if f != ch["freq"])
                for freq in others:
                    pch = self._channel_by_freq(freq)
                    if not pch:
                        continue
                    self._tune(pch)
                    if self.client.strength() >= self.effective_threshold(freq):
                        self.log(f"PRIORITY pre-empt -> {pch['name']}")
                        self._hold(pch, priority=True)
                        return
                self._tune(ch)  # return to held channel
            time.sleep(0.08)

    # ---- action queue (GUI -> worker; only the worker touches the socket) ----
    def request(self, name, **kw):
        self.actions.put((name, kw))
        logger.debug("action queued: %s %s", name, kw)

    def _drain_actions(self):
        while True:
            try:
                name, kw = self.actions.get_nowait()
            except queue.Empty:
                return
            try:
                if name == "noise_floor":
                    self._measure_noise_floor()
                elif name == "reconnect":
                    self._reconnect()
                elif name == "refresh_sql":
                    self._refresh_sql()
                elif name == "set_af":
                    if self.client.connected:
                        db = kw.get("db", 0.0)
                        self.client.set_af(db)
                        self.log(f"Audio gain set {db:.0f} dB")
                elif name == "goto":
                    ch = self._channel_by_freq(kw["freq"])
                    if ch and self.client.connected:
                        self._tune(ch, force_sql=True)
                        self._verify(ch)
            except (ConnectionError, OSError) as e:
                logger.warning("socket error in action %s: %s", name, e)
                self._handle_disconnect()
            except Exception:
                logger.error("action %s failed:\n%s", name, traceback.format_exc())

    def _reconnect(self):
        try:
            self.client.connect()
            self._last_mode = self._last_band = self._last_sql = None
            self._last_sqlpoll = 0.0
            self.log("Connected to GQRX remote (127.0.0.1:7356)")
            # Reflect GQRX's current squelch + audio gain in the GUI on connect.
            try:
                self._set_ui(gqrx_sql=self.client.get_sql(),
                             af=self.client.get_af())
            except Exception:
                pass
            self._set_ui(state="STOPPED", msg="")
        except Exception as e:
            self.log(f"Connect failed: {e}", level=logging.WARNING)
            self._set_ui(state="DISCONNECTED")

    def _maybe_poll_sql(self):
        """Read GQRX's squelch periodically so a change made in GQRX (or any
        other client) is reflected back into RF HotScan's global-squelch slider.
        Throttled so it never slows the scan."""
        now = time.time()
        if now - self._last_sqlpoll < 0.7 or not self.client.connected:
            return
        self._last_sqlpoll = now
        try:
            gsql = self.client.get_sql()
        except (ConnectionError, OSError):
            self._handle_disconnect()
            return
        self._set_ui(gqrx_sql=gsql)
        # Detect a change that did NOT originate from our own set_sql.
        if self._last_sql is None or abs(gsql - self._last_sql) > 0.6:
            self._last_sql = gsql
            if self.get_cfg("squelch_mode") == "global":
                if abs(self.get_cfg("global_sql") - gsql) > 0.6:
                    self.set_cfg(global_sql=gsql)
                    self.log(f"Squelch synced from GQRX: {gsql:.1f} dBFS")

    def _handle_disconnect(self):
        self.run.clear()
        self.client.close()
        self.log("Lost connection to GQRX — press Reconnect", level=logging.WARNING)
        self._set_ui(state="DISCONNECTED", cur=None)

    def _refresh_sql(self):
        """Push the effective squelch for the currently tuned freq to GQRX,
        and verify by read-back. Works even while stopped."""
        if not self.client.connected:
            return
        f = self.client.get_freq()
        thr = self.effective_threshold(f)
        self.client.set_sql(thr)
        self._last_sql = thr
        rb = self.client.get_sql()
        match = abs(rb - thr) < 0.6
        self.log(f"Squelch set {thr:.1f} dBFS @ {f/1e6:.4f} MHz "
                 f"(read-back {rb:.1f} {'OK' if match else 'MISMATCH'})")

    def _measure_noise_floor(self):
        if not self.client.connected:
            return
        # Pause scanning and signal a distinct CALIBRATING state so the user can
        # see RF HotScan is driving GQRX across the bands.
        was_scanning = self.run.is_set()
        self.run.clear()
        self.log("Measuring noise floor (sampling empty in-band frequencies)...")
        self._set_ui(state="CALIBRATING", cur=None,
                     msg="Auto-Noise-Floor: starting…")
        results = {}
        nbands = len(self.bands)
        guard, step, max_samples = 15_000, 25_000, 15
        for bi, (lo, hi) in enumerate(self.bands):
            cands = []
            f = lo - 100_000
            while f <= hi + 100_000:
                if all(abs(f - cf) > guard for cf in self.chan_freqs):
                    cands.append(f)
                f += step
            if not cands:
                continue
            if len(cands) > max_samples:
                k = len(cands) / max_samples
                cands = [cands[int(i * k)] for i in range(max_samples)]
            samples = []
            for j, f in enumerate(cands):
                if not self.alive:
                    return
                self.client.set_mode("FM", 10000)
                self.client.set_freq(f)
                time.sleep(self.get_cfg("settle_ms") / 1000.0 + 0.03)
                sig = self.client.strength()
                samples.append(sig)
                # live visual: move the meter + banner as we sample
                self._set_ui(state="CALIBRATING", strength=sig, thresh=-200,
                             msg=(f"Band {bi+1}/{nbands}  {lo/1e6:.0f}-{hi/1e6:.0f}"
                                  f" MHz   sample {j+1}/{len(cands)}  "
                                  f"@ {f/1e6:.4f}"))
            if samples:
                samples.sort()
                median = samples[len(samples) // 2]
                results[bi] = median
                self.log(f"  Band {lo/1e6:.3f}-{hi/1e6:.3f} MHz: "
                         f"floor ~{median:.1f} dBFS ({len(samples)} samples)")
        with self.lock:
            self.band_floor = results
        margin = self.get_cfg("auto_margin")
        self.log(f"Noise floor done. Auto squelch = floor + {margin:.0f} dB.")
        self._set_ui(state="STOPPED", msg="Noise floor updated")
        self._last_mode = self._last_sql = None
        if was_scanning:           # resume scanning if it was running
            self.run.set()
            self.log("Resuming scan after calibration")


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class ScannerGUI:
    def __init__(self, root):
        self.root = root
        self.tags, self.chans = load_bookmarks(BOOKMARKS)
        # Stable unique id per channel so duplicate-frequency bookmarks each get
        # their own tracked row (tree maps must not be keyed by frequency).
        for i, c in enumerate(self.chans):
            c["cid"] = i
        self.bands = cluster_bands(self.chans)
        self.client = GqrxClient()
        self.scanner = Scanner(self.client, self.tags, self.chans, self.bands)
        self.tag_btns = {}
        self.tree_iid = {}       # cid -> tree item id
        self.iid_cid = {}        # tree item id -> cid
        self._suppress_push = False   # guard: syncing slider FROM gqrx, don't push back
        self._af_inited = False       # audio-gain slider initialised from gqrx yet?

        self._load_settings()
        self._build_style()
        self._build_ui()
        self._rebuild_tree()

        self.scanner.request("reconnect")
        self.root.after(150, self._refresh)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- styling ----
    def _build_style(self):
        self.root.configure(bg=BG)
        self.root.title("RF HotScan — GQRX Bookmark Scanner")
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL2)
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("TButton", background=PANEL2, foreground=FG, borderwidth=0,
                     focuscolor=PANEL2, padding=6)
        st.map("TButton", background=[("active", "#454545")])
        st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff")
        st.map("Accent.TButton", background=[("active", "#3aa0ff")])
        st.configure("TRadiobutton", background=PANEL, foreground=FG)
        st.map("TRadiobutton", background=[("active", PANEL)])
        st.configure("Treeview", background=PANEL2, fieldbackground=PANEL2,
                     foreground=FG, rowheight=22, borderwidth=0)
        st.configure("Treeview.Heading", background=PANEL, foreground=MUTED,
                     borderwidth=0)
        st.map("Treeview", background=[("selected", "#0d3d66")],
               foreground=[("selected", "#ffffff")])

    # ---- layout ----
    def _build_ui(self):
        root = self.root
        root.geometry("1120x740")
        root.minsize(980, 660)

        banner = tk.Frame(root, bg=PANEL, height=120)
        banner.pack(fill="x", padx=10, pady=(10, 6))
        banner.pack_propagate(False)
        left = tk.Frame(banner, bg=PANEL)
        left.pack(side="left", fill="both", expand=True, padx=14, pady=10)
        self.tag_chip = tk.Label(left, text="  —  ", bg=PANEL2, fg=FG,
                                 font=("Helvetica", 12, "bold"), padx=8)
        self.tag_chip.pack(side="top", anchor="w")
        self.lbl_name = tk.Label(left, text="Idle", bg=PANEL, fg=FG,
                                 font=("Helvetica", 22, "bold"))
        self.lbl_name.pack(side="top", anchor="w", pady=(4, 0))
        self.lbl_freq = tk.Label(left, text="—  MHz", bg=PANEL, fg=ACCENT,
                                 font=("Helvetica", 16))
        self.lbl_freq.pack(side="top", anchor="w")
        right = tk.Frame(banner, bg=PANEL)
        right.pack(side="right", fill="y", padx=14, pady=10)
        self.lbl_state = tk.Label(right, text="STOPPED", bg=PANEL, fg=MUTED,
                                  font=("Helvetica", 16, "bold"))
        self.lbl_state.pack(side="top", anchor="e")
        self.lbl_sig = tk.Label(right, text="-- dBFS", bg=PANEL, fg=FG,
                                font=("Helvetica", 14))
        self.lbl_sig.pack(side="top", anchor="e", pady=(6, 0))

        meter_wrap = tk.Frame(root, bg=BG)
        meter_wrap.pack(fill="x", padx=10)
        self.meter = tk.Canvas(meter_wrap, height=26, bg=PANEL2,
                               highlightthickness=0)
        self.meter.pack(fill="x")

        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=8)
        ctrl = tk.Frame(body, bg=PANEL, width=300)
        ctrl.pack(side="left", fill="y")
        ctrl.pack_propagate(False)
        self._build_controls(ctrl)
        rightcol = tk.Frame(body, bg=BG)
        rightcol.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self._build_taglist(rightcol)
        self._build_channel_list(rightcol)
        self._build_log(rightcol)

    def _section(self, parent, text):
        tk.Label(parent, text=text, bg=PANEL, fg=MUTED,
                 font=("Helvetica", 10, "bold")).pack(anchor="w", padx=12,
                                                      pady=(12, 2))

    def _build_controls(self, p):
        self._section(p, "TRANSPORT")
        row = tk.Frame(p, bg=PANEL)
        row.pack(fill="x", padx=12)
        self.btn_start = ttk.Button(row, text="▶ Scan", style="Accent.TButton",
                                    command=self._toggle_scan)
        self.btn_start.pack(side="left", expand=True, fill="x", padx=(0, 4))
        ttk.Button(row, text="⏭ Skip",
                   command=lambda: self.scanner.skip.set()).pack(
                       side="left", expand=True, fill="x", padx=(4, 0))
        row2 = tk.Frame(p, bg=PANEL)
        row2.pack(fill="x", padx=12, pady=(6, 0))
        ttk.Button(row2, text="🔒 Lockout current",
                   command=self._lockout_current).pack(
                       side="left", expand=True, fill="x", padx=(0, 4))
        ttk.Button(row2, text="Clear",
                   command=self._clear_lockouts).pack(side="left", padx=(4, 0))
        rcrow = tk.Frame(p, bg=PANEL)
        rcrow.pack(fill="x", padx=12, pady=(6, 0))
        self.conn_dot = tk.Canvas(rcrow, width=16, height=16, bg=PANEL,
                                  highlightthickness=0)
        self.conn_dot.pack(side="left", padx=(0, 6))
        self._dot = self.conn_dot.create_oval(3, 3, 13, 13, fill=HOT, outline="")
        self.btn_reconnect = ttk.Button(
            rcrow, text="⟳ Reconnect GQRX",
            command=lambda: self.scanner.request("reconnect"))
        self.btn_reconnect.pack(side="left", fill="x", expand=True)

        self._section(p, "SQUELCH")
        self.sql_mode = tk.StringVar(value=self.scanner.get_cfg("squelch_mode"))
        for val, label in (("auto", "Auto (noise-floor)"),
                           ("global", "Global threshold")):
            ttk.Radiobutton(p, text=label, value=val, variable=self.sql_mode,
                            command=self._apply_sql_mode).pack(anchor="w", padx=12)
        self.global_sql = tk.DoubleVar(value=self.scanner.get_cfg("global_sql"))
        _, self._gsql_label = self._slider(
            p, "Global squelch", self.global_sql, -100, -10, "dBFS",
            self._apply_sliders)
        self.margin = tk.DoubleVar(value=self.scanner.get_cfg("auto_margin"))
        self._slider(p, "Auto margin", self.margin, 2, 25, "dB",
                     self._apply_sliders)
        self.btn_nf = ttk.Button(p, text="📈 Auto-Noise-Floor",
                                 command=lambda: self.scanner.request("noise_floor"))
        self.btn_nf.pack(fill="x", padx=12, pady=(6, 0))

        self._section(p, "AUDIO")
        self.af_gain = tk.DoubleVar(value=0.0)
        _, self._af_label = self._slider(
            p, "Audio gain", self.af_gain, -80, 50, "dB", self._apply_gain)

        self._section(p, "TIMING")
        self.settle = tk.DoubleVar(value=self.scanner.get_cfg("settle_ms"))
        self._slider(p, "Settle per hop", self.settle, 20, 300, "ms",
                     self._apply_sliders)
        self.hold = tk.DoubleVar(value=self.scanner.get_cfg("hold_s"))
        self._slider(p, "Hold after loss", self.hold, 0.5, 15, "s",
                     self._apply_sliders)

        self._section(p, "PRIORITY")
        tk.Label(p, text="Tick the ★ column in the list\nto flag priority channels.",
                 bg=PANEL, fg=MUTED, font=("Helvetica", 9),
                 justify="left").pack(anchor="w", padx=12)
        self.prio_int = tk.DoubleVar(
            value=self.scanner.get_cfg("priority_interval"))
        self._slider(p, "Priority interval", self.prio_int, 2, 30, "s",
                     self._apply_priority)

    def _slider(self, parent, label, var, lo, hi, unit, cb):
        f = tk.Frame(parent, bg=PANEL)
        f.pack(fill="x", padx=12, pady=(6, 0))
        top = tk.Frame(f, bg=PANEL)
        top.pack(fill="x")
        tk.Label(top, text=label, bg=PANEL, fg=FG,
                 font=("Helvetica", 10)).pack(side="left")
        val = tk.Label(top, text=f"{var.get():.0f} {unit}", bg=PANEL, fg=ACCENT,
                       font=("Helvetica", 10, "bold"))
        val.pack(side="right")

        def on_move(_=None):
            val.config(text=f"{var.get():.0f} {unit}")
            cb()
        ttk.Scale(f, from_=lo, to=hi, variable=var, orient="horizontal",
                  command=on_move).pack(fill="x")
        return f, val

    def _build_taglist(self, parent):
        hdr = tk.Frame(parent, bg=BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text="TAG FILTER (click to show/hide)", bg=BG, fg=MUTED,
                 font=("Helvetica", 10, "bold")).pack(side="left")
        ttk.Button(hdr, text="None", width=6,
                   command=self._clear_all_tags).pack(side="right", padx=2)
        ttk.Button(hdr, text="All", width=6,
                   command=self._select_all_tags).pack(side="right", padx=2)
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(fill="x", pady=(2, 8))
        enabled = self.scanner.get_cfg("enabled_tags")
        for tag, color in self.tags.items():
            on = tag in enabled
            b = tk.Label(wrap, text=tag, bg=color if on else PANEL2,
                         fg=contrast_fg(color) if on else color,
                         font=("Helvetica", 11, "bold"), padx=10, pady=4,
                         cursor="hand2")
            b.pack(side="left", padx=3)
            b.bind("<Button-1>", lambda e, t=tag: self._toggle_tag(t))
            self.tag_btns[tag] = b

    def _build_channel_list(self, parent):
        cols = ("on", "prio", "freq", "name", "tag", "status", "last")
        self.tree = ttk.Treeview(parent, columns=cols, show="headings", height=12)
        layout = (("on", 36, "On"), ("prio", 34, "★"), ("freq", 95, "Freq MHz"),
                  ("name", 290, "Channel"), ("tag", 70, "Tag"),
                  ("status", 80, "Status"), ("last", 90, "Last active"))
        for c, w, txt in layout:
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w,
                             anchor="w" if c == "name" else "center",
                             stretch=(c == "name"))
        for tag, color in self.tags.items():
            self.tree.tag_configure(tag, foreground=color)
        self.tree.tag_configure("locked", foreground=MUTED)
        self.tree.tag_configure("disabled", foreground="#5a5a5a")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_tree_double)
        for c in sorted(self.chans, key=lambda c: c["freq"]):
            iid = self.tree.insert("", "end", values=(
                "☑", "", f"{c['freq']/1e6:.4f}", c["name"], c["tag"], "", ""),
                tags=(c["tag"],))
            self.tree_iid[c["cid"]] = iid
            self.iid_cid[iid] = c["cid"]

    def _build_log(self, parent):
        tk.Label(parent, text="LOG  (full detail: ~/.config/gqrx/scanner.log)",
                 bg=BG, fg=MUTED, font=("Helvetica", 10, "bold")).pack(
                     anchor="w", pady=(8, 0))
        self.log = tk.Text(parent, height=7, bg=PANEL2, fg=FG, bd=0,
                           font=("Menlo", 10), insertbackground=FG)
        self.log.pack(fill="both", expand=False)
        self.log.configure(state="disabled")

    # ---- tree show/hide by tag ----
    def _rebuild_tree(self):
        enabled = self.scanner.get_cfg("enabled_tags")
        for iid in self.tree_iid.values():
            self.tree.detach(iid)
        for c in sorted(self.chans, key=lambda c: c["freq"]):
            if c["tag"] in enabled:
                self.tree.reattach(self.tree_iid[c["cid"]], "", "end")

    # ---- control callbacks ----
    def _toggle_scan(self):
        if self.scanner.run.is_set():
            self.scanner.run.clear()
            self.btn_start.config(text="▶ Scan")
            logger.info("user: PAUSE")
        else:
            if not self.client.connected:
                self.scanner.request("reconnect")
            self.scanner.run.set()
            self.btn_start.config(text="⏸ Pause")
            logger.info("user: START scan")

    def _restyle_tag(self, tag, on):
        color = self.tags[tag]
        self.tag_btns[tag].config(bg=color if on else PANEL2,
                                  fg=contrast_fg(color) if on else color)

    def _toggle_tag(self, tag):
        en = self.scanner.get_cfg("enabled_tags")
        if tag in en:
            en.discard(tag)
        else:
            en.add(tag)
        self.scanner.set_cfg(enabled_tags=en)
        self._restyle_tag(tag, tag in en)
        self._rebuild_tree()

    def _select_all_tags(self):
        self.scanner.set_cfg(enabled_tags=set(self.tags))
        for tag in self.tags:
            self._restyle_tag(tag, True)
        self._rebuild_tree()
        self.scanner.log("Tag filter: all shown")

    def _clear_all_tags(self):
        self.scanner.set_cfg(enabled_tags=set())
        for tag in self.tags:
            self._restyle_tag(tag, False)
        self._rebuild_tree()
        self.scanner.log("Tag filter: all hidden")

    def _lockout_current(self):
        cur = self.scanner.snapshot_ui().get("cur")
        if not cur:
            return
        lk = self.scanner.get_cfg("lockout")
        lk.add(cur["freq"])
        self.scanner.set_cfg(lockout=lk)
        self.scanner.skip.set()
        self.scanner.log(f"Lockout {cur['name']}")

    def _clear_lockouts(self):
        self.scanner.set_cfg(lockout=set())
        self.scanner.log("Cleared all lockouts")

    def _apply_sql_mode(self):
        self.scanner.set_cfg(squelch_mode=self.sql_mode.get())
        self.scanner.request("refresh_sql")

    def _apply_sliders(self, _=None):
        self.scanner.set_cfg(global_sql=self.global_sql.get(),
                             auto_margin=self.margin.get(),
                             settle_ms=int(self.settle.get()),
                             hold_s=self.hold.get())
        # When the slider was moved programmatically to mirror GQRX, don't echo
        # it straight back (avoids a sync feedback loop).
        if not self._suppress_push:
            self.scanner.request("refresh_sql")

    def _apply_gain(self, _=None):
        if not self._suppress_push:
            self.scanner.request("set_af", db=self.af_gain.get())

    def _apply_priority(self, _=None):
        self.scanner.set_cfg(priority_interval=self.prio_int.get())

    def _chan_for_iid(self, iid):
        cid = self.iid_cid.get(iid)
        return self.chans[cid] if cid is not None else None

    def _on_tree_click(self, event):
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        col = self.tree.identify_column(event.x)
        if col not in ("#1", "#2"):       # #1 = On checkbox, #2 = ★ priority
            return
        ch = self._chan_for_iid(self.tree.identify_row(event.y))
        if ch is None:
            return
        if col == "#1":
            dis = self.scanner.get_cfg("disabled_cids")
            if ch["cid"] in dis:
                dis.discard(ch["cid"])
            else:
                dis.add(ch["cid"])
            self.scanner.set_cfg(disabled_cids=dis)
            self.scanner.log(("Enabled " if ch["cid"] not in dis else "Disabled ")
                             + ch["name"])
        else:
            freq = ch["freq"]
            pf = self.scanner.get_cfg("priority_freqs")
            if freq in pf:
                pf.discard(freq)
            else:
                pf.add(freq)
            self.scanner.set_cfg(priority_freqs=pf)
            self.scanner.log(("Priority +" if freq in pf else "Priority -")
                             + f" {ch['name']}")
        return "break"

    def _on_tree_double(self, _):
        sel = self.tree.selection()
        if sel:
            ch = self._chan_for_iid(sel[0])
            if ch is not None:
                self.scanner.request("goto", freq=ch["freq"])

    # ---- periodic refresh ----
    def _refresh(self):
        ui = self.scanner.snapshot_ui()
        state, cur, s, thr = ui["state"], ui["cur"], ui["strength"], ui["thresh"]
        calibrating = (state == "CALIBRATING")
        statecolor = {"SCANNING": ACCENT, "HOLDING": ACTIVE, "STOPPED": MUTED,
                      "DISCONNECTED": HOT, "CALIBRATING": GOLD}.get(state, FG)
        self.lbl_state.config(text=state, fg=statecolor)
        if calibrating:
            self.tag_chip.config(text=" CALIBRATING ", bg=GOLD,
                                 fg=contrast_fg(GOLD))
            self.lbl_name.config(text="🛰  Auto-Noise-Floor")
            self.lbl_freq.config(text=ui.get("msg", "sampling…"))
            self.btn_nf.config(text="📈 Calibrating…")
        else:
            self.btn_nf.config(text="📈 Auto-Noise-Floor")
            if cur:
                color = self.tags.get(cur["tag"], PANEL2)
                self.tag_chip.config(text=f" {cur['tag']} ", bg=color,
                                     fg=contrast_fg(color))
                self.lbl_name.config(text=cur["name"])
                self.lbl_freq.config(
                    text=f"{cur['freq']/1e6:.4f}  MHz   ·   {cur['mode']}")
        self.lbl_sig.config(text=f"{s:.1f} dBFS   (thr {thr:.0f})")
        self._draw_meter(s, thr)
        self._update_tree(cur)
        self._drain_log()

        # connection indicator dot
        connected = self.client.connected and state != "DISCONNECTED"
        self.conn_dot.itemconfig(self._dot, fill=ACTIVE if connected else HOT)

        # mirror GQRX's squelch into the global-squelch slider (two-way sync)
        gq = ui.get("gqrx_sql")
        if (gq is not None and self.sql_mode.get() == "global"
                and abs(self.global_sql.get() - gq) > 0.6):
            self._suppress_push = True
            self.global_sql.set(round(gq, 1))
            self._gsql_label.config(text=f"{gq:.0f} dBFS")
            self._suppress_push = False

        # initialise audio-gain slider from GQRX once after connect
        af = ui.get("af")
        if af is not None and not self._af_inited:
            self._af_inited = True
            self._suppress_push = True
            self.af_gain.set(round(af, 1))
            self._af_label.config(text=f"{af:.0f} dB")
            self._suppress_push = False

        self.btn_start.config(text="⏸ Pause" if self.scanner.run.is_set()
                              else "▶ Scan")
        self.root.after(120, self._refresh)

    def _draw_meter(self, s, thr):
        c = self.meter
        c.delete("all")
        w = c.winfo_width() or 800
        h = int(c["height"])

        def x(db):
            db = max(METER_MIN, min(METER_MAX, db))
            return (db - METER_MIN) / (METER_MAX - METER_MIN) * w
        active = s >= thr
        c.create_rectangle(0, 0, x(s), h, fill=ACTIVE if active else ACCENT,
                           width=0)
        tx = x(thr)
        c.create_line(tx, 0, tx, h, fill=HOT, width=2)
        for db in range(int(METER_MIN), int(METER_MAX) + 1, 20):
            c.create_line(x(db), h - 5, x(db), h, fill=MUTED)
            c.create_text(x(db) + 2, 8, text=f"{db}", anchor="w", fill=MUTED,
                          font=("Menlo", 8))

    def _update_tree(self, cur):
        lk = self.scanner.get_cfg("lockout")
        pf = self.scanner.get_cfg("priority_freqs")
        dis = self.scanner.get_cfg("disabled_cids")
        la = dict(self.scanner.last_active)
        now = time.time()
        for c in self.chans:
            iid = self.tree_iid[c["cid"]]
            locked = c["freq"] in lk
            off = c["cid"] in dis
            last = la.get(c["freq"])
            laststr = f"{int(now - last)}s ago" if last else ""
            star = "★" if c["freq"] in pf else ""
            on = "☐" if off else "☑"
            status = "OFF" if off else ("LOCKED" if locked else "")
            row_tag = ("disabled",) if off else (
                ("locked",) if locked else (c["tag"],))
            self.tree.item(iid, values=(on, star, f"{c['freq']/1e6:.4f}",
                                        c["name"], c["tag"], status, laststr),
                           tags=row_tag)
        if cur:
            iid = self.tree_iid.get(cur["cid"])
            if iid and self.tree.exists(iid):
                try:
                    self.tree.selection_set(iid)
                    self.tree.see(iid)
                except tk.TclError:
                    pass

    def _drain_log(self):
        lines = []
        while True:
            try:
                lines.append(self.scanner.logq.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.log.configure(state="normal")
            for ln in lines:
                self.log.insert("end", ln + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")

    # ---- settings ----
    def _load_settings(self):
        try:
            with open(SETTINGS) as f:
                d = json.load(f)
        except Exception:
            return
        c = self.scanner.cfg
        for k in ("squelch_mode", "global_sql", "auto_margin", "settle_ms",
                  "hold_s", "priority_interval"):
            if k in d:
                c[k] = d[k]
        if d.get("enabled_tags"):
            c["enabled_tags"] = set(d["enabled_tags"]) & set(self.tags)
        c["lockout"] = set(d.get("lockout", []))
        c["priority_freqs"] = set(d.get("priority_freqs", []))
        # Disabled channels are stored by a stable "freq:name" signature so the
        # selection survives bookmark-file edits that would shift cid indices.
        sigs = set(d.get("disabled", []))
        c["disabled_cids"] = {ch["cid"] for ch in self.chans
                              if self._sig(ch) in sigs}

    @staticmethod
    def _sig(ch):
        return f"{ch['freq']}:{ch['name']}"

    def _save_settings(self):
        c = self.scanner.cfg
        d = {k: c[k] for k in ("squelch_mode", "global_sql", "auto_margin",
                               "settle_ms", "hold_s", "priority_interval")}
        d["enabled_tags"] = sorted(c["enabled_tags"])
        d["lockout"] = sorted(c["lockout"])
        d["priority_freqs"] = sorted(c["priority_freqs"])
        by_cid = {ch["cid"]: ch for ch in self.chans}
        d["disabled"] = sorted(self._sig(by_cid[cid])
                               for cid in c["disabled_cids"] if cid in by_cid)
        try:
            with open(SETTINGS, "w") as f:
                json.dump(d, f, indent=2)
        except Exception:
            pass

    def _on_close(self):
        self.scanner.alive = False
        self.scanner.run.clear()
        self._save_settings()
        try:
            self.client.close()
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    ScannerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
