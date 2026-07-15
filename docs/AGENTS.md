# RF HotScan — Agent Guide

Modular Python/Tkinter bookmark scanner for RTL-SDR dongles (primary) with legacy GQRX TCP fallback.
Audio recorder, speech-to-text transcriber with LLM transcript healing, recordings browser, and
wideband heatmap — three tabs (Scanner / Recordings / Heatmap) in one app.

| Module | Lines | Role |
|---|---|---|
| `rf_hotscan.py` | ~3 000 | GUI + scanner engine + GqrxClient + RecordingsView |
| `rtl_backend.py` | ~740 | Direct RTL-SDR backend (pyrtlsdr) |
| `heatmap.py` | ~1 710 | Wideband sweep, DB, detection, rendering |
| `recorder.py` | ~310 | WAV squelch-gated recorder + RecordingsDB |
| `stt.py` | ~660 | Speech-to-text providers + TranscriptionService (+ healing hook) |
| `healer.py` | ~120 | LLM transcript healing providers (stdlib urllib; Ollama + OpenAI) |
| `player.py` | ~160 | Audio playback |
| `clock.py` | 50 | Single time-source (stdlib only) |
| `test_heatmap.py` | ~230 | Heatmap unit tests |

---

## Ground truth: read these signals, don't guess

The scanner writes **`scanner.log`** (rotating, always-on).
Tail it for real-time observation without touching the GUI:

```bash
tail -f scanner.log
```

The GUI also has a **Log pane** that mirrors the same stream.
For headless or CI work, the log file is the primary observation channel.

---

## Verifying behaviour WITHOUT clicking the GUI

### Headless engine smoke-test

Run from the repo root (verified-runnable; the engine thread starts and the
state lands on `DISCONNECTED` when GQRX isn't running — that's success):

```python
from rf_hotscan import Scanner, GqrxClient, load_bookmarks, cluster_bands, BOOKMARKS
tags, chans = load_bookmarks(BOOKMARKS)
for i, c in enumerate(chans):
    c["cid"] = i
sc = Scanner(GqrxClient(), tags, chans, cluster_bands(chans))
sc.request("reconnect")            # engine thread connects (harmless if GQRX is closed)
import time; time.sleep(1.0)
print("state:", sc.snapshot_ui()["state"])
sc.alive = False                   # stop the engine thread
# ... observe scanner.log ...
```

### Headless GUI smoke-test (Tk opens then closes)

```python
import tkinter as tk
root = tk.Tk()
# build app …
root.after(2500, root.destroy)
root.mainloop()
```

---

## Probing GQRX directly

GQRX must have **Tools → Remote control** enabled.
Open a raw TCP socket to `127.0.0.1:7356`:

```
$ nc 127.0.0.1 7356
f            ← get frequency
F 154600000  ← set frequency
l STRENGTH   ← read signal strength (dBFS)
```

Useful discovery commands:

| Command | Purpose |
|---|---|
| `l ?` | List supported *get* levels |
| `L ?` | List supported *set* levels |
| `_` | Return GQRX version string |

Known levels on GQRX 2.17.x: **STRENGTH** (read-only), **SQL**, **AF**, **LNA_GAIN**.

> There is no remote command to read bookmarks — RF HotScan parses the GQRX CSV bookmark file directly.

---

## Hard invariants

1. **Only the engine thread touches the backend/socket.**
   GUI communicates via `set_cfg` / `request` / `Events`.
2. **Channel identity vs. RF state.** Multiple bookmarks can share a frequency.
   *Per-bookmark* state (enabled/disabled) is keyed by `cid` and persisted by a
   `freq:name` signature. *Per-frequency* state (lockout, priority, last-active)
   is keyed by frequency **by design** — a level-only scanner cannot tell two
   bookmarks on one frequency apart, so locking/prioritizing the frequency is
   the honest behavior. Don't "fix" this to CID keying.
3. **Scan hot-path is lean:** one tune + one strength read per hop.
4. **Scan thread never dies silently** — unhandled exceptions are caught, logged, and surfaced.
5. **Detection is level-only** — no tone/digital decode in the scan loop.
6. **`cfg` / UI access goes through the lock.** Worker threads read config via
   `Scanner.get_cfg` (lock-guarded) — e.g. `TranscriptionService` receives it as
   its `cfg_get` callable. Never hand the raw `cfg` dict to another thread.

---

## Gotchas

- **`get_mode` (`m`) returns TWO lines** (mode then passband).
  Use `_cmd(..., 2)` to consume both.
- **AF gain clamps to −80 … +50 dB**; SQL and STRENGTH are dBFS (≈ −100 noise … 0).
- **Slider feedback loops:** programmatically moving a Tk slider can trigger its
  `command` callback. `_suppress_push` guards GQRX→GUI syncs. Preserve that pattern.
- **Disabled-channel persistence** uses a `freq:name` signature, not `cid`,
  so the selection survives bookmark edits. Keep that mapping in `_load/_save_settings`.
- **Big frequency hops** (across bands) re-centre the SDR hardware;
  `_tune` adds ≈150 ms settle when `band_index` changes.

---

## Backends & RTL-SDR

Two backends, selected via **BACKEND** radio buttons in the GUI.
The scanner holds `self.client` and calls a fixed interface:

| Method | Direction | Notes |
|---|---|---|
| `connect` / `close` / `connected` | lifecycle | |
| `set_freq` / `get_freq` | tune | |
| `set_mode` / `get_mode` | demod mode | |
| `strength` | read | returns dBFS |
| `get_sql` / `set_sql` | squelch | |
| `get_af` / `set_af` | audio gain | |
| `get_lna` / `set_lna` | RF gain | |
| `on_hold(ch, thr)` / `on_resume()` | optional | audio squelch-gate |
| `sweep(freqs)` → `({freq:dbfs}, nwin)` | optional | wideband scan |
| `recommended_settle_ms` | property | GqrxClient=350, RtlBackend=30 |

### GqrxClient (`rf_hotscan.py`)

Stdlib-only, rigctl TCP to `HOST=127.0.0.1`, `PORT=7356`.
No audio samples, no recording capability.

### RtlBackend (`rtl_backend.py`)

Direct dongle via **pyrtlsdr**. Provides `sweep()` and FM-demod audio on `on_hold`.
Constants: `SAMPLE_RATE=2_400_000`, `USABLE_BW=2_000_000`, `DC_GUARD=30_000`,
`channel_bw=12_500` (fixed), `SQUELCH_HYST=2.5` dB.

Run from `.venv` for RTL; bare `python3` runs the GQRX path only (`RTL_AVAILABLE=False`).

---

## Shared-dongle invariant (CRITICAL)

The RTL-SDR is a **single-owner USB device**.
Only one consumer can hold the dongle at a time.

- `_acquire_dongle` / `_release_dongle` enforce exclusive access.
- `DongleBusy` exception is raised on contention.
- `SdrShareCoordinator` provides `borrow` + auto-pause for cooperative sharing
  (e.g., scanner pauses while heatmap sweeps).
- **`cancel_read_async` pitfall:** calling it from the wrong thread can deadlock
  or silently fail. Always cancel from the reader thread's context.

---

## dBFS scale convention

`channel_power_dbfs(iq, fs, offset_hz, bw)` is **the one level measure** used by:

- sweep detection
- per-channel reads
- live hold level display

`window_power_dbfs` (heatmap) is PSD-based and on a **different scale**.
Thresholds do not transfer between differently-scaled measures.
If adding another level source, keep it on a comparable scale or document the offset.

---

## Time base — use `clock.py` everywhere

`clock.py` is the single time source shared by scanner, heatmap, and recorder.

| Function | Returns |
|---|---|
| `now_unix()` | UTC epoch float |
| `mono()` | monotonic clock (for durations) |
| `utc_iso(t)` / `local_iso(t)` | ISO string from epoch |
| `now_iso()` | current time as ISO |
| `file_stamp(t)` | filename-safe timestamp |

**Rules:**
- Persist **UTC epoch**; derive ISO for display.
- Durations from sample counts or `mono()` deltas.
- Heatmap stamps each power frame with `t_unix` + `t_dur_ms` + `iso` in `emit_event`.

---

## Audio squelch-gating + recording

`RtlBackend.on_hold(ch, thr)` starts FM demod and squelch-gating.
Gate logic: `open_ = live_power >= thr - SQUELCH_HYST`.

`recorder.py` (`WavRecorder`): clean-cut WAV files at `SAMPLERATE=48000`.
`CLOSE_CONFIRM_S=0.35` — gate must stay closed this long before ending a file.
`MIN_DUR_S=0.25` — recordings shorter than this are discarded.

GQRX backend: no audio samples available, no recording.

---

## Speech-to-text (`stt.py`, optional)

### Provider interface

Every provider implements: `available` / `ensure_ready` / `warm_up` / `transcribe`.

| Key | Provider | Notes |
|---|---|---|
| `parakeet-mlx` | `ParakeetMLXProvider` | Default. MLX JIT, English only. Model: `mlx-community/parakeet-tdt-0.6b-v2` |
| `whisper-mlx` | `MLXWhisperProvider` | Local. Checks HF cache for weights. Models: `large-v3-turbo`, `medium`, `small` |
| `voxtral` | `VoxtralMLXProvider` | Audio LLM, ≈1× realtime |
| `openai` | `OpenAIProvider` | Cloud, needs API key. Model: `gpt-4o-mini-transcribe`. Also: `gpt-4o-transcribe`, `whisper-1` |

Registry: `_PROVIDERS` dict. Auto-order: `_AUTO_ORDER = ["parakeet-mlx", "whisper-mlx", "voxtral", "openai"]`.

API: `make_provider(prefer="auto", model=None)`, `available_providers()`, `engine_options()`.

### TranscriptionService

Off-thread worker fed by a bounded queue. Target sample rate: `TARGET_SR=16000`.
States: `loading` → `idle` → `transcribing` → (back to `idle` or `error`).
`warm_up()` runs once on start (MLX JIT compile).
Filters junk transcriptions, writes results to `RecordingsDB`, emits via `transcriptq` → GUI **Transcripts** pane.

`WavRecorder.on_record(meta)` feeds completed recordings into the service.

### Transcript healing (`healer.py`, optional)

An LLM pass that cleans up raw transcripts using channel context (`name`,
`tag`, `desc` from the bookmark file's description fields).

- `HealerProvider` interface: `available()` / `heal(text, context, second_text)`;
  failures set `last_error` and return the input text unchanged.
- Providers: `ollama` (local daemon at `localhost:11434`; models discovered
  live) and `openai` (chat completions via stdlib urllib; gated on
  `OPENAI_API_KEY`). `make_healer(name, model)` builds one; `engine_options()`
  feeds the GUI picker. **`healer.py` must stay stdlib-only.**
- Flow (`TranscriptionService._heal`): runs when `enable_healing` is on, BEFORE
  the junk short-circuit. With `agentic_fallback` on, junk or <3-word
  transcripts first get a second opinion from a cached fallback STT provider
  (`fallback_stt_engine`/`fallback_stt_model`), and the LLM arbitrates between
  the two readings. A junk transcript rescued by healing is emitted as real
  text; an unrescued one falls through to `no_speech`.
- Results land in `recordings.sqlite` columns `healed_transcript`,
  `healed_by_engine`, `healed_at` (UTC epoch via `clock.now_unix()`), and are
  visible/editable in the Recordings tab.

---

## Heatmap (`heatmap.py`)

Second tab in the GUI **and** a standalone CLI module.

### Pipeline

`SweepConfig` → `SweepSource` (`RtlSweepSource` or `FakeSweepSource`) →
per-window Welch FFT → crop (`crop=0.20`) / DC-null → stitch → one dBFS row per sweep.

`HeatmapRecorder` engine thread (same shape as the scanner engine).
Default: `bin_hz=3000`, `duration_s=30`.

### HeatmapDB (SQLite WAL)

Tables: `sessions`, `power`, `activity`, `iq_dumps`.
Power rows are quantized `uint8`; `load_matrix` reconstructs the full array.

### Activity detection

Per-bin min-hold-with-leak floor. Row > floor + margin → active.
Contiguous active bins are clustered into activity events.

### Rendering

Live Tk canvas + offline matplotlib export.

### Scale caveat

`window_power_dbfs` ≠ `channel_power_dbfs` — see [dBFS scale convention](#dbfs-scale-convention).

### CLI

```bash
python -m heatmap scan|render|list|info   # outputs JSON
```

`FakeSweepSource` is **testing only** (synthetic data).

### Verification

```bash
.venv/bin/python test_heatmap.py
```

Shared dongle rules apply — see [shared-dongle invariant](#shared-dongle-invariant-critical).

`APP_VERSION = "heatmap-1.0"`.

---

## Things intentionally NOT done

- **No tone squelch** (CTCSS/DPL) — GQRX remote limitation; RTL backend could add it (planned).
- **No P25/DMR/NXDN demodulation.**
- **No writing to GQRX's bookmark file** — RF HotScan reads it, never edits it.
