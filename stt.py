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

import os
import sys
import time
import wave
import queue
import threading

import clock


def _load_dotenv():
    """Load KEY=VALUE lines from a .env next to this module into os.environ
    (real environment wins). Lets OPENAI_API_KEY / HF_TOKEN live in a project
    .env without exporting them globally. No dependency on python-dotenv."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass
    # Be lenient about the OpenAI key's name: the SDK only reads OPENAI_API_KEY,
    # but people write OpenAI_Key / OPENAI_KEY / OPENAI_TOKEN. Map any such alias.
    if not os.environ.get("OPENAI_API_KEY"):
        for k, v in list(os.environ.items()):
            kl = k.lower()
            if "openai" in kl and ("key" in kl or "token" in kl) and v.strip():
                os.environ["OPENAI_API_KEY"] = v.strip()
                break


_load_dotenv()

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
    # Local engines consume the decoded float32 array; cloud engines upload the
    # WAV file directly (and skip the 48k->16k resample). The service reads this
    # to decide whether to bother decoding the WAV.
    wants_audio = True
    cloud = False

    def available(self):
        """True if deps import and the model/credentials are present."""
        return False

    def ensure_ready(self):
        return False, "not implemented"

    def warm_up(self):
        pass

    def transcribe(self, audio, sample_rate, wav_path=None):
        """Return transcript text. Local providers use `audio` (float32 mono
        ndarray); cloud providers upload `wav_path`."""
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

    def transcribe(self, audio, sample_rate=TARGET_SR, wav_path=None):
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


# Default OpenAI transcription model. "gpt-4o-mini-transcribe" is cheap + strong;
# "gpt-4o-transcribe" is most accurate; "whisper-1" is the classic fallback.
OPENAI_MODEL = "gpt-4o-mini-transcribe"


class OpenAIProvider(SttProvider):
    """Cloud STT via OpenAI's audio transcription API. Uploads the WAV directly.

    Requires `pip install openai` and OPENAI_API_KEY in the environment.
    NOTE: this sends recorded audio to OpenAI's servers — not local/offline.
    """

    name = "openai"
    wants_audio = False
    cloud = True

    def __init__(self, model=OPENAI_MODEL):
        self.model = model
        self.model_id = model
        self._client = None

    def available(self):
        import importlib.util
        import os
        return (importlib.util.find_spec("openai") is not None
                and bool(os.environ.get("OPENAI_API_KEY")))

    def ensure_ready(self):
        import importlib.util
        import os
        if importlib.util.find_spec("openai") is None:
            return False, "openai package not installed (pip install openai)"
        if not os.environ.get("OPENAI_API_KEY"):
            return False, "OPENAI_API_KEY not set in environment"
        return True, "ok"

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI()
        return self._client

    def warm_up(self):
        # No model to load locally; just construct the client so the first real
        # job doesn't pay import latency.
        self._get_client()

    def transcribe(self, audio, sample_rate=TARGET_SR, wav_path=None):
        if not wav_path:
            raise ValueError("OpenAIProvider needs a wav_path to upload")
        client = self._get_client()
        with open(wav_path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model=self.model, file=f, response_format="text")
        # response_format="text" → resp is a str; otherwise an object with .text
        text = resp if isinstance(resp, str) else getattr(resp, "text", "")
        return (text or "").strip()


# Local Whisper models (MLX). Only those actually present in the HF cache are
# offered by engine_options(); the download is a setup step (requirements-stt).
WHISPER_MLX_MODELS = [
    ("mlx-community/whisper-large-v3-turbo", "large-v3-turbo"),
    ("mlx-community/whisper-medium-mlx", "medium"),
    ("mlx-community/whisper-small-mlx", "small"),
]
DEFAULT_WHISPER_MLX = WHISPER_MLX_MODELS[0][0]


class MLXWhisperProvider(SttProvider):
    """OpenAI Whisper running locally on Apple MLX (mlx-whisper). Offline.

    Consumes the decoded float32 16 kHz array (so we don't depend on ffmpeg,
    which mlx_whisper.load_audio would otherwise shell out to for file paths).
    """

    name = "whisper-mlx"
    wants_audio = True

    def __init__(self, model_id=DEFAULT_WHISPER_MLX):
        self.model_id = model_id
        self._warmed = False

    def available(self):
        import importlib.util
        if importlib.util.find_spec("mlx_whisper") is None:
            return False
        ok, _ = self.ensure_ready()
        return ok

    def ensure_ready(self):
        # A repo dir can exist with only config.json while the weights are still
        # an interrupted .incomplete download — that hangs transcribe(). So
        # require a committed weights file, not just the repo's presence.
        try:
            from huggingface_hub import scan_cache_dir
            for r in scan_cache_dir().repos:
                if r.repo_id != self.model_id:
                    continue
                names = [f.file_name for rev in r.revisions for f in rev.files]
                if any(n.endswith((".safetensors", ".npz")) for n in names):
                    return True, "ok"
                return False, (f"model '{self.model_id}' present but weights not "
                               "fully downloaded")
            return False, f"model '{self.model_id}' not in HuggingFace cache"
        except Exception as e:
            return False, f"hf cache scan failed: {e}"

    def warm_up(self):
        if self._warmed:
            return
        np = _lazy_np()
        import mlx_whisper
        mlx_whisper.transcribe(np.zeros(TARGET_SR, dtype=np.float32),
                               path_or_hf_repo=self.model_id)
        self._warmed = True

    def transcribe(self, audio, sample_rate=TARGET_SR, wav_path=None):
        np = _lazy_np()
        import mlx_whisper
        if audio is None:                         # safety: decode if not given
            audio = read_wav_mono16k(wav_path)
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        res = mlx_whisper.transcribe(audio, path_or_hf_repo=self.model_id)
        return (res.get("text", "") if isinstance(res, dict) else "").strip()


DEFAULT_VOXTRAL = "mzbac/voxtral-mini-3b-4bit-mixed"


class VoxtralMLXProvider(SttProvider):
    """Mistral Voxtral Mini (audio LLM) on Apple MLX, in pure transcription mode.
    Offline. Heavier + slower than Parakeet (~1x realtime) but more accurate; only
    listed when its weights are already in the HuggingFace cache."""

    name = "voxtral"
    wants_audio = False           # the processor reads the WAV path itself

    def __init__(self, model_id=DEFAULT_VOXTRAL):
        self.model_id = model_id
        self._model = None
        self._proc = None

    def available(self):
        import importlib.util
        if importlib.util.find_spec("mlx_voxtral") is None:
            return False
        ok, _ = self.ensure_ready()
        return ok

    def ensure_ready(self):
        try:
            from huggingface_hub import scan_cache_dir
            for r in scan_cache_dir().repos:
                if r.repo_id != self.model_id:
                    continue
                names = [f.file_name for rev in r.revisions for f in rev.files]
                if any(n.endswith((".safetensors", ".npz")) for n in names):
                    return True, "ok"
                return False, f"model '{self.model_id}' weights incomplete"
            return False, f"model '{self.model_id}' not in HuggingFace cache"
        except Exception as e:
            return False, f"hf cache scan failed: {e}"

    def warm_up(self):
        from mlx_voxtral import VoxtralForConditionalGeneration, VoxtralProcessor
        if self._model is None:
            self._model = VoxtralForConditionalGeneration.from_pretrained(self.model_id)
            self._proc = VoxtralProcessor.from_pretrained(self.model_id)

    def transcribe(self, audio, sample_rate=TARGET_SR, wav_path=None):
        if not wav_path:
            raise ValueError("VoxtralMLXProvider needs a wav_path")
        import mlx.core as mx
        self.warm_up()
        inputs = self._proc.apply_transcrition_request(language="en", audio=wav_path)
        out = self._model.generate(input_ids=inputs.input_ids,
                                   input_features=inputs.input_features,
                                   max_new_tokens=256, temperature=0.0)
        text = self._proc.decode(out[0][inputs.input_ids.shape[1]:],
                                 skip_special_tokens=True)
        mx.clear_cache()          # free the MLX buffer cache between clips (3B model)
        return (text or "").strip()


# Registry of known providers, in auto-preference order (local first).
_PROVIDERS = {
    "parakeet-mlx": lambda model=None: ParakeetMLXProvider(model or PARAKEET_MODEL),
    "whisper-mlx": lambda model=None: MLXWhisperProvider(model or DEFAULT_WHISPER_MLX),
    "voxtral": lambda model=None: VoxtralMLXProvider(model or DEFAULT_VOXTRAL),
    "openai": lambda model=None: OpenAIProvider(model or OPENAI_MODEL),
}
_AUTO_ORDER = ["parakeet-mlx", "whisper-mlx", "voxtral", "openai"]


def make_provider(prefer="auto", model=None):
    """Return a provider by name (e.g. 'openai', 'parakeet-mlx'), or the best
    available one for 'auto'. Returns None if nothing is usable."""
    if prefer and prefer != "auto":
        factory = _PROVIDERS.get(prefer)
        if factory is None:
            return None
        p = factory(model)
        return p if p.available() else None
    for name in _AUTO_ORDER:
        p = _PROVIDERS[name](model if name == prefer else None)
        if p.available():
            return p
    return None


def available_providers():
    """Names of providers whose deps + credentials/model are present."""
    return [name for name in _AUTO_ORDER if _PROVIDERS[name]().available()]


# Selectable cloud models for the OpenAI engine (label shown in the UI picker).
OPENAI_MODELS = [
    ("gpt-4o-mini-transcribe", "GPT-4o mini transcribe"),
    ("gpt-4o-transcribe", "GPT-4o transcribe"),
    ("whisper-1", "Whisper-1"),
]


def engine_options():
    """Concrete, currently-usable (label, engine, model) STT choices for the GUI
    model picker — local engines first, then cloud. Only options whose deps +
    model/credentials are present are returned."""
    opts = []
    if ParakeetMLXProvider().available():
        opts.append({"label": "Parakeet TDT · local",
                     "engine": "parakeet-mlx", "model": None})
    for mid, label in WHISPER_MLX_MODELS:
        if MLXWhisperProvider(mid).available():
            opts.append({"label": f"Whisper {label} · local",
                         "engine": "whisper-mlx", "model": mid})
    if VoxtralMLXProvider().available():
        opts.append({"label": "Voxtral Mini 3B · local",
                     "engine": "voxtral", "model": None})
    if OpenAIProvider().available():
        for mid, label in OPENAI_MODELS:
            opts.append({"label": f"{label} · OpenAI cloud",
                         "engine": "openai", "model": mid})
    return opts


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
        # Live progress for the UI: state in {loading, idle, transcribing},
        # current = name being worked on, queued = jobs waiting.
        self.status = {"state": "loading", "current": "", "queued": 0}
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
            self.status["state"] = "error"
            self.log(f"STT unavailable: {detail}")
            return
        try:
            self.status["state"] = "loading"
            self.log(f"Loading STT model ({self.provider.name})…")
            self.provider.warm_up()
            self.ready = True
            self.status["state"] = "idle"
            self.log("STT model ready")
        except Exception as e:
            self.status["state"] = "error"
            self.log(f"STT model load failed: {e}")
            return
        while self.alive:
            self.status["queued"] = self.q.qsize()
            try:
                job = self.q.get(timeout=0.5)
            except queue.Empty:
                self.status["state"] = "idle"
                continue
            self.status["state"] = "transcribing"
            self.status["current"] = job.get("name", "")
            self.status["queued"] = self.q.qsize()
            self._do(job)
            self.status["state"] = "idle"
            self.status["current"] = ""

    def _emit(self, job, text, status, rt=None):
        """Push a UI line for this transmission. status is None for real text,
        else 'no_speech' / 'error'. Keyed by wav_path so the GUI updates the
        already-listed (start-time) line in place."""
        self.transcriptq.put({"kind": "text", "key": job.get("wav_path"),
                              "iso": job.get("iso_start"),
                              "unix_start": job.get("unix_start"),
                              "tag": job.get("tag", ""),
                              "name": job.get("name", ""),
                              "text": text, "status": status, "rt": rt})

    def _do(self, job):
        try:
            audio = None
            n = 0
            if self.provider.wants_audio:
                audio = read_wav_mono16k(job["wav_path"])
                n = len(audio)
            t0 = time.time()
            text = self.provider.transcribe(
                audio, TARGET_SR, wav_path=job["wav_path"]).strip()
            dur = max(0.01, float(job.get("duration", (n / TARGET_SR) or 1.0)))
            rt = (time.time() - t0) / dur
            if is_junk(text):
                # No intelligible speech — still list it, with a status.
                self.db.set_transcript(job["rec_id"], transcript="",
                                       transcript_engine=self.provider.name,
                                       transcribed_at=clock.now_unix())
                self._emit(job, "", "no_speech", rt)
                self.log(f"STT {job.get('name', '')}: (no intelligible speech)")
                return
            self.db.set_transcript(
                job["rec_id"], transcript=text,
                transcript_engine=self.provider.name,
                transcript_model=getattr(self.provider, "model_id", ""),
                transcript_rt=round(rt, 3), transcribed_at=clock.now_unix())
            self._emit(job, text, None, rt)
            self.log(f"STT {job.get('name', '')}: {text[:60]}")
        except Exception as e:
            self.log(f"STT failed for {job.get('name', '')}: {e}")
            self._emit(job, "", "error")
            try:
                self.db.set_transcript(job["rec_id"], transcript_engine="error",
                                       transcribed_at=clock.now_unix())
            except Exception:
                pass


def _cli(argv):
    # Optional first arg: engine name (parakeet-mlx | openai). Else auto.
    prefer = "auto"
    if argv and argv[0] in _PROVIDERS:
        prefer, argv = argv[0], argv[1:]
    if not argv:
        print("usage: stt.py [parakeet-mlx|openai] <audio.wav> [...]")
        print(f"available: {available_providers()}")
        return 2
    p = make_provider(prefer)
    if p is None:
        print(f"No STT provider available for '{prefer}'. "
              f"available: {available_providers()}")
        return 1
    print(f"provider: {p.name}", file=sys.stderr)
    p.warm_up()
    for path in argv:
        audio = read_wav_mono16k(path) if p.wants_audio else None
        n = len(audio) if audio is not None else 0
        t0 = time.time()
        text = p.transcribe(audio, TARGET_SR, wav_path=path)
        dt = time.time() - t0
        rt = dt / max(0.01, (n / TARGET_SR) or 1.0)
        print(f"\n[{path}]  ({dt:.2f}s, RTF {rt:.3f})\n{text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
