# RF HotScan — Architecture

RF HotScan is organized into a clean, decoupled layers designed to separate the user interface, scanning engine, and hardware backend. While GQRX TCP remote control was the historical starting point, the architecture now places the direct RTL-SDR backend (`rtl_backend.py`) front and center, providing low-latency sweeps, demodulation, recording, and transcription.

```
┌───────────────────────────────────────────────────────────────┐
│  ScannerGUI (Tkinter, main thread)                            │
│  - builds widgets, paints state every ~120 ms via root.after  │
│  - never touches the hardware/socket directly                 │
│  - sends intents to the engine: cfg updates + action requests │
└───────────────▲───────────────────────────┬──────────────────┘
                │ snapshot_ui() (locked)     │ set_cfg(), request()
                │ logq, last_active          │ run/skip Events
┌───────────────┴───────────────────────────▼──────────────────┐
│  Scanner (engine, ONE background thread)                      │
│  - owns the main scan loop and state machine                  │
│  - state machine: STOPPED/SCANNING/HOLDING/CALIBRATING        │
│  - processes an action queue (noise_floor, listen_freq, ...)  │
└───────────────────────────────┬──────────────────────────────┘
                                │ Calls backend interfaces:
                                │ connect(), sweep(), set_freq(), ...
                                │
        ┌───────────────────────┴───────────────────────┐
        ▼                                               ▼
┌──────────────────────────────┐                ┌──────────────────────────────┐
│ RtlBackend (Primary Direct)   │                │ GqrxClient (Legacy TCP)      │
│ - Direct USB (librtlsdr)     │                │ - thin rigctl/TCP wrapper    │
│ - Fast Sweeping & FFTs       │                │ - Serialized commands        │
│ - Demodulation & Audio Out   │                │ - GQRX handles audio out     │
│ - Hooks to Recorder & STT    │                │                              │
└──────────────────────────────┘                └──────────────────────────────┘
```

---

## 1. Bookmark Parsing & Channel Tracking

- `load_bookmarks(path)` → `(tags, channels)`.
  - `tags`: `{tag_name: "#rrggbb"}`.
  - `channels`: list of dicts `{freq (Hz, int), name, mode ("FM"/"AM"/"WFM"), bw (Hz), tag}`.
  - `map_mode()` collapses bookmark modulation labels to standardized backend mode tokens.
- `cluster_bands(channels, gap=5 MHz)` → list of `(lo_hz, hi_hz)` band spans, formed by splitting the sorted frequency list wherever the gap exceeds 5 MHz. Used to scope noise-floor sampling and to add extra hardware settle time on big frequency jumps.
- `band_index(freq, bands)` → identifies which band a frequency belongs to.
- `luminance()` / `contrast_fg()` select contrasting black/white foreground text for tag labels.

The GUI assigns each channel a stable **`cid`** (its index) right after loading.
**Tree rows and per-channel state are keyed by `cid`, never by frequency**, because duplicate-frequency bookmarks exist. Keying by frequency silently collides; using `cid` preserves correct tracking.

---

## 2. Hardware Backends

The engine and user interface speak to the hardware through a unified backend interface. This consists of methods like `connect/close/connected`, `set_mode/get_mode`, `set_freq/get_freq`, `strength`, `get_sql/set_sql`, `get_af/set_af`, and `get_lna/set_lna`.

### RtlBackend (Direct SDR)
- Communicates directly with the RTL-SDR dongle via `pyrtlsdr`.
- Implements `sweep()`, enabling `Scanner._sweep_pass` to sweep a whole band in a single pass at ~35–77 channels per second by capturing and FFT-processing wideband IQ samples.
- Implements `on_hold()` and `on_resume()` to run real-time Narrowband FM (NBFM) demodulation via `FMDemod` and play it back locally through `sounddevice`.
- Includes safety ownership locks (`_acquire_dongle`) to prevent conflicts with the Heatmap or other processes.

### GqrxClient (Legacy/Fallback)
- A minimal wrapper over a TCP socket speaking GQRX's rigctl line protocol.
- Each method sends one command and reads the reply lines. A `threading.Lock` serializes access.
- In GQRX mode, audio is handled entirely by GQRX itself. No local demodulation, recording, or fast sweeps are available.

---

## 3. Scanner (Engine Thread)

One daemon thread runs the scanner `_loop()`. All backend communications happen on this thread.

### Config & State Isolation
- `self.cfg` — a dictionary guarded by `self.lock`, mutated via `set_cfg(**kw)` and read via `get_cfg(key)`. Keys include: `enabled_tags`, `lockout`, `disabled_cids`, `priority_freqs`, `squelch_mode`, `global_sql`, `auto_margin`, `settle_ms`, `hold_s`, and `priority_interval`.
- `self.ui` — a dictionary guarded by the same lock; the GUI reads it via `snapshot_ui()`.
- `self.run` / `self.skip` — `threading.Event`s for start/pause and skipping active holds.
- `self.logq` — transfers engine activity events to the GUI's log pane and the file logger.

### The Scan Loop
For each active channel:
1. `_tune(ch)` — tunes to the frequency and sets the mode if changed. In direct RTL mode, it applies a settle delay to allow the hardware to stabilize.
2. Read `strength()`.
3. If `strength >= effective_threshold`, `_hold(ch)`.

### Auto-Noise-Floor Calibration
1. Pauses scanning and sets state to `CALIBRATING`.
2. Sweeps empty channels (bookmarks + offset margins) across all bands.
3. Calculates the **median** background noise per band.
4. Thresholds in *auto* squelch mode are calculated dynamically as `floor + auto_margin`.

---

## 4. Recorder & STT Subsystems

### Silence-Trimmed Recorder (`recorder.py`)
In direct RTL mode, when a channel hold triggers:
- `WavRecorder` writes transmission audio (48 kHz/16-bit mono) directly to `./recordings/`.
- The recorder gates on the squelch status: it only writes samples while the squelch is open, yielding clean cuts with no static tails.
- Metadata is stored in a SQLite database (`recordings.sqlite`) and appended to `recordings.events.jsonl`.

### Speech-to-Text Transcription (`stt.py`)
- Off-thread worker (`TranscriptionService`) listens to new WAV completions.
- Runs local MLX-based models (Parakeet-MLX default, Whisper-MLX, or Voxtral-mlx) or cloud OpenAI API models.
- Returns transcripts to the GUI to populate the interactive scrollable transcript history pane.

---

## 5. Heatmap Tab (`heatmap.py`)

A dedicated notebook tab hosting a spectrum heatmap visualization. It operates independently of the channel bookmarks:
- Captures IQ buffers directly from `RtlBackend` using Welch Power Spectral Density (PSD) calculation.
- Stitching algorithms align tiled ~2 MHz windows into a full activity span.
- Persists power data down to SQLite (`heatmap.sqlite`).
- Uses custom fast Tkinter rendering for live waterfalls and matplotlib for offline high-fidelity PNG export.

---

## Invariants to Preserve

1. **Only the engine thread performs backend/socket operations.** The GUI must send events or requests.
2. **Key per-channel state by `cid`, never by frequency.** Duplicate frequencies exist in bookmark lists.
3. **The scan thread must never die.** Catch and log all hardware/unexpected errors within the loop.
4. **Shared-dongle coordination.** `RtlBackend` must be locked before acquisition; coordinate scanner pause during heatmap sweeps using `SdrShareCoordinator`.
