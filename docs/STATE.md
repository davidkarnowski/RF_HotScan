# RF HotScan — Current State (2026-07-15)

A point-in-time snapshot for humans and agents. For *how it's built* see `docs/ARCHITECTURE.md`; for cross-agent conventions see `docs/AGENTS.md`. This is the "where are we right now" layer, kept faithful to the **committed code**.

---

## 1. What it is

A Python/Tkinter **scanner** for public-safety / amateur radio, in three Notebook tabs: **Scanner / Recordings / Heatmap**. It detects active channels from a bookmark list, parks ("holds") on a transmission, plays its audio, optionally **records** each transmission to WAV, optionally **transcribes** recordings (local or cloud STT), and optionally **heals** transcripts with an LLM using channel context. A **radio playhead** in the banner lets you manually tune any frequency and listen, independent of scanning.

Two interchangeable **backends**:
- **RTL-SDR direct (default & focus)** — `rtl_backend.py` owns the dongle: channelized FFT sweep (~35–77 ch/s), NBFM demod + gapless audio. 100 ms tuner PLL-lock settle before reading samples.
- **GQRX remote (legacy fallback)** — `GqrxClient` over rigctl TCP 127.0.0.1:7356. Produces no audio samples (recording/STT are RTL-only); GQRX makes its own audio.

Target platforms: **macOS (Apple Silicon) primary, Linux/Ubuntu secondary. Windows is out of scope.**

---

## 2. Git status

- Branch: **`feature/phase2-stt-healing`** (local; not pushed).
- Working tree: clean after the Phase 3 commits.

### Recent commits (newest first)
| Hash | Message |
|---|---|
| *(this commit)* | docs: sync all docs with post-remediation code |
| `70142c3` | chore: retire GQRX-first branding (docstring, window title) |
| `2364cac` | fix(stt): healing gets desc context, junk-eligible dual-STT fallback, cached fallback provider, locked cfg access |
| `3e1ba2a` | Phase 1: DB migration and Recordings UI |
| `3e58b1d` | docs: sync documentation with current codebase truth |
| `c7fe0ff` | docs: update documentation to reflect raw RTL-SDR backend focus |

Plans and their status live in `docs/plans/` (Phase 1 complete, Phase 2 implemented + remediated, Phase 3 = the remediation itself).

---

## 3. How to run

```bash
.venv/bin/python rf_hotscan.py
```
- **RTL backend** needs `requirements-rtl.txt` (numpy/scipy/sounddevice/pyrtlsdr); **STT** needs `requirements-stt.txt`. All STT deps are optional/lazy-imported.
- **Healing** needs either a running Ollama (`localhost:11434`) or `OPENAI_API_KEY`; `healer.py` itself is stdlib-only.
- macOS Tk: `brew install python-tk@3.14`.
- Secrets in `.env` (git-ignored): `OPENAI_API_KEY`, `HF_TOKEN`. Never commit `.env`.

---

## 4. Module map

| File | Role |
| --- | --- |
| `rf_hotscan.py` | Main app: `Scanner` engine, `GqrxClient`, `ScannerGUI`, `RecordingsView` (Recordings tab), bookmarks, squelch/noise-floor, multi-row tag filters, radio playhead, transcript pane + recording playback |
| `rtl_backend.py` | `RtlBackend`: dongle control, channelized sweep, `FMDemod`, squelch-gated audio + playback (`on_hold`/`listen`/`play_async`), recorder hooks, async-stream guards |
| `recorder.py` | `WavRecorder` (clean-cut per-transmission WAV) + `RecordingsDB` (SQLite; transcript + healed columns, `desc`) |
| `stt.py` | `SttProvider` interface + 4 engine families (Parakeet-MLX, Whisper-MLX, Voxtral, OpenAI); `TranscriptionService` worker with `_heal` hook; `.env` loader |
| `healer.py` | `HealerProvider` interface + Ollama/OpenAI healers (stdlib urllib); `make_healer`, `engine_options` |
| `player.py` | `WavPlayer`: async playback on the OS default device, independent of the SDR |
| `clock.py` | Shared time base: UTC epoch persisted, ISO-8601 rendered |
| `heatmap.py` | Spectrum capture/heatmap (third tab; SQLite + JSONL) |

---

## 5. Threading & data-flow rules (IMPORTANT for agents)

- **One engine thread owns the device.** GUI calls `snapshot_ui()` / `set_cfg()` / `request(...)`; it never touches the backend device directly.
- **Keying:** enabled/disabled is per-bookmark (`cid`, persisted by `freq:name` signature); lockout / priority / last-active are per-frequency **by design** (level-only detection can't split same-frequency bookmarks). Scan rotation dedupes by frequency.
- **One dBFS scale everywhere** (`channel_power_dbfs`); RTL detection bandwidth `channel_bw` (12.5 kHz) is **fixed** — `set_mode()` must not change it.
- **Audio is started only by `on_hold()`** (RTL). Both the scan loop's `_hold()` and the manual playhead's `_listen_freq()` call it; nothing else makes sound.
- Transcript rows keyed by **`wav_path`**; events flow recorder→`txnq`→GUI and STT service→`transcriptq`→GUI, drained in `_drain_transcripts`.
- Worker threads read config via **`Scanner.get_cfg`** (lock-guarded) — `TranscriptionService` receives it as `cfg_get`. Never share the raw `cfg` dict across threads.
- Persist UTC epoch (`clock.now_unix()`), render ISO; durations from sample counts / `clock.mono()`.

---

## 6. Radio playhead (manual tune + listen)

Banner widget: an **editable frequency field**, **▶ Play**, **⏹ Stop**, and a green **● LIVE** dot.
- Engine actions `listen_freq(freq)` / `stop_listen` on `Scanner`; UI state `ui["tuned"]` (authoritative tuned Hz) and `ui["listening"]` (audio live).
- `listen_freq` pauses scanning, tunes, and starts RTL audio via `on_hold`; for a non-bookmarked frequency it synthesizes an NBFM channel (`cid=-1`).
- The field shows `ui["tuned"]` and is rewritten by the refresh tick **except while being edited** (`_freq_editing`). `_parse_freq` accepts MHz / Hz / commas, clamps ~24–1766 MHz.
- **Double-click a station → tune + start audio immediately.**
- Last manual frequency persists (`last_listen_freq` in `scanner_settings.json`).
- GQRX: tuning yields audio inherently; Stop soft-mutes.

---

## 7. STT subsystem + healing (as wired today)

`SttProvider` interface (`available`/`ensure_ready`/`warm_up`/`transcribe`). **Engines wired in `_PROVIDERS`:** `parakeet-mlx`, `whisper-mlx`, `voxtral`, `openai`.
- **Local:** Parakeet TDT v2 (default), Voxtral Mini 3B, Whisper-MLX (turbo/medium/small).
- **Cloud (needs `OPENAI_API_KEY`):** gpt-4o-mini-transcribe, gpt-4o-transcribe, whisper-1.
- `engine_options()` only returns engines whose deps + weights/credentials are present, so the GUI dropdown reflects what's actually installed.

**Re-transcribe (↻):** each finalized transcript row has a ↻ button that re-runs that recording through the **currently-selected** model (changing the Model dropdown rebuilds the service).

**Healing (HEALING (LLM) section in the control panel):**
- Cfg keys: `enable_healing`, `healer_engine` (`ollama`/`openai`), `healer_model`, `agentic_fallback`, `fallback_stt_engine`, `fallback_stt_model`.
- `TranscriptionService._heal` runs after the raw transcript, **before** the junk short-circuit. Context sent to the LLM: channel `name`, `tag`, and `desc` (from the bookmark file's description fields — tag-level + channel-level, merged).
- With **Agentic Fallback (Dual-STT)** on, junk or <3-word transcripts get a second opinion from a cached fallback STT provider, and the LLM arbitrates between the two readings. A rescued junk transcript is emitted as real text; an unrescued one stays `no_speech`.
- Healed results land in `recordings.sqlite` (`healed_transcript`, `healed_by_engine`, `healed_at`) and appear in the Recordings tab (editable inline).
- Healer failures are surfaced (`last_error` → one log line); Ollama availability is checked by pinging the daemon; OpenAI healer options appear only when the key is set.

---

## 8. Recordings tab

`RecordingsView` (second tab): latest 300 recordings with time/tag/channel/duration, STT engine + raw transcript, healer + healed transcript. Own WAL connection to `recordings.sqlite`; **manual ↻ Refresh** (not live). Double-click a row to play (pause/resume + progress bar); double-click the *Healed STT* column to edit the cell inline.

---

## 9. Bookmarks & filter categories

Bookmark source: `~/.config/gqrx/bookmarks.csv` (fallback `./bookmarks.csv`).
- The **live** file currently has **15 tags** (local Long Beach set + USFS + NWS + LASD), ~206 channels.
- The **bundled** `examples/long_beach_bookmarks.csv` has **14 tags** (no USFS), ~200 channels.
- **Description extension:** an optional 3rd field on tag lines and 6th field on channel lines carries a description used as healer context (`load_bookmarks` merges tag-level + channel-level into `desc`). ⚠️ **GQRX rewrites `bookmarks.csv` when you edit bookmarks in its own UI, which would strip these extra fields** — edit the file by hand (or keep a backup) if descriptions matter.

UI: tag-filter buttons wrap to multiple rows (`_build_taglist`); per-row ▶ play and ↻ re-transcribe buttons in the transcript pane; the signal-meter red squelch marker is click-draggable; wheel/two-finger scroll works anywhere over the control panel or station listing.

---

## 10. Recording & playback

`WavRecorder` writes one clean-cut WAV per transmission (48 kHz/16-bit mono); metadata (incl. `desc`) → `recordings.sqlite` (WAL) + one JSONL event per recording. `WavPlayer` plays on its own OS-default output stream (never touches the SDR). Manual-listen records too if Record is enabled (`on_hold` arms the recorder).

---

## 11. Where data lives

Next to the app: `recordings/` (+ `recordings.sqlite`, `recordings.events.jsonl`), `scanner.log`, `scanner_settings.json`, `heatmap.sqlite`, `heatmap.log`, `heatmap.events.jsonl`, `heatmap_settings.json`, `.models/` (gitignored), HF cache for MLX weights. Shared with GQRX: only `~/.config/gqrx/bookmarks.csv`. Gitignored: `.env*`, `.models/`, `recordings/`, logs, settings, `dev/`, PDFs.

---

## 12. Known issues / open work

- Recordings tab needs a manual ↻ Refresh to see new recordings (no live update).
- Healing requires a running Ollama or an OpenAI key; with neither, the HEALING section shows no model picker.
- Whisper-MLX weights were deleted for space; re-download to use that engine.
- GQRX editing its own bookmarks would strip the `desc` extension fields (see §9).
- Fallback STT guard compares provider *names* only — a fallback that is the same engine with a different model is skipped.
- Live end-to-end healing verification (dongle + real transmission) still pending; the pipeline is verified offline with stubs (see `docs/plans/phase_3_remediation.md`).

---

## 13. Pointers

`docs/ARCHITECTURE.md` (internals), `docs/AGENTS.md` (conventions), `README.md` (public overview), `docs/plans/` (phase plans + remediation), `dev/` (gitignored maintainer notes/research).
