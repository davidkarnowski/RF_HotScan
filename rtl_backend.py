#!/usr/bin/env python3
"""
RF HotScan — direct RTL-SDR backend (Phase 1 channelized sweep + Phase 2 FM audio).

This is the GQRX-free path from dev/SPIKE_direct_rtlsdr.md. It owns the dongle
directly via pyrtlsdr and provides:

  * sweep(freqs, bw)  -> {freq_hz: power_dbfs}
        Channelized detection: groups channels into ~2 MHz windows, captures
        each window ONCE, FFTs it, and reads per-channel power. Our ~77 bookmark
        frequencies collapse from ~77 sequential GQRX hops to ~8-12 captures.

  * tune(hz) + FM demod + sounddevice playback for the parked channel.

Dependencies (optional, lazily imported so the GQRX path stays stdlib-only):
    brew install rtl-sdr
    pip install numpy scipy sounddevice pyrtlsdr      (use the project .venv)

Run the self-test (GQRX must be closed — the dongle is single-owner):
    .venv/bin/python rtl_backend.py            # sweep the real bookmark set
    .venv/bin/python rtl_backend.py --listen 462012500 8   # demod+play 8 s
"""

import sys
import time
import math
import threading

# Lazy heavy imports — only when the RTL backend is actually used.
np = None
signal = None
RtlSdr = None


def _lazy_imports():
    global np, signal, RtlSdr
    if np is None:
        import numpy as _np
        from scipy import signal as _signal
        from rtlsdr import RtlSdr as _RtlSdr
        np, signal, RtlSdr = _np, _signal, _RtlSdr


# Usable bandwidth: keep clear of the DC spike and the rolled-off band edges.
SAMPLE_RATE = 2_400_000
USABLE_BW = 2_000_000          # of the 2.4 MHz captured, treat ~2.0 as usable
DC_GUARD = 30_000              # keep channels at least this far off 0 Hz (DC)


def plan_windows(freqs, usable=USABLE_BW, dc_guard=DC_GUARD):
    """Greedily pack sorted channel freqs into the fewest ~`usable`-wide windows.

    Returns a list of (center_hz, [member_freqs]). Centers are nudged so no
    member sits on the DC spike."""
    freqs = sorted(set(freqs))
    windows = []
    i = 0
    half = usable / 2.0
    while i < len(freqs):
        start = freqs[i]
        # take every channel that fits within `usable` of the window start
        j = i
        while j < len(freqs) and freqs[j] - start <= usable:
            j += 1
        members = freqs[i:j]
        center = (members[0] + members[-1]) / 2.0
        # nudge center if any member lands on DC
        if any(abs(f - center) < dc_guard for f in members):
            center += dc_guard * 2
            # if that pushes a member out of the usable half, fall back to
            # centering just below the first member
            if any(abs(f - center) > half for f in members):
                center = members[0] - dc_guard * 3
        windows.append((int(round(center)), members))
        i = j
    return windows


_PWR_TAPS = {}


def channel_power_dbfs(iq, fs, offset_hz, bw):
    """Mean power (dBFS) in a `bw`-wide channel at `offset_hz` from the capture
    center. ONE measure used everywhere (sweep detection, per-channel level read,
    and the live hold level) so a squelch threshold means the same thing in all
    three — otherwise the noise floor / threshold wouldn't transfer between them."""
    _lazy_imports()
    key = (int(fs), int(bw))
    taps = _PWR_TAPS.get(key)
    if taps is None:
        taps = signal.firwin(127, max(2000.0, bw / 2.0), fs=fs)
        _PWR_TAPS[key] = taps
    n = len(iq)
    t = np.arange(n) / fs
    x = iq * np.exp(-2j * np.pi * offset_hz * t)
    x = signal.lfilter(taps, 1.0, x)[len(taps):]      # drop filter warmup
    p = float(np.mean(np.abs(x) ** 2)) if x.size else 1e-12
    return 10.0 * math.log10(p + 1e-12)


class FMDemod:
    """Stateful streaming narrow-FM demodulator.

    Designed for gapless block-by-block processing of a CONTINUOUS IQ stream:
    every filter, the digital downconverter (NCO), and the FM discriminator
    carry their state across blocks, so there are no per-block discontinuities
    (the usual source of clicks/stutter). Decimation is two-stage with persistent
    FIR state; block length must be a multiple of dec1*dec2.

    Chain:  IQ @ fs  -> NCO shift channel to baseband (continuous phase)
                     -> LPF + /dec1  -> LPF(channel) + /dec2  (=> audio_rate, complex)
                     -> polar-discriminator FM demod  -> de-emphasis -> audio LPF
    """

    def __init__(self, fs=SAMPLE_RATE, audio_rate=48000, channel_offset=0.0,
                 deemphasis=750e-6, dev_hz=5000.0):
        _lazy_imports()
        self.fs = fs
        self.audio_rate = audio_rate
        self.off = float(channel_offset)
        self.dec1 = 10
        self.fs1 = fs / self.dec1                       # e.g. 240 kHz
        self.dec2 = int(round(self.fs1 / audio_rate))   # e.g. 5 -> 48 kHz
        # anti-alias / channel filters
        self.t1 = signal.firwin(129, 100_000, fs=fs)            # stage-1 anti-alias
        self.t2 = signal.firwin(129, 11_000, fs=self.fs1)       # channel select (~±11 kHz)
        self.ta = signal.firwin(129, 3_400, fs=audio_rate)      # audio band
        self.z1 = np.zeros(len(self.t1) - 1, dtype=np.complex128)
        self.z2 = np.zeros(len(self.t2) - 1, dtype=np.complex128)
        self.za = np.zeros(len(self.ta) - 1)
        a = math.exp(-1.0 / (audio_rate * deemphasis))          # de-emphasis IIR
        self.de_b, self.de_a = [1 - a], [1.0, -a]
        self.zde = np.zeros(1)
        self.phase = 0.0                                        # NCO phase accumulator
        self.last = 0j                                         # last IQ sample (discriminator)
        self.gain = audio_rate / (2 * np.pi * dev_hz)          # normalise deviation -> ~unity
        self.volume = 1.0                                       # linear audio volume
        self.last_power_dbfs = -120.0                          # channel power of last block

    def reset(self):
        self.z1[:] = 0
        self.z2[:] = 0
        self.za[:] = 0
        self.zde[:] = 0
        self.phase = 0.0
        self.last = 0j
        self.last_power_dbfs = -120.0

    def process(self, iq):
        n = len(iq)
        # continuous NCO: phase carries across blocks (no boundary click)
        inc = -2.0 * np.pi * self.off / self.fs
        ph = self.phase + inc * np.arange(1, n + 1)
        x = iq * np.exp(1j * ph)
        self.phase = float(ph[-1] % (2 * np.pi))
        # stage 1: LPF + decimate (persistent filter state, aligned decimation)
        x, self.z1 = signal.lfilter(self.t1, 1.0, x, zi=self.z1)
        x = x[::self.dec1]
        # stage 2: channel LPF + decimate
        x, self.z2 = signal.lfilter(self.t2, 1.0, x, zi=self.z2)
        x = x[::self.dec2]
        # channel power (same scale whether scanning or listening)
        if x.size:
            self.last_power_dbfs = 10.0 * math.log10(
                float(np.mean(np.abs(x) ** 2)) + 1e-12)
        # FM discriminator with carried last sample (continuity at block edge)
        xx = np.empty(len(x) + 1, dtype=np.complex128)
        xx[0] = self.last
        xx[1:] = x
        self.last = x[-1] if len(x) else self.last
        d = np.angle(xx[1:] * np.conj(xx[:-1]))
        # de-emphasis + audio band-limit (both stateful)
        d, self.zde = signal.lfilter(self.de_b, self.de_a, d, zi=self.zde)
        d, self.za = signal.lfilter(self.ta, 1.0, d, zi=self.za)
        return np.clip(d * self.gain * self.volume, -1.0, 1.0).astype(np.float32)


class RtlBackend:
    def __init__(self, sample_rate=SAMPLE_RATE, gain=40.0, ppm=0,
                 sweep_nsamp=1 << 16):
        _lazy_imports()
        self.sample_rate = sample_rate
        self.gain = gain
        self.ppm = ppm
        self.sweep_nsamp = sweep_nsamp     # samples per window capture
        self.sdr = None
        self.lock = threading.Lock()
        self._audio_stop = threading.Event()
        self._audio_thread = None
        self._streaming = threading.Event()   # set while the async reader owns the device
        # --- engine-facing (Backend interface, mirrors GqrxClient) state ---
        self.recommended_settle_ms = 30     # RTL retunes fast; no GQRX meter lag
        self.channel_bw = 10000
        self.strength_nsamp = 32768         # ~14 ms capture for a level read
        self.volume_db = 0.0
        self._cur_freq = 462_000_000
        self._threshold = -50.0
        self._playing = False
        self._live_power = -120.0
        self._audio_live = False            # has the audio stream produced a block?
        self._hold_t0 = 0.0
        self._meas = None                   # cached FMDemod for level reads

    # ---- device lifecycle ----
    def connect(self):
        with self.lock:
            self.sdr = RtlSdr()
            self.sdr.sample_rate = self.sample_rate
            if self.ppm:
                self.sdr.freq_correction = self.ppm
            self.sdr.gain = self.gain            # fixed gain => stable dBFS
            # prime the tuner
            self.sdr.center_freq = 462_000_000
            self.sdr.read_samples(2048)

    def close(self):
        self.stop_audio()
        with self.lock:
            if self.sdr is not None:
                try:
                    self.sdr.close()
                finally:
                    self.sdr = None

    @property
    def connected(self):
        return self.sdr is not None

    # ---- Phase 1: channelized power sweep ----
    def _capture(self, center_hz):
        with self.lock:
            self.sdr.center_freq = int(center_hz)
            return self.sdr.read_samples(self.sweep_nsamp)

    def capture_iq(self, center_hz, nsamp=None):
        """Public, lock-guarded raw-IQ capture for an arbitrary window.

        Used by the heatmap range-sweep (heatmap.py), which needs the full IQ
        block for a window rather than per-channel power. `nsamp` overrides the
        per-window sample count for this one capture; defaults to sweep_nsamp."""
        if nsamp is None:
            return self._capture(center_hz)
        with self.lock:
            self.sdr.center_freq = int(center_hz)
            return self.sdr.read_samples(int(nsamp))

    def _channel_power_dbfs(self, iq, center_hz, channel_hz, bw):
        """Power (dBFS-ish) in a `bw`-wide band offset (channel-center) from the
        capture center, via a Welch periodogram of the IQ block."""
        fs = self.sample_rate
        nperseg = 4096
        f, pxx = signal.welch(iq, fs=fs, nperseg=nperseg, return_onesided=False,
                              detrend=False, scaling="spectrum")
        f = np.fft.fftshift(f)
        pxx = np.fft.fftshift(pxx)
        off = channel_hz - center_hz
        sel = np.abs(f - off) <= (bw / 2.0)
        if not sel.any():                       # nearest bin fallback
            sel = np.array([np.argmin(np.abs(f - off))])
        power = float(np.sum(pxx[sel]))
        return 10.0 * math.log10(power + 1e-12)

    def sweep(self, freqs, bw=None):
        """Return {freq_hz: power_dbfs} using as few captures as bandwidth allows."""
        bw = bw or self.channel_bw
        windows = plan_windows(freqs)
        out = {}
        for center, members in windows:
            iq = self._capture(center)
            for ch in members:
                out[ch] = channel_power_dbfs(iq, self.sample_rate, ch - center, bw)
        return out, len(windows)

    # ---- Phase 2: gapless FM demod + audio on one parked channel ----
    #
    # The fix for stutter: capture and playback are DECOUPLED.
    #  * a continuous async IQ reader (read_samples_async) never lets the dongle
    #    overflow between reads (synchronous read_samples drops samples while you
    #    demodulate -> stutter);
    #  * demod is fully stateful (FMDemod) so blocks join seamlessly;
    #  * demodulated audio goes into a small ring buffer that a sounddevice
    #    CALLBACK drains at the device's own pace (so the output never underruns
    #    as long as the demod keeps up — which it easily does: ~5 ms work per
    #    21 ms block).
    AUDIO_RATE = 48000
    IQ_BLOCK = 51200             # mult of 512 (async) and of dec1*dec2=50

    def listen(self, channel_hz, seconds=None):
        import sounddevice as sd
        try:
            import queue as _queue
        except ImportError:
            import Queue as _queue

        center = int(channel_hz) + DC_GUARD * 4      # keep channel off the DC spike
        try:
            with self.lock:
                self.sdr.center_freq = center
        except Exception as e:                       # dongle glitch -> give up audio
            self._audio_err = e
            self._playing = False
            return
        demod = FMDemod(self.sample_rate, self.AUDIO_RATE,
                        channel_offset=channel_hz - center)
        demod.volume = 10.0 ** (self.volume_db / 20.0)
        aq = _queue.Queue(maxsize=32)                # ~0.7 s of audio max
        self._audio_stop.clear()
        lead = {"buf": np.zeros(0, dtype=np.float32)}

        def iq_cb(samples, _ctx):
            if self._audio_stop.is_set():
                try:
                    self.sdr.cancel_read_async()
                except Exception:
                    pass
                return
            try:
                audio = demod.process(samples)
                # live level on the SAME scale as sweep/measure (so hold-loss
                # uses the same threshold as detection)
                self._live_power = channel_power_dbfs(
                    samples, self.sample_rate, channel_hz - center,
                    self.channel_bw)
                self._audio_live = True
                aq.put_nowait(audio)
            except _queue.Full:
                pass                                  # output not draining; skip

        def audio_cb(outdata, frames, _t, _status):
            pos = 0
            while pos < frames:
                if lead["buf"].size == 0:
                    try:
                        lead["buf"] = aq.get_nowait()
                    except _queue.Empty:
                        outdata[pos:, 0] = 0.0         # underrun -> silence
                        return
                take = min(frames - pos, lead["buf"].size)
                outdata[pos:pos + take, 0] = lead["buf"][:take]
                lead["buf"] = lead["buf"][take:]
                pos += take

        # optional auto-stop after `seconds` (CLI use)
        if seconds:
            threading.Timer(seconds, self._request_stop).start()

        # The async reader runs in THIS (the audio) thread, so when the thread is
        # joined the device is guaranteed free again — no separate reader thread
        # racing the engine's sync level-reads. The output stream's callback
        # drains the ring buffer independently.
        self._streaming.set()
        stream = sd.OutputStream(samplerate=self.AUDIO_RATE, channels=1,
                                 dtype="float32", blocksize=1024, latency="high",
                                 callback=audio_cb)
        stream.start()
        try:
            self.sdr.read_samples_async(iq_cb, self.IQ_BLOCK)   # blocks until cancel
        except Exception as e:
            self._audio_err = e
        finally:
            self._streaming.clear()
            try:
                stream.stop(); stream.close()
            except Exception:
                pass
            self._playing = False

    def _request_stop(self):
        self._audio_stop.set()
        # only cancel when a stream is actually active — calling cancel_read_async
        # on an idle dongle corrupts its state and breaks the next center_freq.
        if self._streaming.is_set():
            try:
                if self.sdr is not None:
                    self.sdr.cancel_read_async()
            except Exception:
                pass

    def play_async(self, channel_hz, **kw):
        self.stop_audio()
        self._audio_stop.clear()
        self._audio_thread = threading.Thread(
            target=self.listen, args=(channel_hz,), kwargs=kw, daemon=True)
        self._audio_thread.start()

    def stop_audio(self):
        self._request_stop()
        if self._audio_thread and self._audio_thread.is_alive() \
                and threading.current_thread() is not self._audio_thread:
            self._audio_thread.join(timeout=2.0)
            self._audio_thread = None
        # make sure the device is no longer owned by an async reader before any
        # subsequent synchronous level-read
        t0 = time.time()
        while self._streaming.is_set() and time.time() - t0 < 1.0:
            time.sleep(0.01)

    # ---- Backend interface (mirrors GqrxClient so Scanner drives either) ----
    # Single-channel path: the engine tunes a channel and reads its level. On the
    # RTL this is a short capture -> channel power (no GQRX meter lag). While
    # parked (on_hold), level comes from the live audio demod so we don't fight
    # the dongle with a second capture.
    def set_mode(self, mode, bw):
        self.channel_bw = int(bw) or self.channel_bw

    def get_mode(self):
        return "FM", str(self.channel_bw)

    def set_freq(self, hz):
        self._cur_freq = int(hz)

    def get_freq(self):
        return int(self._cur_freq)

    def _measure_channel(self, freq):
        # never do a synchronous read while the async audio reader owns the device
        t0 = time.time()
        while self._streaming.is_set() and time.time() - t0 < 1.0:
            time.sleep(0.01)
        center = int(freq) + DC_GUARD * 4          # dodge the DC spike
        with self.lock:
            if self.sdr is None or self._streaming.is_set():
                return self._live_power
            self.sdr.center_freq = center
            iq = self.sdr.read_samples(self.strength_nsamp)
        return channel_power_dbfs(iq, self.sample_rate, freq - center,
                                  self.channel_bw)

    def strength(self):
        if self._playing:
            alive = (self._audio_thread is not None
                     and self._audio_thread.is_alive())
            # use the live stream level; tolerate ~1 s of priming before giving up
            if alive and (self._audio_live or time.time() - self._hold_t0 < 1.0):
                return self._live_power
            self._playing = False             # audio failed/ended -> fall back
        if not self.connected:
            return -120.0
        return self._measure_channel(self._cur_freq)

    def get_sql(self):
        return self._threshold

    def set_sql(self, dbfs):
        self._threshold = float(dbfs)         # software squelch (engine compares)

    def get_af(self):
        return self.volume_db

    def set_af(self, db):
        self.volume_db = float(db)

    def get_lna(self):
        return float(self.gain)

    def set_lna(self, db):
        self.gain = float(db)
        with self.lock:
            if self.sdr is not None:
                try:
                    self.sdr.gain = self.gain     # real tuner gain
                except Exception:
                    pass

    # audio hooks the Scanner calls (GqrxClient has neither; GQRX makes its own
    # sound). These let the engine drive RTL audio on park/resume.
    def on_hold(self, freq):
        self._audio_live = False
        self._hold_t0 = time.time()
        self._live_power = self._threshold     # seed so first hold read isn't stale
        self._playing = True
        self.play_async(int(freq))

    def on_resume(self):
        self._playing = False
        self.stop_audio()


# --------------------------------------------------------------------------
# Self-test against the real bookmark set (GQRX must be closed).
# --------------------------------------------------------------------------
def _load_bookmark_freqs():
    import os
    path = os.path.expanduser("~/.config/gqrx/bookmarks.csv")
    freqs = []
    section = None
    for line in open(path):
        t = line.strip()
        if t.startswith("#"):
            section = "chans" if "Frequency" in t else section
            continue
        if section == "chans" and ";" in t:
            try:
                freqs.append(int(t.split(";")[0]))
            except ValueError:
                pass
    return sorted(set(freqs))


def main(argv):
    if "--listen" in argv:
        idx = argv.index("--listen")
        freq = int(argv[idx + 1])
        secs = float(argv[idx + 2]) if len(argv) > idx + 2 else 8.0
        be = RtlBackend()
        be.connect()
        print(f"Listening {freq/1e6:.4f} MHz for {secs:.0f}s … (Ctrl-C to stop)")
        try:
            be.listen(freq, seconds=secs)
        finally:
            be.close()
        return

    freqs = _load_bookmark_freqs()
    windows = plan_windows(freqs)
    print(f"{len(freqs)} unique channels -> {len(windows)} capture windows")
    be = RtlBackend()
    be.connect()
    try:
        t0 = time.time()
        powers, nwin = be.sweep(freqs)
        dt = time.time() - t0
        print(f"\nFULL SWEEP: {len(freqs)} channels in {nwin} captures, "
              f"{dt*1000:.0f} ms  ({dt/len(freqs)*1000:.1f} ms/channel, "
              f"{len(freqs)/dt:.0f} ch/s)")
        floor = sorted(powers.values())[len(powers) // 2]
        print(f"median floor ~{floor:.1f} dBFS;  hottest channels:")
        for f, p in sorted(powers.items(), key=lambda kv: -kv[1])[:8]:
            bar = "#" * max(0, int((p - floor) / 1.5))
            print(f"  {f/1e6:9.4f} MHz  {p:7.1f} dBFS  {bar}")
    finally:
        be.close()


if __name__ == "__main__":
    main(sys.argv[1:])
