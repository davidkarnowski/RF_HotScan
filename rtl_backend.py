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

    def sweep(self, freqs, bw=16000):
        """Return {freq_hz: power_dbfs} using as few captures as bandwidth allows."""
        windows = plan_windows(freqs)
        out = {}
        for center, members in windows:
            iq = self._capture(center)
            for ch in members:
                out[ch] = self._channel_power_dbfs(iq, center, ch, bw)
        return out, len(windows)

    # ---- Phase 2: FM demod + audio on one parked channel ----
    def _fm_demod(self, iq, center_hz, channel_hz, audio_rate=48000,
                  ch_bw=16000):
        fs = self.sample_rate
        n = len(iq)
        t = np.arange(n) / fs
        # digital downconvert the wanted channel to baseband
        x = iq * np.exp(-2j * np.pi * (channel_hz - center_hz) * t)
        # decimate to an intermediate rate a few x the channel bandwidth
        dec1 = max(1, int(fs // (audio_rate * 5)))
        x = signal.decimate(x, dec1, ftype="fir")
        fs1 = fs / dec1
        # polar-discriminator FM demod (instantaneous frequency)
        d = np.angle(x[1:] * np.conj(x[:-1]))
        # de-emphasis (~750 us one-pole IIR for NBFM voice)
        tau = 750e-6
        a = math.exp(-1.0 / (fs1 * tau))
        d = signal.lfilter([1 - a], [1, -a], d)
        # decimate to audio rate + light AGC normalize
        dec2 = max(1, int(fs1 // audio_rate))
        audio = signal.decimate(d, dec2, ftype="fir").astype(np.float32)
        peak = np.max(np.abs(audio)) or 1.0
        return audio * (0.3 / peak)

    def listen(self, channel_hz, seconds=None, audio_rate=48000, ch_bw=16000):
        """Continuously demod + play one channel until stop_audio() / timeout."""
        import sounddevice as sd
        center = int(channel_hz)            # park the channel near center (offset to dodge DC)
        center += DC_GUARD * 4
        self._audio_stop.clear()
        block = 1 << 16
        with sd.OutputStream(samplerate=audio_rate, channels=1,
                             dtype="float32") as stream:
            t0 = time.time()
            while not self._audio_stop.is_set():
                if seconds and time.time() - t0 >= seconds:
                    break
                iq = self._capture(center)
                audio = self._fm_demod(iq, center, channel_hz, audio_rate, ch_bw)
                stream.write(np.ascontiguousarray(audio))

    def play_async(self, channel_hz, **kw):
        self.stop_audio()
        self._audio_thread = threading.Thread(
            target=self.listen, args=(channel_hz,), kwargs=kw, daemon=True)
        self._audio_thread.start()

    def stop_audio(self):
        self._audio_stop.set()
        if self._audio_thread and self._audio_thread.is_alive():
            self._audio_thread.join(timeout=1.0)
        self._audio_thread = None


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
