# Phase 2: Context-Based STT Healing & Agentic STT

**Status:** Planned
**Focus:** AI/Backend (LLM integration)

This phase introduces an AI layer to clean up, correct, and deduce transcriptions based on radio channel context. It also introduces an optional agentic fallback where a second STT model is consulted if the first transcription is poor. This phase operates on top of the DB schema introduced in Phase 1.

## 1. Create `healer.py`

Create a new file `healer.py` in the root of the project to abstract LLM calls.

### File: `healer.py` (New File)
**Instructions:**
Implement the following structure:
```python
import os
import json
import urllib.request
import urllib.error

class HealerProvider:
    name = "base"
    
    def available(self): return False
    def heal(self, text, context, second_text=None):
        raise NotImplementedError

class OllamaHealerProvider(HealerProvider):
    name = "ollama"
    def available(self): return True # Assuming local ollama is running
    
    def heal(self, text, context, second_text=None):
        prompt = self._build_prompt(text, context, second_text)
        req = urllib.request.Request("http://localhost:11434/api/generate", 
                                     data=json.dumps({"model": "llama3", "prompt": prompt, "stream": False}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result.get("response", text).strip()
        except Exception:
            return text

    def _build_prompt(self, text, context, second_text):
        channel = context.get('name', 'Unknown Channel')
        tag = context.get('tag', 'Unknown Tag')
        if second_text:
            return f"You are an expert radio transcription editor. Model A heard: '{text}'. Model B heard: '{second_text}'. Context: Channel '{channel}' with tag '{tag}'. Deduce the actual transmission and output ONLY the corrected text."
        else:
            return f"You are an expert radio transcription editor. The raw transcription is: '{text}'. Context: Channel '{channel}' with tag '{tag}'. Correct any obvious speech-to-text errors and output ONLY the corrected text."

class OpenAIHealerProvider(HealerProvider):
    name = "openai"
    def available(self): return bool(os.environ.get("OPENAI_API_KEY"))
    # Implement heal similarly using the openai python package.

def make_healer(name):
    if name == "ollama": return OllamaHealerProvider()
    if name == "openai": return OpenAIHealerProvider()
    return None
```

## 2. Update `stt.py`

Integrate `healer.py` into the transcription pipeline.

### File: `stt.py`
**Diff / Instructions:**
1. Import `healer`.
```python
try:
    import healer
except ImportError:
    healer = None
```
2. Modify `TranscriptionService.__init__` to accept healing config:
```diff
--- a/stt.py
+++ b/stt.py
@@ -438,7 +438,7 @@
     Decoupled from the audio/scan threads; never blocks them.
     """
 
-    def __init__(self, provider, db, log=None):
+    def __init__(self, provider, db, log=None, cfg=None):
         self.provider = provider
         self.db = db
         self.log = log or (lambda *_a: None)
+        self.cfg = cfg or {}
```
3. Update `_do(self, job)` to execute healing.
```python
# After self.db.set_transcript(...) of the raw text:
if self.cfg.get("enable_healing") and healer:
    healer_prov = healer.make_healer(self.cfg.get("healer_engine", "ollama"))
    if healer_prov and healer_prov.available():
        context = {"name": job.get("name"), "tag": job.get("tag")}
        
        second_text = None
        # Optional Agentic Fallback check
        if self.cfg.get("agentic_fallback") and (is_junk(text) or len(text.split()) < 3):
            # Example logic: Load a fallback provider like whisper-mlx
            fallback_prov = make_provider("whisper-mlx")
            if fallback_prov and fallback_prov.available():
                fallback_prov.warm_up()
                second_text = fallback_prov.transcribe(audio, TARGET_SR, wav_path=job["wav_path"]).strip()

        healed_text = healer_prov.heal(text, context, second_text=second_text)
        self.db.set_transcript(job["rec_id"], healed_transcript=healed_text, healed_by_engine=healer_prov.name, healed_at=time.time())
        # Emit updated line if necessary
```

## 3. Expose Configuration to UI (`rf_hotscan.py`)

Add configuration variables and UI elements so the user can turn this on or off.

### File: `rf_hotscan.py`
**Diff / Instructions:**
1. Update scanner default config.
```diff
--- a/rf_hotscan.py
+++ b/rf_hotscan.py
@@ -398,6 +398,9 @@
             "stt_engine": "auto",   # provider name: parakeet-mlx | whisper-mlx | openai
             "stt_model": "",        # provider-specific model id ("" = provider default)
+            "enable_healing": False,
+            "healer_engine": "ollama",
+            "agentic_fallback": False,
             "priority_interval": 6.0,
```
2. Pass `self.cfg` to `TranscriptionService`.
```diff
# Inside _handle_record (or wherever TranscriptionService is spawned)
- self.stt_worker = stt.TranscriptionService(p, self.rec_db, log=self.log)
+ self.stt_worker = stt.TranscriptionService(p, self.rec_db, log=self.log, cfg=self.cfg)
```
3. Add UI toggles to `ScannerSettings` window for the three new config keys.
