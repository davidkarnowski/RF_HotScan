# RF HotScan — Current State (2026-06-21)

A point-in-time snapshot for humans and agents. For *how it's built* see `docs/ARCHITECTURE.md`; for cross-agent conventions see `docs/AGENTS.md`. This is the "where are we right now" layer, kept faithful to the **committed code**.

---

## 1. What it is

A Python/Tkinter **scanner** for public-safety / amateur radio. It detects active channels from a bookmark list, parks ("holds") on a transmission, plays its audio, optionally **records** each transmission to WAV, and optionally **transcribes** recordings (local or cloud STT). A **radio playhead** in the banner lets you manually tune any frequency and listen, independent of scanning.

Two interchangeable **backends**:
- **RTL-SDR direct (default & focus)** — `rtl_backend.py` owns the dongle: channelized FFT sweep (~35–77 ch/s), NBFM demod + gapless audio. 100 ms tuner PLL-lock settle before reading samples.
- **GQRX remote (legacy fallback)** — `GqrxClient` over rigctl TCP 127.0.0.1:7356. Produces no audio samples (recording/STT are RTL-only); GQRX makes its own audio.

Target platforms: **macOS (Apple Silicon) primary, Linux/Ubuntu secondary. Windows is out of scope.**

---

## 2. Git status

- Branch: **`main`** (fully merged, in sync with GitHub private remote).
- Last commit: **`5bfc6ba`** — "chore: harden .gitignore (IDE, editor, crypto key patterns)"
- Working tree: clean.

### Recent commits (newest first)
| Hash | Message |
|---|---|
| `5bfc6ba` | chore: harden .gitignore (IDE, editor, crypto key patterns) |
| `076f244` | merge: direct-sdr-backend — RTL backend, heatmap, recording, STT, playhead |
| `88deb51` | fix: heatmap crash on second session start (nan_to_num with None dmin) |
| `453dc8c` | fix: squelch readout snap-back (RTL) + auto-noise-floor not applying |
| `3401613` | docs: update STATE.md — Voxtral re-added, re-transcribe, draggable squelch, scroll |
| `df0bdb6` | feat: draggable squelch marker, re-transcribe button, panel-wide scroll |
| `4d7d74b` | feat: re-add Voxtral STT provider |

---

## 3. How to run

```bash
.venv/bin/python rf_hotscan.py
```
- **RTL backend** needs `requirements-rtl.txt` (numpy/scipy/sounddevice/pyrtlsdr); **STT** needs `requirements-stt.txt`. All STT deps are optional/lazy-imported.
- macOS Tk: `brew install python-tk@3.14`.
- Secrets in `.env` (git-ignored): `OPENAI_API_KEY`, `HF_TOKEN`. Never commit `.env`.

---

## 4. Module map

| File | Role |
| --- | --- |
| `rf_hotscan.py` | Main app: `Scanner` engine, `GqrxClient`, `ScannerGUI`, bookmarks, squelch/noise-floor, multi-row tag filters, **radio playhead**, transcript pane + recording playback |
| `rtl_backend.py` | `RtlBackend`: dongle control, channelized sweep, `FMDemod`, squelch-gated audio + playback (`on_hold`/`listen`/`play_async`), recorder hooks |
| `recorder.py` | `WavRecorder` (clean-cut per-transmission WAV) + `RecordingsDB` (SQLite) |
| `stt.py` | `SttProvider` interface + **3 engine families (Parakeet-MLX, Whisper-MLX, OpenAI)**; `TranscriptionService` worker; `.env` loader |
| `player.py` | `WavPlayer`: async playback on the OS default device, independent of the SDR |
| `clock.py` | Shared time base: UTC epoch persisted, ISO-8601 rendered |
| `heatmap.py` | Spectrum capture/heatmap (separate tab; SQLite + JSONL) |

---

## 5. Threading & data-flow rules (IMPORTANT for agents)

- **One engine thread owns the device.** GUI calls `snapshot_ui()` / `set_cfg()` / `request(...)`; it never touches the backend device directly.
- Channel state keyed by **`cid`** (not frequency — duplicate-frequency bookmarks exist). Scan rotation dedupes by frequency.
- **One dBFS scale everywhere** (`channel_power_dbfs`); RTL detection bandwidth `channel_bw` (12.5 kHz) is **fixed** — `set_mode()` must not change it.
- **Audio is started only by `on_hold()`** (RTL). Both the scan loop's `_hold()` and the manual playhead's `_listen_freq()` call it; nothing else makes sound.
- Transcript rows keyed by **`wav_path`**; events flow recorder→`txnq`→GUI and STT service→`transcriptq`→GUI, drained in `_drain_transcripts`.
- Persist UTC epoch, render ISO; durations from sample counts / `clock.mono()`.

---

## 6. Radio playhead (manual tune + listen)

Banner widget: an **editable frequency field**, **▶ Play**, **⏹ Stop**, and a green **● LIVE** dot.
- Engine actions `listen_freq(freq)` / `stop_listen` on `Scanner`; UI state `ui["tuned"]` (authoritative tuned Hz) and `ui["listening"]` (audio live).
- `listen_freq` pauses scanning, tunes, and starts RTL audio via `on_hold`; for a non-bookmarked frequency it synthesizes an NBFM channel (`cid=-1`).
- The field shows `ui["tuned"]` and is rewritten by the refresh tick **except while being edited** (`_freq_editing`) so typing isn't clobbered ("locked to the actual tune"). `_parse_freq` accepts MHz / Hz / commas, clamps ~24–1766 MHz.
- **Double-click a station → tune + start audio immediately** (the fix for the long-standing "no audio on double-click" bug; `goto` is folded into `listen_freq`).
- Last manual frequency persists (`last_listen_freq` in `scanner_settings.json`).
- GQRX: tuning yields audio inherently; Stop soft-mutes.

---

## 7. STT subsystem (as wired today)

`SttProvider` interface (`available`/`ensure_ready`/`warm_up`/`transcribe`). **Engines wired in `_PROVIDERS`:** `parakeet-mlx`, `whisper-mlx`, `voxtral`, `openai`.
- **Local:** Parakeet TDT v2 (default), Voxtral Mini 3B, Whisper-MLX (turbo/small).
- **Cloud (needs `OPENAI_API_KEY`):** gpt-4o-mini-transcribe, gpt-4o-transcribe, whisper-1.
- `engine_options()` only returns engines whose deps + weights are present, so the GUI dropdown reflects what's actually installed.

**Currently listed (weights on disk):** Parakeet v2 + Voxtral Mini 3B (local) and the 3 OpenAI cloud models. Whisper-MLX is registered but **auto-hidden** — its weights were deleted to save space; re-download to use it.

**Re-transcribe (↻):** each finalized transcript row has a ↻ button that re-runs that recording through the **currently-selected** model (changing the Model dropdown rebuilds the service). Pairs Parakeet (fast, live) with Voxtral (slower, more accurate) for an after-the-fact second pass.

---

## 8. Bookmarks & filter categories

**13 filter tags**, 100+ frequencies. Local: LBC, LBPD, LBFD, POLB, LBCC, LBUSD, CSULB, USCG, LBT, LBMH, HAM, GMRS/FRS. Plus:
- **USFS** — Angeles National Forest (LA River Ranger District & Chilao); 16 ch.
- **NWS** — NOAA Weather Radio, Southern California; 8 ch.
- **LASD** — LA County Sheriff; ~28 ch (dispatch / local-tac / special ops).

UI: tag-filter buttons wrap to multiple rows (`_build_taglist`, ~7/row); per-row ▶ play and ↻ re-transcribe buttons in the transcript pane; the signal-meter red squelch marker is **click-draggable** (sets the global squelch, switches to global mode, keeps marker/slider/readout aligned); wheel/two-finger scroll works anywhere over the control panel or station listing (`_on_mousewheel` bind_all router), not just on the scrollbar.

---

## 9. Recording & playback

`WavRecorder` writes one clean-cut WAV per transmission (48 kHz/16-bit mono); metadata → `recordings.sqlite` (WAL) + one JSONL event per recording. `WavPlayer` plays on its own OS-default output stream (never touches the SDR). Manual-listen records too if Record is enabled (on_hold arms the recorder).

---

## 10. Where data lives

Next to the app: `recordings/` (+ `recordings.sqlite`, `recordings.events.jsonl`), `scanner.log`, `scanner_settings.json`, `.models/` (gitignored), HF cache for MLX weights. Shared with GQRX: only `~/.config/gqrx/bookmarks.csv`. Gitignored: `.env*`, `.models/`, `recordings/`, logs, settings, `dev/`, PDFs.

---

## 11. Known issues / open work

- Whisper-MLX weights were deleted for space; re-download to use that engine.
- Vosk + Parakeet-v3 (Canary) providers are not wired (Voxtral was re-added); add back only if disk space allows.
- Playhead + draggable squelch + re-transcribe have been tested and verified.

---

## 12. Pointers

`docs/ARCHITECTURE.md` (internals), `docs/AGENTS.md` (conventions), `README.md` (public overview), `dev/PLAN_playhead.md` + `dev/stt_eval/` (gitignored maintainer notes/research).
