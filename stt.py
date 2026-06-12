#!/usr/bin/env python3
"""
RF HotScan — local speech-to-text on transmission recordings.

Transcribes the recorder's WAVs locally (no cloud). Default engine is
Parakeet-MLX (NVIDIA Parakeet TDT on Apple Silicon) — RT-factor ~0.08, English.
The `SttProvider` interface keeps the engine swappable: add a Whisper provider
later with zero call-site changes.

The Parakeet provider is adapted from the maintainer's SpeakNoEvil project
(src/speaknoevil/stt/{base,parakeet}.py) — same model + load/warm-up approach.

Optional deps (lazy): `pip install parakeet-mlx` (pulls mlx). macOS/Linux.
Self-test:  .venv/bin/python stt.py <recording.wav>
"""

import sys
import time
import wave
import queue
import threading

import clock

PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v2"
TARGET_SR = 16000

# Whisper-style hallucination junk that ASR emits on noise/silence.
_JUNK = {"", "you", "thank you", "thank you.", "thanks for watching",
         "thanks for watching.", ".", "bye", "bye."}

_np = None


def _lazy_np():
    global _np
    if _np is None:
        import numpy as np
        _np = np
    return _np


def read_wav_mono16k(path):
    """Read a (48 kHz mono 16-bit) WAV → float32 mono at 16 kHz in [-1, 1]."""
    np = _lazy_np()
    w = wave.open(path, "rb")
    sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
    raw = w.readframes(n)
    w.close()
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if sr != TARGET_SR:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(int(sr), TARGET_SR)
        a = resample_poly(a, TARGET_SR // g, int(sr) // g)
    return a.astype(np.float32)


def is_junk(text):
    return text.strip().lower() in _JUNK


# --------------------------------------------------------------------------
# Provider interface (swappable engines)
# --------------------------------------------------------------------------
class SttProvider:
    name = "base"

    def available(self):
        """True if deps import and the model is present."""
        return False

    def ensure_ready(self):
        return False, "not implemented"

    def warm_up(self):
        pass

    def transcribe(self, audio, sample_rate):
        """audio: float32 mono ndarray. Returns transcript text."""
        raise NotImplementedError


class ParakeetMLXProvider(SttProvider):
    """Parakeet-TDT via MLX (Apple Silicon). Adapted from SpeakNoEvil."""

    name = "parakeet-mlx"

    def __init__(self, model_id=PARAKEET_MODEL):
        self.model_id = model_id
        self._model = None
        self._warmed = False

    def available(self):
        import importlib.util
        if importlib.util.find_spec("parakeet_mlx") is None:
            return False
        ok, _ = self.ensure_ready()
        return ok

    def ensure_ready(self):
        try:
            from huggingface_hub import scan_cache_dir
            repos = {r.repo_id for r in scan_cache_dir().repos}
            if self.model_id in repos:
                return True, "ok"
            return False, f"model '{self.model_id}' not in HuggingFace cache"
        except Exception as e:
            return False, f"hf cache scan failed: {e}"

    def _load(self):
        if self._model is None:
            from parakeet_mlx import from_pretrained
            self._model = from_pretrained(self.model_id)
        return self._model

    def warm_up(self):
        """Eat the one-time MLX JIT compile (~3-7 s) on a silent buffer so the
        first real clip is fast."""
        if self._warmed:
            return
        np = _lazy_np()
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel
        model = self._load()
        mel = get_logmel(mx.array(np.zeros(TARGET_SR, dtype=np.float32)),
                         model.preprocessor_config)
        res = model.generate(mel)
        if isinstance(res, list) and res:
            _ = res[0].text
        self._warmed = True

    def transcribe(self, audio, sample_rate=TARGET_SR):
        np = _lazy_np()
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel
        model = self._load()
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        mel = get_logmel(mx.array(audio), model.preprocessor_config)
        res = model.generate(mel)
        if isinstance(res, list):
            res = res[0] if res else None
        return (res.text.strip() if res is not None else "")


def make_provider(prefer="auto", model=None):
    """Return the best available provider, or None if STT deps are missing."""
    p = ParakeetMLXProvider(model or PARAKEET_MODEL)
    if p.available():
        return p
    return None


def available_providers():
    return [p.name for p in (ParakeetMLXProvider(),) if p.available()]


# --------------------------------------------------------------------------
# Transcription service — off-thread worker fed by finished recordings
# --------------------------------------------------------------------------
class TranscriptionService:
    """Background worker: WAV job -> transcript -> RecordingsDB + UI queue.

    Decoupled from the audio/scan threads; never blocks them.
    """

    def __init__(self, provider, db, log=None):
        self.provider = provider
        self.db = db
        self.log = log or (lambda *_a: None)
        self.q = queue.Queue(maxsize=64)
        self.transcriptq = queue.Queue()     # UI lines (drained by the GUI)
        self.ready = False
        self.alive = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def enqueue(self, rec):
        """Accept a recorder recording dict (from WavRecorder.on_record) or an
        equivalent job dict; normalize the fields we need."""
        job = {"rec_id": rec.get("id", rec.get("rec_id")),
               "wav_path": rec.get("wav_path"),
               "name": rec.get("name", ""), "tag": rec.get("tag", ""),
               "iso_start": rec.get("iso_start"),
               "unix_start": rec.get("unix_start"),
               "duration": rec.get("duration_s", rec.get("duration", 1.0))}
        if not job["wav_path"] or job["rec_id"] is None:
            return
        try:
            self.q.put_nowait(job)
        except queue.Full:
            self.log(f"STT queue full — dropped {job.get('name', '')}")

    def stop(self):
        self.alive = False

    def _loop(self):
        ok, detail = self.provider.ensure_ready()
        if not ok:
            self.log(f"STT unavailable: {detail}")
            return
        try:
            self.log(f"Loading STT model ({self.provider.name})…")
            self.provider.warm_up()
            self.ready = True
            self.log("STT model ready")
        except Exception as e:
            self.log(f"STT model load failed: {e}")
            return
        while self.alive:
            try:
                job = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            self._do(job)

    def _do(self, job):
        try:
            audio = read_wav_mono16k(job["wav_path"])
            t0 = time.time()
            text = self.provider.transcribe(audio, TARGET_SR).strip()
            dur = max(0.01, float(job.get("duration", len(audio) / TARGET_SR)))
            rt = (time.time() - t0) / dur
            if is_junk(text):
                self.db.set_transcript(job["rec_id"], transcript="",
                                       transcript_engine=self.provider.name,
                                       transcribed_at=clock.now_unix())
                return
            self.db.set_transcript(
                job["rec_id"], transcript=text,
                transcript_engine=self.provider.name,
                transcript_model=getattr(self.provider, "model_id", ""),
                transcript_rt=round(rt, 3), transcribed_at=clock.now_unix())
            self.transcriptq.put({"iso": job.get("iso_start"),
                                  "unix_start": job.get("unix_start"),
                                  "tag": job.get("tag", ""),
                                  "name": job.get("name", ""), "text": text, "rt": rt})
            self.log(f"STT {job.get('name', '')}: {text[:60]}")
        except Exception as e:
            self.log(f"STT failed for {job.get('name', '')}: {e}")
            try:
                self.db.set_transcript(job["rec_id"], transcript_engine="error",
                                       transcribed_at=clock.now_unix())
            except Exception:
                pass


def _cli(argv):
    if not argv:
        print("usage: stt.py <audio.wav> [...]"); return 2
    p = make_provider()
    if p is None:
        print("No STT provider available (pip install parakeet-mlx; model must be "
              "in the HuggingFace cache).")
        return 1
    print(f"provider: {p.name}", file=sys.stderr)
    p.warm_up()
    for path in argv:
        audio = read_wav_mono16k(path)
        t0 = time.time()
        text = p.transcribe(audio, TARGET_SR)
        dt = time.time() - t0
        rt = dt / max(0.01, len(audio) / TARGET_SR)
        print(f"\n[{path}]  ({dt:.2f}s, RTF {rt:.3f})\n{text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
