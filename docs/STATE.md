# RF HotScan — Current State (2026-06-18)

A point-in-time snapshot of the application for humans and agents. For *how it's
built* see `docs/ARCHITECTURE.md`; for cross-agent conventions see
`docs/AGENTS.md`. This file is the "where are we right now" layer.

---

## 1. What it is

RF HotScan is a Python/Tkinter **scanner** for public-safety / amateur radio. It
detects active channels from a bookmark list, parks ("holds") on a transmission,
plays its audio, optionally **records** each transmission to WAV, and optionally
**transcribes** recordings with local or cloud speech-to-text. There is a live
transcript pane with per-transmission playback and **re-transcription** (select a
different STT engine + click ↻ to re-run).

Two interchangeable **backends** drive the radio:
- **RTL-SDR direct (default)** — `rtl_backend.py` owns the dongle: channelized
  FFT sweep (~35–77 ch/s), NBFM demod + gapless audio. **Now with audio fix:**
  100ms tuner PLL lock wait before reading samples (was missing, causing "PLL
  not locked" warnings and garbled audio).
- **GQRX remote** — `GqrxClient` in `rf_hotscan.py` over rigctl TCP 127.0.0.1:7356.
  Kept as an option; produces no audio samples (so recording/STT are RTL-only).

Target platforms: **macOS (Apple Silicon) primary, Linux/Ubuntu secondary.
Windows is explicitly out of scope.**

---

## 2. Git status (right now)

- Branch: **`direct-sdr-backend`** (the direct-RTL line of work; not yet merged).
- Last commit: **`0325014`** — "feat: add LA County Sheriff Department (LASD)
  dispatch, tactical, and special ops channels".
- **Working tree: CLEAN** (all changes committed).

### Recent commits (last 5, newest first)
| Hash | Message |
|---|---|
| `0325014` | feat: add LA County Sheriff Department (LASD) dispatch, tactical, and special ops channels |
| `657f761` | fix: RTL audio playback by waiting for tuner PLL lock + heatmap dtype error |
| `6ff8c38` | fix: unmute audio when double-clicking to tune a station |
| `94bffb2` | feat: add NOAA Weather Radio (NWS) bookmarks for Southern California |
| `4169de1` | refactor: wrap tag filter buttons to multiple rows |

---

## 3. How to run

```bash
# from the project root
.venv/bin/python rf_hotscan.py
```
- Core scanner/GQRX path is dependency-light. The **RTL backend** needs
  `requirements-rtl.txt` (numpy/scipy/sounddevice/pyrtlsdr); **STT** needs
  `requirements-stt.txt` (parakeet-mlx; optionally mlx-whisper, mlx-voxtral,
  vosk, openai). All STT deps are optional and lazily imported.
- macOS needs Tk: `brew install python-tk@3.14` (Homebrew Python 3.14 + `.venv`).
- Secrets live in `.env` (git-ignored): `OPENAI_API_KEY` (cloud STT), `HF_TOKEN`
  (faster model downloads). **Never commit or overwrite `.env`.**

---

## 4. Module map

| File | Lines | Role |
|---|---|---|
| `rf_hotscan.py` | ~2500 | Main app: `Scanner` engine, `GqrxClient`, `ScannerGUI`, bookmarks, squelch/noise-floor, transcripts+playback UI, multi-row tag filters |
| `rtl_backend.py` | ~730 | `RtlBackend`: dongle control, channelized sweep, `FMDemod`, squelch-gated audio, recorder hooks, audio playback with PLL lock settle |
| `recorder.py` | ~306 | `WavRecorder` (clean-cut per-transmission WAV) + `RecordingsDB` (SQLite); `on_start`/`on_record`/`on_discard` hooks |
| `stt.py` | ~700 | `SttProvider` interface + 5 engine families (Parakeet, Whisper-MLX, Voxtral, Vosk, OpenAI); `TranscriptionService` worker; `.env` loader |
| `player.py` | ~159 | `WavPlayer`: async play/pause/stop on the OS default device, independent of the SDR |
| `clock.py` | ~50 | Shared time base: UTC epoch persisted, ISO-8601 rendered |
| `heatmap.py` | ~1720 | Spectrum capture/heatmap (separate subsystem; SQLite + JSONL); fixed numpy dtype bug on colormap rebuild |

---

## 5. Threading & data-flow rules (IMPORTANT for agents)

- **One engine thread owns the device.** The GUI never touches the backend
  device directly — it calls `snapshot_ui()` / `set_cfg()` / `request(...)` and
  reads results from queues. Respect this; don't add direct device calls from Tk
  callbacks.
- Channel state is keyed by **`cid`** (not frequency — duplicate-frequency
  bookmarks exist). The scan rotation dedupes by frequency.
- **One dBFS scale everywhere.** `channel_power_dbfs()` is used for sweep
  detection, per-channel level reads, and the live squelch level. The RTL
  detection bandwidth (`channel_bw`, 12.5 kHz) is **fixed** — `set_mode()` must
  NOT change it, or the noise floor stops being comparable to live levels.
- Transcript rows are keyed by **`wav_path`**; lifecycle events
  (`start`→`stop`→`text`, plus `discard`) flow recorder-thread → `txnq` →
  GUI, and STT results flow service-thread → `transcriptq` → GUI. Both drained in
  `_drain_transcripts`.
- Time: persist **UTC epoch**, render ISO. Durations come from sample counts or
  `clock.mono()` deltas, never from subtracting wall-clock reads.

---

## 6. STT subsystem

`SttProvider` is the swappable interface (`available()` / `ensure_ready()` /
`warm_up()` / `transcribe(audio, sr, wav_path)`). `wants_audio` tells the service
whether to decode the WAV (local) or pass the path (cloud/Voxtral).
`make_provider(engine, model)` + `engine_options()` (GUI dropdown) +
`TranscriptionService` (off-thread queue worker, writes `recordings.sqlite`).

**Engines available now** (all downloaded/validated):
- Local: Parakeet TDT v2 & v3 (Canary), Whisper large-v3-turbo, Whisper small,
  Voxtral Mini 3B, Vosk (Kaldi).
- Cloud (needs `OPENAI_API_KEY`): gpt-4o-mini-transcribe, gpt-4o-transcribe,
  whisper-1.

### Benchmark findings (20 LBPD/LBFD/HAM clips, ground-truth WER)

| Model | WER↓ | × realtime | Notes |
|---|---|---|---|
| OpenAI Whisper-1 (cloud) | 24.5% | net-bound | best accuracy |
| GPT-4o mini transcribe (cloud) | 25.1% | net-bound | |
| **Voxtral Mini 3B (local)** | **28.9%** | **1×** | best *local* accuracy, slow |
| GPT-4o transcribe (cloud) | 32.5% | net-bound | worse than the cheaper cloud models |
| **Parakeet TDT v2 (local)** | 33.6% | **35×** | fastest by far; live default |
| Canary v3 (local) | 36.9% | 8× | worse than v2 here (multilingual dilution) |
| Whisper large-v3-turbo (local) | 51.0% | 3× | hallucinates on short clips |
| Vosk (local) | 60.8% | 5× | low ceiling but never hallucinates |
| Whisper small (local) | 226% | 6× | broken on short noisy clips (verified NOT corrupt) |

**Operating guidance ("two models for two jobs"):** Parakeet for the **live**
transcript pane (35× realtime, 0.26 s/clip); Voxtral or a cloud model for
**after-the-fact** re-transcription of a garbled clip (the ↻ button).

---

## 7. Bookmarks & filter categories

**9 filter tags** with 100+ frequencies covering:
- **LBC** (Long Beach city services)
- **LBPD** (Long Beach PD)
- **LBFD** (Long Beach Fire/EMS)
- **POLB** (Port of Long Beach)
- **LBCC** (County services)
- **LBUSD** (School district)
- **CSULB** (Campus)
- **USCG** (Coast Guard)
- **HAM** (Amateur radio)
- **GMRS/FRS** (GMRS/FRS — family radio)
- **USFS** (US Forest Service — Angeles National Forest, Los Angeles River Ranger District & Chilao area; 16 channels: forest net, admin net, air-to-ground, R5 tactical, LA County Fire coordination)
- **NWS** (NOAA Weather Radio — Southern California; 8 channels spanning LA County, San Diego, Inland Empire, Ventura)
- **LASD** (LA County Sheriff Department; 28 channels: 18 dispatch, 7 local tactical, 3 special ops)

### UI improvements
- **Per-row playback buttons:** Each transmission in the transcript pane shows ▶ (play) and ↻ (re-transcribe with selected STT engine)
- **Multi-row tag filters:** Filter buttons now wrap to 2–3 rows instead of overflowing off-screen (wraps at ~7 buttons/row based on 900px available width)
- **Double-click tuning:** Selecting a station automatically unmutes audio (sets AF gain to 0 dB if negative)

---

## 8. Audio playback (RTL backend)

**Recent fix (commit `657f761`):**
- **Root cause:** RTL tuner's PLL wasn't locking to the requested frequency before the backend began reading IQ samples. This caused "PLL not locked" warnings and garbled/missing audio.
- **Fix:** Added 100ms settle time after setting `sdr.center_freq` to allow the R820T tuner's phase-locked loop to lock before `read_samples_async()` begins.
- **Result:** Double-clicking a station now produces clear, immediate audio playback (previously silent or garbled until manual gain adjustment).

---

## 9. Recording & playback

- `WavRecorder` writes one **clean-cut** WAV per transmission (starts at signal
  onset, ends at signal drop; sub-`MIN_DUR` blips discarded). 48 kHz/16-bit mono.
  Metadata → `recordings.sqlite` (WAL, thread-locked, additive migrations);
  one JSONL event per recording for agent tailing.
- Hooks: `on_start` (live UI row at onset), `on_record` (finalized → STT + stop
  row), `on_discard` (blip → drop the live row).
- `WavPlayer` plays a recording on its **own** sounddevice output stream — never
  touches the RTL device, so scanning continues. Click a row's ▶, or select a row
  for the transport bar.
- **Audio routing:** output follows the **OS default device** (PortAudio is
  re-initialized per stream so a hot-plugged headset is picked up). No device is
  pinned.

---

## 10. Where data lives

App data lives **next to the app** (moved out of `~/.config/gqrx`):
- `recordings/` — WAVs + `recordings.sqlite` + `recordings.events.jsonl`
- `scanner.log`, `scanner_settings.json`
- `.models/` — locally-downloaded model weights (Vosk); **gitignored**
- HuggingFace cache (`~/.cache/huggingface`) — MLX model weights
- **Shared with GQRX:** only `~/.config/gqrx/bookmarks.csv` (with an APPDIR
  fallback). Heatmap data: `heatmap.sqlite` / `heatmap.events.jsonl`.

Gitignored: `.env*`, `.models/`, `recordings/`, logs, settings, `dev/`,
`Frequency_Reference/`, PDFs.

---

## 11. Known issues / open decisions

- **Branch `direct-sdr-backend` is unmerged** — no PR yet; ready for review.
- **Canary-v3 underperforms** Parakeet-v2 on this traffic — likely keep v2 as
  the local default; v3 stays selectable.
- **Voxtral is ~1× realtime** — fine for batch/re-transcribe, too slow for live.
- WER number-formatting caveat (above) — a digit-normalizing WER pass was
  proposed but not built.

---

## 12. Pointers

- `docs/ARCHITECTURE.md` — design/internals.
- `docs/AGENTS.md` — conventions, JSONL schemas, cross-agent coordination.
- `README.md` — public-facing overview + bookmark provenance.
- `dev/PROGRESS.md`, `dev/PLAN_stt.md`, `dev/SPIKE_direct_rtlsdr.md` — maintainer
  notes (gitignored).
