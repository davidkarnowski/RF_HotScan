# RF HotScan

**A high-performance direct RTL-SDR bookmark scanner, audio recorder, speech-to-text transcriber, and spectrum heatmap.**

RF HotScan is a Python-based software-defined radio (SDR) application featuring a unified dark-themed Tkinter GUI. Its primary mode of operation uses a direct, raw hardware backend to scan frequency bookmarks, detect transmissions, record clean audio cuts, transcribe them in real time using local or cloud AI models, and visualize spectrum activity. It also retains a legacy/fallback remote-control backend to drive GQRX via TCP.

---

## Architecture & Backends

RF HotScan is built around a decoupled architecture where the scanner GUI and the scanner engine are hardware-agnostic. The application supports two interchangeable backends:

1. **Direct RTL-SDR (Primary / Recommended)**
   - Communicates directly with the RTL-SDR dongle via `rtl_backend.py` using `pyrtlsdr` and `librtlsdr`.
   - Performs a fast, channelized FFT sweep (~35–77 channels per second).
   - Handles real-time Narrowband FM (NBFM) demodulation and gapless local audio playback in Python.
   - Enables all recording and speech-to-text (STT) capabilities.
2. **GQRX Remote (Legacy / Fallback)**
   - Drives GQRX via its rigctl-compatible TCP interface (`127.0.0.1:7356`).
   - Uses GQRX for demodulation and audio output.
   - Recording, STT, and fast FFT sweeps are disabled in GQRX mode.

---

## Core Features

- **Direct RTL-SDR Scanning**: Fast channelized sweeps with lock-on signal detection.
- **Tag-Aware Bookmark Scanner**: Organizes channels by tags (e.g., `LBPD`, `LBFD`, `HAM`, `GMRS/FRS`). Filter sweeps by toggling tag groups live in the UI.
- **Transmission Recorder & Player**: Automatically cuts recordings when squelch opens, saving clean, silence-trimmed WAV files to `./recordings/` and metadata to a SQLite database (`recordings.sqlite`).
- **Real-time Speech-to-Text**: Transcribes recordings instantly.
  - **Local Models**: Fast default [Parakeet-MLX](https://github.com/ml-explore/mlx-examples) (Parakeet TDT v2, ~35× realtime) and high-accuracy Voxtral (Mini 3B) when weights are present.
  - **Cloud Models**: OpenAI Whisper-1, GPT-4o-mini-transcribe, and GPT-4o-transcribe (requires `OPENAI_API_KEY`).
  - **Re-transcription**: A `↻` button next to transcript rows allows re-processing a clip with the currently selected model.
- **Radio Playhead**: A manual tuning interface in the banner. Type in a frequency (e.g., `146.52` or `462.5625`) or double-click any channel in the list to immediately tune and listen.
- **Auto-Noise-Floor Calibration**: Pauses scanning, sweeps the band to measure background noise, and dynamically calibrates the squelch threshold for the environment.
- **Spectrum Heatmap**: A dedicated tab (and CLI tool) that sweeps a frequency range, records power readings to SQLite (`heatmap.sqlite`), detects active signals, and renders waterfalls.
- **Robust Multi-row Filtering & Controls**: Includes draggable squelch sliders, priority channel overrides (`★`), per-channel lockouts, and panel-wide scroll support.

---

## Module Overview

| Module | Role |
| --- | --- |
| [rf_hotscan.py](file:///Users/dk/Projects/SDR/rf-hotscan/rf_hotscan.py) | Main entry point. Defines `Scanner` loop, `ScannerGUI`, and legacy `GqrxClient`. |
| [rtl_backend.py](file:///Users/dk/Projects/SDR/rf-hotscan/rtl_backend.py) | Direct SDR backend. Manages hardware, FFT sweeping, NBFM demodulation (`FMDemod`), and gapless playback. |
| [heatmap.py](file:///Users/dk/Projects/SDR/rf-hotscan/heatmap.py) | Spectrum analyzer. Drives start-to-stop sweeps, Persists power matrices, and generates waterfalls. |
| [recorder.py](file:///Users/dk/Projects/SDR/rf-hotscan/recorder.py) | Silence-trimmed `WavRecorder` (48 kHz/16-bit mono) and `RecordingsDB` (SQLite). |
| [stt.py](file:///Users/dk/Projects/SDR/rf-hotscan/stt.py) | Real-time speech-to-text service integrating local (MLX) and OpenAI models. |
| [player.py](file:///Users/dk/Projects/SDR/rf-hotscan/player.py) | Async audio player for recorded transmissions. |
| [clock.py](file:///Users/dk/Projects/SDR/rf-hotscan/clock.py) | Shared time base for matching local/UTC timestamps across the app. |

---

## Dependencies & Environment Setup

RF HotScan is tested on **macOS (Apple Silicon)** and **Linux/Ubuntu**. Windows is out of scope.

### 1. System Requirements
- **Python 3.9+** with Tkinter.
- **RTL-SDR USB Dongle** (e.g., RTL-SDR Blog V3 or V4).
- **Native Drivers**: `librtlsdr` must be installed:
  - macOS: `brew install rtl-sdr`
  - Ubuntu/Debian: `sudo apt install rtl-sdr librtlsdr-dev`

### 2. Virtual Environment Setup
Run the environment setup script to configure a virtual environment with the direct backend requirements:
```bash
chmod +x setup_rtl_env.sh
./setup_rtl_env.sh
```

Or install manually:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-rtl.txt
```
To enable speech-to-text, also install:
```bash
pip install -r requirements-stt.txt
```

### 3. API Keys & Models (Optional)
Create a `.env` file in the project root (never commit `.env` to Git):
```env
OPENAI_API_KEY=your-openai-api-key
HF_TOKEN=your-huggingface-token-if-needed
```
Local models (Parakeet-MLX, Voxtral, etc.) are downloaded automatically to the Hugging Face cache folder upon first use.

---

## Running the Application

Always run the application from the project root using the virtual environment's Python interpreter:

```bash
# Main GUI app (Direct RTL-SDR backend selected by default)
.venv/bin/python rf_hotscan.py
```

### Legacy GQRX Fallback Mode
If you wish to run the legacy GQRX mode:
1. Start GQRX and enable Remote Control (Tools -> Remote control).
2. Start the application:
   ```bash
   .venv/bin/python rf_hotscan.py
   ```
3. Swap the `BACKEND` setting to **GQRX** in the GUI.

---

## Heatmap CLI Usage

The heatmap module can be executed headlessly without launching the GUI. This is useful for scripting and background tasks.

```bash
# Sweep FM Broadcast band (88-108 MHz) for 10 seconds and return JSON results
.venv/bin/python -m heatmap scan --start 88e6 --stop 108e6 --duration 10 --device 0 --gain auto --json

# List past database scan sessions
.venv/bin/python -m heatmap list

# Export a session rendering to a PNG image
.venv/bin/python -m heatmap render <session_id> --png output.png
```

*Note: For headless test runs or continuous integration, passing `--device fake` runs a simulated spectrum capture without needing an RTL-SDR dongle connected.*

---

## Runtime Files

- `./scanner.log`: Main application log (crucial for troubleshooting).
- `./scanner_settings.json`: Persisted GUI options, lockouts, disabled channels, and slider levels.
- `./recordings/`: Location of recorded WAV clips, metadata database `recordings.sqlite`, and `recordings.events.jsonl` event log.
- `./heatmap.sqlite`: Power matrices, sessions, and activity records for the Heatmap.
- `./heatmap.log` & `./heatmap.events.jsonl`: Event logs for spectrum analyzer operations.
- `~/.config/gqrx/bookmarks.csv`: Legacy bookmark file (if GQRX is present; falls back to `./bookmarks.csv` locally).

---

## License & Copyleft Note

RF HotScan is distributed as open source. However, be aware of the dependencies:
- Core scanning logic (`rf_hotscan.py`, `clock.py`) uses Python standard library modules.
- The optional direct RTL-SDR driver layer uses `pyrtlsdr` (GPL-3.0) and `librtlsdr` (GPL-2.0-or-later).
- Distributing a combined binary work incorporating the RTL-SDR backend triggers GPL copyleft requirements for the combined package. Source-only distributions remain unconstrained.
