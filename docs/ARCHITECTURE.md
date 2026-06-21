# RF HotScan — Architecture

RF HotScan's GQRX scanner core is a single Python file (`rf_hotscan.py`, standard
library only) organized into four layers, described first. Optional modules add
the direct RTL-SDR backend (`rtl_backend.py`), transmission recording
(`recorder.py`), a shared clock (`clock.py`), and the RF-activity heatmap
(`heatmap.py`, §5 — a second GUI tab). This document explains each layer, the
threading model, and the key algorithms, so a human or an AI agent can extend it
safely.

```
┌───────────────────────────────────────────────────────────────┐
│  ScannerGUI (Tkinter, main thread)                            │
│  - builds widgets, paints state every ~120 ms via root.after  │
│  - never touches the socket directly                          │
│  - sends intents to the engine: cfg updates + action requests │
└───────────────▲───────────────────────────┬──────────────────┘
                │ snapshot_ui() (locked)     │ set_cfg(), request()
                │ logq, last_active          │ run/skip Events
┌───────────────┴───────────────────────────▼──────────────────┐
│  Scanner (engine, ONE background thread)                      │
│  - owns the scan loop + all GQRX socket I/O                   │
│  - state machine: STOPPED/SCANNING/HOLDING/CALIBRATING/DISC.  │
│  - processes an action queue (noise_floor, reconnect, ...)    │
└───────────────────────────────┬──────────────────────────────┘
                                 │ GqrxClient methods
┌────────────────────────────────▼─────────────────────────────┐
│  GqrxClient (thin rigctl/TCP wrapper, thread-safe via a lock) │
└────────────────────────────────┬─────────────────────────────┘
                                 │ TCP 127.0.0.1:7356
┌────────────────────────────────▼─────────────────────────────┐
│  GQRX remote control (Hamlib rigctld-compatible)             │
└───────────────────────────────────────────────────────────────┘
```

Plus a small set of **module-level pure functions** for parsing and color math.

---

## 1. Bookmark parsing & bands (module-level functions)

- `load_bookmarks(path)` → `(tags, channels)`.
  - `tags`: `{tag_name: "#rrggbb"}`.
  - `channels`: list of dicts `{freq (Hz, int), name, mode ("FM"/"AM"/"WFM"),
    bw (Hz), tag}`.
  - `map_mode()` collapses GQRX's "Narrow FM" etc. to the remote mode token.
- `cluster_bands(channels, gap=5 MHz)` → list of `(lo_hz, hi_hz)` band spans,
  formed by splitting the sorted frequency list wherever the gap exceeds 5 MHz.
  Used to scope noise-floor sampling and to add extra settle time on big hops.
- `band_index(freq, bands)` → which band a frequency belongs to.
- `luminance()` / `contrast_fg()` choose black/white text over a tag color.

The GUI assigns each channel a stable **`cid`** (its index) right after loading.
**Tree rows and per-channel state are keyed by `cid`, never by frequency**,
because duplicate-frequency bookmarks exist (two uses on one Hz). Keying by
frequency silently collides; this was a real bug and the invariant must hold.

---

## 2. GqrxClient

A minimal wrapper over a TCP socket speaking the rigctl line protocol. Each
method sends one command and reads the expected number of reply lines
(`_readline` buffers on `\n`). A `threading.Lock` serializes access so a stray
call from anywhere can't interleave bytes. Methods: `set_freq/get_freq`,
`set_mode/get_mode`, `strength` (`l STRENGTH`), `get_sql/set_sql`,
`get_af/set_af`, `connect/close`, `connected`.

**Invariant:** in normal operation only the engine thread calls these. The GUI
issues `request(...)` actions that the engine executes on its own thread.

---

## 3. Scanner (engine)

One daemon thread runs `_loop()`. All GQRX I/O happens here.

### Config & state

- `self.cfg` — a dict guarded by `self.lock`, mutated via `set_cfg(**kw)` and
  read via `get_cfg(key)` (returns a copy for sets). Keys: `enabled_tags`,
  `lockout` (freqs), `disabled_cids`, `priority_freqs`, `squelch_mode`
  (`auto`/`global`), `global_sql`, `auto_margin`, `settle_ms`, `hold_s`,
  `priority_interval`.
- `self.ui` — a dict guarded by the same lock; the GUI reads it with
  `snapshot_ui()`. Keys: `state`, `cur` (current channel dict), `strength`,
  `thresh`, `msg`, `gqrx_sql`, `af`.
- `self.run` / `self.skip` — `threading.Event`s for start/pause and skip.
- `self.logq` — events for the GUI log panel; also written to the file logger.

### The scan loop

For each channel in `active_list()`:
1. `_tune(ch)` — set mode (only if changed), set frequency, set GQRX squelch to
   the effective threshold (only if changed), sleep `settle_ms` (+150 ms extra
   when the band changed, to allow the hardware to re-center).
2. Read `strength()`.
3. If `strength >= effective_threshold`, `_hold(ch)`.

`active_list()` applies the tag filter, lockout set, and `disabled_cids`, then
**dedupes by frequency** (a level-only scanner can't distinguish two same-Hz
bookmarks, so each frequency is visited once per sweep).

`effective_threshold(freq)`:
- *global* mode → `global_sql`.
- *auto* mode → `band_floor[band] + auto_margin` (falls back to `global_sql` if
  the floor hasn't been measured yet).

### Hold

`_hold(ch)` parks on the channel, polling `strength()` ~12 Hz. It tracks the
last time the signal was above threshold; when that exceeds `hold_s`, it
releases. `skip` breaks out immediately. While parked, priority frequencies are
peeked on `priority_interval` and pre-empt if active.

### Action queue

The GUI never touches the socket. It calls `scanner.request(name, **kw)` which
enqueues an action drained by the engine in `_drain_actions()`:
- `reconnect` — (re)open the socket; on success, read back `SQL` and `AF` into
  `ui` so the GUI reflects GQRX immediately.
- `refresh_sql` — push the effective squelch for the current frequency and
  read it back (verified).
- `set_af` — set audio gain.
- `goto` — tune a specific channel.
- `noise_floor` — run the calibration sweep.

### Two-way squelch sync

`_maybe_poll_sql()` runs on a throttle (≤ every 0.7 s, in both the stopped and
scanning paths). It reads GQRX's `SQL` into `ui["gqrx_sql"]`. If the value
differs from the last value RF HotScan itself set (`self._last_sql`), the change
came from GQRX (or another client); in *global* mode it adopts the new value
into `global_sql`. The GUI, seeing `ui["gqrx_sql"]`, moves the slider — guarded
by `_suppress_push` so the programmatic move doesn't echo back and form a loop.

### Auto-Noise-Floor calibration

`_measure_noise_floor()`:
1. Remembers whether scanning was active, then clears `run` (pauses scanning)
   and sets state `CALIBRATING`.
2. For each band, generates candidate frequencies across the band span at 25 kHz
   steps, **excluding** anything within 15 kHz of a real bookmark (so it samples
   *empty* spectrum), and caps to ~15 spread samples.
3. Tunes each, reads strength, and pushes live progress to `ui` (the meter and
   banner move so the user sees RF HotScan driving GQRX).
4. Stores the **median** per band as the noise floor. Squelch in *auto* mode is
   then `floor + auto_margin`.
5. Restores state and resumes scanning if it was running.

### Resilience

The whole loop body is wrapped so a socket error → `_handle_disconnect()` and
any other exception is logged with a traceback and the loop continues. The scan
thread must never die silently (an earlier bug; keep this guarantee).

---

## 4. ScannerGUI (Tkinter)

- Builds a dark `clam`-themed UI: banner (current channel + state), dBFS meter
  (`Canvas`), control panel (transport, squelch, audio, timing, priority), tag
  filter chips with **All/None**, the channel `Treeview`, and the log pane.
- `_refresh()` runs every ~120 ms via `root.after`: snapshots `ui`, repaints the
  banner/meter/tree, updates the connection dot, syncs the squelch slider from
  `gqrx_sql`, and initializes the audio-gain slider from `af` once.
- **Channel list columns:** `On` (☑/☐ enable toggle), `★` (priority), Freq,
  Channel, Tag, Status, Last active. `_on_tree_click` dispatches on the clicked
  column (`#1` = enable, `#2` = priority). Rows are styled by tag color, or
  muted when locked/disabled.
- **Settings persistence** (`scanner_settings.json`): sliders, squelch mode,
  enabled tags, lockouts, and **disabled channels by a stable `freq:name`
  signature** (so the selection survives bookmark edits that would shift `cid`s).

---

## Backends (GQRX remote vs direct RTL-SDR)

`Scanner.client` is a **backend** — any object implementing the method set the
engine calls (`connect/close/connected`, `set_mode/get_mode`,
`set_freq/get_freq`, `strength`, `get_sql/set_sql`, `get_af/set_af`,
`get_lna/set_lna`), with optional `on_hold/on_resume` (audio on park) and
`sweep(freqs) -> ({freq: dbfs}, nwin)` (channelized fast path). The GUI BACKEND
selector swaps it live.

- **`GqrxClient`** — GQRX over rigctl TCP (stdlib only). No `sweep`; the engine
  uses the per-channel `_tune`+`strength` loop with the ~350 ms meter dwell.
- **`RtlBackend`** (`rtl_backend.py`) — owns the dongle via pyrtlsdr. Provides
  `sweep()`, so `Scanner._sweep_pass()` reads a whole sweep from a few ~2 MHz
  captures (~62–74 ch/s). On `on_hold` it streams gapless NBFM audio
  (`FMDemod` + sounddevice callback). Level uses `channel_power_dbfs`, the same
  measure for sweep / per-channel reads / live hold level.

The engine and GUI are otherwise backend-agnostic (~70% reuse as the spike
predicted). See `docs/AGENTS.md` → *Backends & RTL-SDR* for the shared-dongle
invariant (GQRX / RtlBackend / heatmap are mutually exclusive owners) and the
`cancel_read_async`-on-idle pitfall. Single ownership is **enforced**: a
process-wide owner in `rtl_backend.py` raises `DongleBusy` if a second owner
opens the dongle.

---

## 5. Heatmap (`heatmap.py`) — a second tab

A `ttk.Notebook` hosts two tabs: **Scanner** (everything above) and **Heatmap**.
The heatmap is a separate, frequency-range (not bookmark) feature: it sweeps a
start→stop band over a time window into a time × frequency activity heatmap.

```
HeatmapTab (Tkinter)  ──▶ HeatmapRecorder (engine thread) ──▶ SweepSource
  live waterfall          owns the sweep loop + HeatmapDB      ├─ FakeSweepSource (synthetic IQ)
  + detected-activity      (mirrors Scanner's threading)        └─ RtlSweepSource → RtlBackend.capture_iq
```

- **Sweep DSP:** `SweepConfig` derives FFT geometry and tiles the range into
  ~2 MHz windows (`plan_range_windows`); each window's IQ → Welch PSD
  (`window_power_dbfs`) → crop edges, null the DC bin, scatter onto a global bin
  grid → one dBFS row per sweep.
- **Persistence (`HeatmapDB`, `heatmap.sqlite`, WAL):** one quantised uint8 power
  row per sweep (`ref`/`scale`/`t_unix`/`t_dur_ms`); `sessions` / `power` /
  `activity` / `iq_dumps`. `load_matrix()` rebuilds the exact heatmap offline.
- **Activity:** per-bin min-hold-with-leak floor → `row > floor+margin` mask →
  contiguous active bins clustered into detected ranges (duty %).
- **Rendering:** live pure-Tk `HeatmapView` (PhotoImage waterfall, custom
  colormaps, auto-range — no matplotlib); offline `render_session_png` uses
  matplotlib (lazy import) for re-render / PNG export.
- **Shared dongle:** `SdrShareCoordinator` lends the Scanner's connected
  `RtlBackend` to the heatmap (borrow mode) and pauses/resumes the Scanner around
  each capture — keyed to capture execution, not tab focus. Borrow never closes
  the borrowed backend; FFT geometry auto-syncs to its sample rate.
- **Headless:** the engine is GUI-free — `python -m heatmap scan|render|list|info`
  (JSON for agents) and `run_scan(...)`. Tested via `test_heatmap.py` on the
  `FakeSweepSource` (no dongle).

Heavy deps (numpy/scipy/matplotlib/pyrtlsdr) are lazily imported, so importing
`heatmap` — and launching the app — stays cheap and the GQRX path stays
stdlib-only.

---

## Invariants to preserve

1. **Only the engine thread performs socket I/O.** GUI → engine via
   `set_cfg()` / `request()` / Events.
2. **Per-channel state is keyed by `cid`, not frequency.** Duplicate
   frequencies exist.
3. **Keep the scan hot-path lean:** `set_freq` + `strength` per hop. Read-backs
   and verification happen at transitions, not every hop.
4. **The scan thread never dies silently** — broad `except` with traceback
   logging.
5. **Detection is level-only.** No tone (CTCSS/DPL) gating is available via the
   remote protocol; don't assume otherwise.
