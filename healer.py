import os
import json
import urllib.request
import urllib.error
import stt

# Load .env so OpenAI API key is populated
stt._load_dotenv()

class HealerProvider:
    name = "base"
    last_error = None      # str set by heal() on failure; None on success

    def available(self):
        return False

    def heal(self, text, context, second_text=None):
        raise NotImplementedError

    def _build_prompt(self, text, context, second_text):
        channel = context.get('name', 'Unknown Channel')
        tag = context.get('tag', 'Unknown Tag')
        desc = context.get('desc', '')
        
        ctx_str = f"Channel '{channel}' with tag '{tag}'."
        if desc:
            ctx_str += f" Additional Context: {desc}"

        if second_text:
            return f"You are an expert radio transcription editor. Model A heard: '{text}'. Model B heard: '{second_text}'. Context: {ctx_str}. Deduce the actual transmission and output ONLY the corrected text."
        else:
            return f"You are an expert radio transcription editor. The raw transcription is: '{text}'. Context: {ctx_str}. Correct any obvious speech-to-text errors and output ONLY the corrected text. Do not add any conversational text."

class OllamaHealerProvider(HealerProvider):
    name = "ollama"
    
    def __init__(self, model="qwen2.5:1.5b-instruct"):
        self.model = model or "qwen2.5:1.5b-instruct"
        
    def available(self):
        try:
            with urllib.request.urlopen("http://localhost:11434/api/tags",
                                        timeout=1):
                return True
        except Exception:
            return False

    def heal(self, text, context, second_text=None):
        self.last_error = None
        prompt = self._build_prompt(text, context, second_text)
        req = urllib.request.Request("http://localhost:11434/api/generate",
                                     data=json.dumps({"model": self.model, "prompt": prompt, "stream": False}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result.get("response", text).strip()
        except Exception as e:
            self.last_error = str(e)
            return text



class OpenAIHealerProvider(HealerProvider):
    name = "openai"
    
    def __init__(self, model="gpt-4o-mini"):
        self.model = model or "gpt-4o-mini"
        
    def available(self):
        return bool(os.environ.get("OPENAI_API_KEY"))
    
    def heal(self, text, context, second_text=None):
        self.last_error = None
        prompt = self._build_prompt(text, context, second_text)
        req = urllib.request.Request("https://api.openai.com/v1/chat/completions",
                                     data=json.dumps({
                                         "model": self.model,
                                         "messages": [{"role": "user", "content": prompt}],
                                         "temperature": 0.2
                                     }).encode("utf-8"),
                                     headers={
                                         "Content-Type": "application/json",
                                         "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY')}"
                                     })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result["choices"][0]["message"]["content"].strip()
        except Exception as e:
            self.last_error = str(e)
            return text
def make_healer(name, model=""):
    if name == "ollama": return OllamaHealerProvider(model)
    if name == "openai": return OpenAIHealerProvider(model)
    return None

def engine_options():
    """Currently-usable healer choices for the GUI picker. OpenAI entries are
    gated on the API key (same policy as stt.engine_options); Ollama models are
    discovered live from the local daemon."""
    opts = []
    if os.environ.get("OPENAI_API_KEY"):
        opts.append({"engine": "openai", "model": "gpt-4o-mini", "label": "OpenAI: gpt-4o-mini"})
        opts.append({"engine": "openai", "model": "gpt-4o", "label": "OpenAI: gpt-4o"})

    try:
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=1) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for m in data.get("models", []):
                name = m["name"]
                opts.append({"engine": "ollama", "model": name, "label": f"Ollama: {name}"})
    except Exception:
        pass
        
    return opts
