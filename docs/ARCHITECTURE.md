# Architecture

RF HotScan is a single-process, pure-Python scanner and spectrum analyzer built on
Tkinter. It has no build step and no package install — run `rf_hotscan.py` directly.

| File | Lines | Role |
|------|------:|------|
| `rf_hotscan.py` | 2,745 | Scanner engine, GUI, bookmark loader, main entry |
| `rtl_backend.py` | 730 | RTL-SDR backend: sweep, listen, FM demod, dongle coordination |
| `heatmap.py` | 1,713 | Wideband heatmap: recorder, SQLite store, waterfall view, CLI |
| `recorder.py` | 307 | Audio recording to WAV |
| `stt.py` | 581 | Speech-to-text integration (Whisper / Vosk) |
| `player.py` | 160 | Audio playback utilities |
| `clock.py` | 51 | Monotonic clock helpers |

## Block Diagram

```
┌───────────────────────────────────────────────────────────┐
│                      Tk Root (Notebook)                   │
│  ┌─────────────────────────┐  ┌─────────────────────────┐ │
│  │      ScannerGUI         │  │      HeatmapTab         │ │
│  │  (dark clam theme)      │  │  (waterfall + controls) │ │
│  └────────┬────────────────┘  └────────┬────────────────┘ │
│           │ set_cfg / request / Events  │ action queue     │
│  ┌────────▼────────────────┐  ┌────────▼────────────────┐ │
│  │       Scanner           │  │   HeatmapRecorder       │ │
│  │    (engine thread)      │  │   (background thread)   │ │
│  └────────┬────────────────┘  └────────┬────────────────┘ │
│           │                            │                   │
│    ┌──────┴──────┐              ┌──────▼──────┐           │
│    │  GqrxClient │              │ SweepSource │           │
│    │  (TCP/4532) │              │ (base class)│           │
│    └─────────────┘              └──────┬──────┘           │
│                                        │                   │
│    ┌─────────────┐              ┌──────▼──────────┐       │
│    │  RtlBackend │◄─────────────│ RtlSweepSource  │       │
│    │ (librtlsdr) │  borrow via  │ (capture_iq)    │       │
│    └─────────────┘  Coordinator └─────────────────┘       │
└───────────────────────────────────────────────────────────┘
```

Both backends (`GqrxClient` and `RtlBackend`) implement the same interface, so the
scanner engine is backend-agnostic. The `SdrShareCoordinator` arbitrates dongle access
between the scanner and the heatmap recorder.

---

## §1 Bookmark Parsing & Bands

Bookmarks are loaded from GQRX's CSV format (`~/.config/gqrx/bookmarks.csv`, falling
back to `APPDIR/bookmarks.csv`). Each line contains frequency, name, modulation, and
bandwidth, with tags delimited by `;`.

Key functions:

| Function | Purpose |
|----------|---------|
| `load_bookmarks(path)` | Parse CSV → `(tags, channels)`. Returns ordered list of tags and channel dicts |
| `map_mode()` | Collapse modulation labels (e.g., "Narrow FM" → "FM") for backend compatibility |
| `cluster_bands(chans, gap=5MHz)` | Group channels into band spans separated by ≥5 MHz gaps |
| `band_index(freq, bands)` | Return which band a frequency belongs to |
| `luminance()` / `contrast_fg()` | Compute readable foreground color for tag label chips |

**CID keying.** Channels are identified by a composite ID (CID), not by frequency alone.
Duplicate-frequency bookmarks are valid (e.g., same frequency with different modes or
tags). All per-channel state — enabled, priority, lockout, last-active — is keyed by CID.
The settings persistence scheme uses a `freq:name` signature so that edits to the
bookmarks file don't silently discard saved state.

---

## §2 Hardware Backends

### Common Interface

Every backend must implement:

| Method | Signature | Notes |
|--------|-----------|-------|
| `connect()` | `→ None` | Open connection / acquire hardware |
| `close()` | `→ None` | Release resources |
| `connected` | property `→ bool` | |
| `set_freq(hz)` / `get_freq()` | `→ None` / `→ int` | Tuner frequency |
| `set_mode(mode)` / `get_mode()` | `→ None` / `→ str` | Modulation |
| `strength()` | `→ float` (dBFS) | Current signal level |
| `get_sql()` / `set_sql(val)` | `→ float` / `→ None` | Squelch threshold |
| `get_af()` / `set_af(val)` | `→ float` / `→ None` | AF gain |
| `get_lna()` / `set_lna(val)` | `→ float` / `→ None` | LNA / RF gain |
| `recommended_settle_ms` | attribute | Delay after tune before valid readings |

**Optional methods** (presence detected via `hasattr`):

| Method | Purpose |
|--------|---------|
| `sweep(freqs) → (dict, n_windows)` | Batch measure all freqs; returns `{freq: power_dbfs}` |
| `on_hold(ch, thr)` | Called when scanner locks onto a channel |
| `on_resume()` | Called when scanner releases a channel |

### GqrxClient (rf_hotscan.py L178–256)

TCP rigctl wrapper speaking Hamlib protocol on port 4532. Each command is serialized
through a `threading.Lock`. `recommended_settle_ms = 350` — GQRX needs time to
re-settle its DSP chain after a retune.

Method map to rigctl verbs:

- `strength()` → `l STRENGTH`
- `set_freq()` → `F <hz>`, `get_freq()` → `f`
- `set_mode()` → `M <mode> <bw>`, `get_mode()` → `m`

### RtlBackend (rtl_backend.py L243–670)

Direct RTL-SDR access via `librtlsdr`. Core constants:

```
SAMPLE_RATE   = 2_400_000
USABLE_BW     = 2_000_000
DC_GUARD      =    30_000
channel_bw    =    12_500   (fixed)
SQUELCH_HYST  =       2.5   dB
recommended_settle_ms = 30
```

**`plan_windows(freqs, usable, dc_guard)`** — Greedily packs sorted frequencies into the
fewest ~2 MHz windows. Nudges center frequencies to keep channels away from the DC spike.

**`channel_power_dbfs(iq, fs, offset_hz, bw)`** — THE canonical power measurement used
everywhere (sweep, single-channel reads, hold squelch checks). Pipeline:
127-tap FIR bandpass → frequency-shift to baseband → filter → mean |x|² → dB.

**FMDemod (L103–199)** — Stateful narrowband FM demodulator chain:

```
IQ → NCO shift → LPF + decimate ÷10 → channel LPF + decimate ÷5 (→ 48 kHz)
   → polar discriminator → de-emphasis (τ = 750 µs) → audio LPF
   → AGC (fast 0.5 / slow 0.03, frozen while squelched) → tanh soft limiter
```

Output: `audio_rate = 48000`, `dec1 = 10`, `dec2 = 5`.

**Listen pipeline** — `listen()` starts async IQ capture → FMDemod → ring buffer →
`sounddevice.OutputStream` for real-time audio.

**Sweep pipeline** — `sweep()` calls `plan_windows`, captures one IQ block per window,
runs `channel_power_dbfs` for each channel in that window, returns the full power map.

**Dongle coordination** — A single RTL-SDR stick is shared via `_owner_lock` /
`_owner` / `_owner_label`. `_acquire_dongle` and `_release_dongle` enforce exclusive
access. Attempting to acquire while busy raises `DongleBusy(RuntimeError)`.
`cancel_read_async` is used to interrupt IQ streaming, but must be called carefully
(pitfall: calling it outside an active read is a no-op or error depending on driver).

---

## §3 Scanner Engine (rf_hotscan.py L379–1009)

The `Scanner` runs on a dedicated daemon thread. It owns the backend connection and
performs ALL backend I/O — the GUI never talks to hardware directly.

### Config & State

**`self.cfg`** (set by GUI via `set_cfg`):

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled_tags` | all | Which tag groups to scan |
| `lockout` | `set()` | CIDs to skip |
| `disabled_cids` | `set()` | Per-channel disable |
| `priority_freqs` | `set()` | CIDs to check more often |
| `squelch_mode` | `"auto"` | `"auto"` or `"global"` |
| `global_sql` | `-50.0` | Manual squelch threshold (dBFS) |
| `auto_margin` | `8.0` | dB above noise floor for auto mode |
| `settle_ms` | `350` | Post-tune settling time |
| `hold_s` | `3.0` | Seconds of silence before releasing |
| `record` | `False` | Enable audio recording |
| `mute_squelch` | `False` | Mute audio below squelch |
| `stt_enabled` | `False` | Enable speech-to-text |
| `stt_engine` | `""` | `"whisper"` or `"vosk"` |
| `stt_model` | `""` | Model path |
| `priority_interval` | `6.0` | Seconds between priority checks |
| `last_listen_freq` | `None` | Frequency for listen mode |

**`self.ui`** (read by GUI via `_refresh`):

| Key | Meaning |
|-----|---------|
| `state` | `SCANNING` / `HOLDING` / `CALIBRATING` / `IDLE` / … |
| `cur` | Current channel dict |
| `strength` | Last measured dBFS |
| `thresh` | Active squelch threshold |
| `msg` | Status message |
| `gqrx_sql` | GQRX's squelch value (for two-way sync) |
| `af`, `lna` | Current AF/LNA gain |
| `powers` | `{cid: dBFS}` from last sweep |
| `sweep_n` | Sweep counter |
| `rate` | Channels per second |
| `tuned` | Currently tuned frequency |
| `listening` | `True` if in listen mode |

### The Scan Loop (L546–623)

```
_loop():
    while running:
        _drain_actions()
        if paused: sleep; continue

        # Fast path (RTL-SDR):
        if hasattr(client, 'sweep'):
            _sweep_pass()        # ~1s for full scan

        # Slow path (GQRX):
        else:
            for ch in active_channels:
                _tune(ch)
                _settled_strength()   # ~350ms settle
                if above_threshold:
                    _hold(ch)         # ~30s full scan
```

**`_sweep_pass` (L625–668)** — Calls `client.sweep(freqs)`, picks the strongest active
channel above threshold. Priority channels are favored. One sweep takes ~1 second for
RTL vs ~30 seconds per sequential GQRX scan.

### Hold (L685–749)

**`_hold` (L685–706)** — Calls `client.on_hold(ch, thr)` if available (enables RTL audio
demod), then enters `_hold_loop`. On exit, calls `client.on_resume()`.

**`_hold_loop` (L708–749)** — Polls `strength()` at ~80 ms intervals. Releases after
`hold_s` seconds of continuous silence (signal below threshold). The `skip` event
breaks out immediately. Priority pre-emption can interrupt a hold, **but NOT while
RTL `_playing`** (to avoid cutting off active audio).

### Action Queue

`_drain_actions` processes a thread-safe queue of requests from the GUI:

| Action | Effect |
|--------|--------|
| `reconnect` | Tear down and re-establish backend connection |
| `refresh_sql` | Re-read squelch from backend |
| `set_af` | Push AF gain to backend |
| `goto` | Tune to specific channel immediately |
| `noise_floor` | Trigger `_measure_noise_floor` |
| `listen_freq` | Enter single-frequency listen mode |
| `stop_listen` | Exit listen mode |

### Two-Way Squelch Sync

`_maybe_poll_sql` runs at ≤0.7 s throttle and reads GQRX's squelch into
`ui["gqrx_sql"]`. If the value differs from `_last_sql` (meaning the change came from
GQRX's UI, not ours), and we're in `global` mode, the scanner adopts the new value.
`_suppress_push` guards against GUI feedback loops when the app itself pushes a value.

### Auto-Noise-Floor Calibration (L928–1009)

Pauses scanning and enters `CALIBRATING` state. For each band:

1. Generate candidate frequencies at 25 kHz steps across the band span
2. Exclude frequencies within 15 kHz of any bookmarked channel
3. Cap at ~15 samples per band
4. Tune to each candidate, read `strength()`, report live progress to `ui`
5. Store **median** power as the band's noise floor
6. Auto threshold = floor + `auto_margin`

### Resilience

The scan loop is wrapped in a broad `except` with `traceback` logging. The thread never
dies silently — errors are captured and surfaced in `ui["msg"]`.

---

## §4 ScannerGUI (rf_hotscan.py L1015–~2680)

### Dark Theme

Built on Tk's `clam` theme with a custom dark palette:

| Token | Hex | Usage |
|-------|-----|-------|
| `BG` | `#1e1e1e` | Window background |
| `PANEL` | `#2a2a2a` | Frame / panel background |
| `FG` | `#e6e6e6` | Primary text |
| `ACCENT` | `#1e90ff` | Buttons, highlights |
| `ACTIVE` | `#3ad13a` | Active / on-air indicator |
| `HOT` | `#ff5252` | Alerts, lockout |
| `GOLD` | `#ffd24a` | Priority star |

Meter range: `METER_MIN = -100.0` / `METER_MAX = 0.0` dBFS.

### Widget Layout

- **Banner** — App title and status line
- **dBFS meter** — Tk `Canvas` bar with gradient fill and threshold marker
- **Control panel** — Start/Stop, squelch mode, gain sliders, record toggle
- **Tag filter chips** — Colored toggle buttons per tag
- **Channel Treeview** — Main channel list (see columns below)
- **Log pane** — Scrolling event log
- **Transcript pane** — STT transcript output

### Channel List

Columns: **On** (☑/☐), **★** (priority), **Freq**, **Channel**, **Tag**, **Status**,
**Last active**.

`_on_tree_click` dispatches based on which column was clicked — toggling enable, setting
priority, or initiating a goto.

### Refresh & Backend Selector

`_refresh` fires every ~120 ms via `root.after()`. It reads `scanner.ui` and updates all
widgets. No backend I/O occurs on the GUI thread.

**Backend selector** — Radio buttons switch between RTL-SDR and GQRX. `_set_backend(kind)`
performs a live swap: stops the engine, closes the old backend, opens the new one, and
restarts scanning.

---

## §5 Heatmap (heatmap.py)

### Architecture

```
HeatmapTab (GUI)
    │
    ▼
HeatmapRecorder (background thread, action queue: start/stop/pause/resume/dump)
    │
    ▼
SweepSource (base class)
    ├── RtlSweepSource (real hardware, borrows dongle via SdrShareCoordinator)
    └── FakeSweepSource (synthetic data, test_heatmap.py only)
```

### SweepConfig

Defines the sweep parameters: `f_start`, `f_stop`, `samp_rate`, `bin_hz` (default 3000),
`gain`, `crop` (0.20 — discard 20% of band edges), `device`, `duration_s` (30),
`margin_db` (8), `iq_mode`, `colormap`. Derives FFT geometry and tiles the requested
range into ~2 MHz windows via `plan_range_windows`.

### Sweep DSP Pipeline

Per hop: capture IQ → Welch PSD → crop edges → null DC bin → scatter power values onto
a global frequency-bin grid → produces one dBFS row per complete sweep.

### Persistence — HeatmapDB

SQLite database with WAL journaling for concurrent read/write.

| Table | Contents |
|-------|----------|
| `sessions` | Sweep config, start/stop times, metadata |
| `power` | Quantized `uint8` power values per bin per sweep |
| `activity` | Detected activity events |
| `iq_dumps` | Raw IQ snapshots for post-hoc analysis |

`load_matrix()` reconstructs the exact heatmap matrix from stored `uint8` data.

### Activity Detection

Per-bin min-hold-with-leak establishes a noise floor estimate. Each row is compared
against `floor + margin_db`. Contiguous active bins are clustered into activity events.

### Rendering

- **Live** — `HeatmapView` renders a Tk waterfall using `PhotoImage` with custom
  colormaps and auto-range scaling
- **Offline** — `render_session_png()` produces a matplotlib figure for export

### Shared Dongle — SdrShareCoordinator (rf_hotscan.py L2681–2720)

`begin_external_use(label)` pauses the scanner and quiesces audio, then returns a
connected `RtlBackend`. `end_external_use()` resumes scanning. This is how the heatmap
recorder borrows the dongle without conflicting with the scanner.

### Headless / CLI

`heatmap.py` is also a standalone CLI with subcommands:

| Command | Purpose |
|---------|---------|
| `scan` | Run a headless sweep session |
| `render` | Render a stored session to PNG |
| `list` | List recorded sessions |
| `info` | Show session metadata |

The `run_scan()` function provides a programmatic API for the same workflow.

### Synthetic Source

`FakeSweepSource` generates deterministic test data. It is used only by
`test_heatmap.py` and is never available in the GUI.

---

## Invariants

These architectural invariants must be preserved across all changes:

1. **Only the engine thread performs backend I/O.** The GUI communicates with the
   scanner exclusively through `set_cfg`, `request` (action queue), and `Events`.
   Never call backend methods from the Tk main loop.

2. **Per-channel state is keyed by CID, not frequency.** Duplicate-frequency bookmarks
   are valid. Using raw frequency as a key will silently merge distinct channels.

3. **Keep the scan hot-path lean.** One tune + one strength read per hop in sequential
   mode; one `sweep()` call in batch mode. Do not add per-channel overhead.

4. **The scan thread never dies silently.** Every code path in `_loop` is wrapped in a
   broad `except` with `traceback` logging. If you add new code paths, maintain this
   guarantee.

5. **Detection is level-only.** There is no tone gating, CTCSS decode, or other
   sub-audible signaling. Squelch decisions are purely amplitude-based.
