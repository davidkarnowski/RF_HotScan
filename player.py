#!/usr/bin/env python3
"""
RF HotScan — transmission playback.

Plays back recorded transmission WAVs (the ones the recorder wrote) with simple
play / pause / stop transport. Completely independent of the scanner:

  * It does NOT touch the RTL-SDR device — it only opens a sounddevice OUTPUT
    stream, so scanning/demod keep running uninterrupted.
  * It plays one file at a time on its own stream; clicking another recording
    stops the current one and starts the new one.
  * Audio goes to the OS default output device (device=None). Like the live
    scanner audio, PortAudio is re-initialised on each play() so a freshly
    plugged-in headset / changed system output is picked up.

Recordings are short (seconds), so the whole WAV is loaded into memory and a
position pointer is advanced by the audio callback — pause just stops advancing
it. Thread-safe enough for the GUI: the callback only reads/writes a couple of
plain attributes (CPython makes those assignments atomic).

    p = WavPlayer(log=print)
    p.play("recordings/…wav")   # starts playing
    p.pause()                   # toggle pause/resume
    p.stop()                    # stop + rewind
    p.state                     # 'stopped' | 'playing' | 'paused'
"""

import wave


class WavPlayer:
    def __init__(self, log=None):
        self.log = log or (lambda *_a, **_k: None)
        self._stream = None
        self._samples = None       # float32 mono ndarray of the loaded file
        self._sr = 48000
        self._pos = 0              # playback cursor (frames)
        self._paused = False
        self._state = "stopped"    # stopped | playing | paused
        self._path = None

    @property
    def state(self):
        return self._state

    @property
    def current(self):
        return self._path

    @property
    def progress(self):
        """0.0–1.0 fraction played (for a progress readout)."""
        n = 0 if self._samples is None else len(self._samples)
        return (min(1.0, self._pos / n) if n else 0.0)

    def play(self, path):
        """Load and start playing `path`. Stops any current playback first."""
        import numpy as np
        import sounddevice as sd
        self.stop()
        try:
            w = wave.open(path, "rb")
            sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
            raw = w.readframes(n)
            w.close()
            a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            if ch > 1:
                a = a.reshape(-1, ch).mean(axis=1)
        except Exception as e:
            self.log(f"Playback: cannot load {path}: {e}")
            return False
        self._samples = a
        self._sr = sr
        self._pos = 0
        self._paused = False
        self._path = path
        # Follow the OS default output device (see module docstring).
        try:
            sd._terminate()
            sd._initialize()
        except Exception:
            pass
        try:
            self._stream = sd.OutputStream(
                samplerate=sr, channels=1, dtype="float32", device=None,
                callback=self._cb, finished_callback=self._finished)
            self._state = "playing"
            self._stream.start()
        except Exception as e:
            self.log(f"Playback: cannot open output: {e}")
            self._state = "stopped"
            return False
        return True

    def _cb(self, outdata, frames, _t, _status):
        import sounddevice as sd
        if self._paused:
            outdata[:] = 0.0
            return
        a = self._samples
        if a is None:
            outdata[:] = 0.0
            raise sd.CallbackStop
        pos = self._pos
        chunk = a[pos:pos + frames]
        k = len(chunk)
        outdata[:k, 0] = chunk
        if k < frames:                      # reached the end
            outdata[k:, 0] = 0.0
            self._pos = len(a)
            raise sd.CallbackStop
        self._pos = pos + frames

    def _finished(self):
        self._state = "stopped"
        self._pos = 0
        self._paused = False

    def pause(self):
        """Toggle pause/resume (no effect when stopped)."""
        if self._state == "playing":
            self._paused = True
            self._state = "paused"
        elif self._state == "paused":
            self._paused = False
            self._state = "playing"

    def stop(self):
        st = self._stream
        self._stream = None
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:
                pass
        self._state = "stopped"
        self._pos = 0
        self._paused = False

    def close(self):
        self.stop()


def _cli(argv):
    if not argv:
        print("usage: player.py <recording.wav>")
        return 2
    import time
    p = WavPlayer(log=print)
    p.play(argv[0])
    while p.state != "stopped":
        time.sleep(0.1)
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
