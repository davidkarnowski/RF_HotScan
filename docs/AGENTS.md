# RF HotScan — Notes for AI Agents

This file orients an AI coding agent (or any new contributor) working on RF
HotScan. Read [`ARCHITECTURE.md`](ARCHITECTURE.md) first for the design; this
file is about *how to work on it productively and safely*.

## What this project is

A single-file Tkinter GUI (`rf_hotscan.py`, stdlib only) that turns GQRX into a
bookmark scanner by driving GQRX's rigctl-style remote-control TCP server. There
is no build step and no package install.

## Ground truth: read these signals, don't guess

- **The verbose log** is your primary observability tool:
  `./scanner.log` (next to the app). It records every frequency hop
  (`HOP #n freq tag s=<dBFS> thr=<dBFS> ** ACTIVE **`), state transitions
  (`STATE x -> y`), holds, action processing, squelch read-backs (`VERIFY`,
  `Squelch set ... read-back ... OK/MISMATCH`), and full tracebacks on error.
  Tail it while reproducing:
  ```sh
  tail -f scanner.log    # in the app dir
  ```
  A robust pattern for headless observation: record the current line count,
  run/repro, then read only the new lines.
- **The GUI log pane** shows the same events at INFO level (no per-hop spam).

## Verifying behavior WITHOUT clicking the GUI

The engine (`Scanner`) is fully drivable headless — this is the fastest way to
reproduce and confirm a fix. The GUI just calls these same methods.

```python
import time, rf_hotscan as g              # run from the repo dir
tags, chans = g.load_bookmarks(g.BOOKMARKS)
for i, c in enumerate(chans):             # the GUI assigns cid; do the same
    c["cid"] = i
bands = g.cluster_bands(chans)
client = g.GqrxClient()
sc = g.Scanner(client, tags, chans, bands)
sc.request("reconnect"); time.sleep(0.6)  # connect
sc.set_cfg(enabled_tags={"LBPD"}, squelch_mode="global", global_sql=-30.0)
sc.run.set()                              # == pressing Start
time.sleep(3)
print(sc.snapshot_ui()["state"], sc._hops)
sc.run.clear(); sc.alive = False; client.close()
```

You can also build the GUI headless to test widget logic without a long-lived
window:

```python
import tkinter as tk, rf_hotscan as g
root = tk.Tk(); gui = g.ScannerGUI(root); root.update_idletasks()
gui._toggle_tag("LBPD"); root.update_idletasks()
print(len(gui.tree.get_children("")))     # visible rows
root.destroy()
```

To smoke-test the full window without it lingering, schedule a destroy:
`root.after(2500, root.destroy); root.mainloop()`.

## Probing GQRX directly

GQRX must have **Tools → Remote control** enabled. To discover/confirm protocol
capabilities, open a raw socket to `127.0.0.1:7356` and send commands. Useful:
`l ?` and `L ?` list supported get/set levels; `_` returns the GQRX version.
Known levels on 2.17.x: `STRENGTH` (read), `SQL`, `AF`, `LNA_GAIN`. There is no
remote command to read GQRX's bookmarks — RF HotScan parses the CSV file.

## Hard invariants — do not break these

1. **Only the engine thread touches the socket.** From the GUI, mutate state via
   `scanner.set_cfg(...)`, fire work via `scanner.request(name, **kw)`, and use
   the `run`/`skip` Events. If you need a new GQRX operation from the GUI, add an
   **action** handled in `_drain_actions()`, don't call the client from the GUI.
2. **Key per-channel state by `cid`, never by frequency.** Duplicate-frequency
   bookmarks exist (e.g. 153.800 and 935.225 each appear twice). Frequency keys
   silently collide.
3. **Keep the scan hot-path lean:** one `set_freq` + one `strength` per hop. Put
   read-backs/verification at transitions (hold entry, goto, squelch change),
   not in the per-hop loop, or you slow the sweep.
4. **The scan thread must never die silently.** Keep the broad `except` in
   `_loop()` that logs a traceback and continues.
5. **Detection is level-only.** The remote protocol cannot gate on CTCSS/DPL
   tone. Tones live in channel names as reference only.
6. **`cfg`/`ui` access goes through the lock** (`set_cfg`/`get_cfg`/`_set_ui`/
   `snapshot_ui`). Don't read or mutate those dicts directly across threads.

## Gotchas

- `get_mode` (`m`) returns **two** lines (mode then passband). `_cmd(..., 2)`.
- `set_mode` is `M <mode> <passband_hz>`; passband 0 keeps the default.
- AF gain clamps to **-80..+50 dB**; SQL/STRENGTH are dBFS (~-100 noise .. 0).
- Programmatically moving a slider can trigger its `command`; the GUI guards
  GQRX→GUI syncs with `_suppress_push` to avoid feedback loops. Preserve that.
- Disabled channels persist by `freq:name` signature, not `cid`, so the
  selection survives bookmark edits. Keep that mapping in `_load/_save_settings`.
- Big frequency hops (across bands) re-center the SDR hardware; `_tune` adds
  ~150 ms settle when the band index changes.

## Backends & RTL-SDR (multi-agent coordination)

There are now TWO ways the engine can talk to hardware, selected in the GUI
(BACKEND section). **The Scanner holds `self.client` and calls a fixed set of
methods on it; anything implementing them is a backend:**

`connect/close/connected, set_mode/get_mode, set_freq/get_freq, strength,
get_sql/set_sql, get_af/set_af, get_lna/set_lna`, optional `on_hold(freq)/
on_resume()` (audio on park), and optional `sweep(freqs)->({freq:dbfs}, nwin)`
(channelized fast path). `recommended_settle_ms` hints the per-channel dwell.

- **`GqrxClient`** (in `rf_hotscan.py`) — the GQRX-remote backend. Stdlib only.
- **`RtlBackend`** (in `rtl_backend.py`) — direct dongle via pyrtlsdr. Implements
  `sweep()` (so `Scanner._sweep_pass` runs, ~62–74 ch/s) and produces audio on
  `on_hold` via the gapless streaming `FMDemod`.

**Run from the project `.venv`** (`.venv/bin/python rf_hotscan.py`) to get RTL +
tkinter; bare `python3` runs the GQRX path only (`RTL_AVAILABLE=False`).

### Shared-dongle invariant (READ if you touch RTL or heatmap)
The RTL dongle is a **single-owner USB device.** GQRX, `RtlBackend`, and the
**heatmap** (`heatmap.py`, `RtlSweepSource` / `RtlBackend.capture_iq`) can NOT
own it at the same time. Rules:
- Only one of {GQRX running, RtlBackend connected, heatmap sweeping} may hold the
  dongle at once. Switching to RTL offers to quit GQRX (`gqrx_quit()`).
- **This is now ENFORCED** by a process-wide owner in `rtl_backend.py`:
  `connect()` calls `_acquire_dongle(self, owner_label)` and `close()` releases;
  a second owner opening the dongle raises `DongleBusy` (clear message) instead
  of a corrupted next tune. `dongle_owner()` returns the current owner label.
  Re-acquire by the *same* instance is allowed (idempotent reconnect).
- **Borrow + auto-pause (in-app coordination).** When both tabs live in one
  process, a Heatmap capture does NOT open a second dongle: `SdrShareCoordinator`
  (in `rf_hotscan.py`) lends the Scanner's already-connected `RtlBackend` to the
  heatmap and pauses the Scanner's scan + audio (`run.clear()` + `on_resume()`)
  for the duration of the capture, then resumes it. `RtlSweepSource(cfg,
  backend=…)` runs in *borrow mode* (never closes the borrowed backend). Pause/
  resume is keyed to **capture execution, not tab focus**. If the Scanner is on
  GQRX (coordinator returns `None`), the heatmap opens its own dongle.
- **Never call `cancel_read_async()` on an idle dongle** — it corrupts the next
  `center_freq` (LIBUSB_ERROR_IO). Guard with the `_streaming` flag.
- Do all device access through the owner's lock; never do a synchronous
  `read_samples` while an async reader is active (the borrow path stops the
  Scanner's audio first, so `capture_iq` is the only reader).

### dBFS scale convention
`rtl_backend.channel_power_dbfs(iq, fs, offset_hz, bw)` is the ONE level measure
used by sweep detection, per-channel reads, AND the live hold level, so a squelch
threshold / noise floor means the same thing in all three. If you add another
level source (e.g. the heatmap's `window_power_dbfs`), keep it on a comparable
scale or document the offset — thresholds calibrated on one don't transfer to a
differently-scaled one.

### Time base — use `clock.py` everywhere
`clock.py` (stdlib) is the single time source shared by the scanner, heatmap, and
recorder: `now_unix()` (UTC epoch float), `mono()` (durations), `utc_iso(t)` /
`local_iso(t)` / `now_iso()`, `file_stamp(t)` (compact UTC for filenames). Rule:
**persist UTC epoch; derive ISO for display.** Durations come from sample counts
or `mono()` deltas, never from subtracting two wall-clock reads. The heatmap
stamps each `power` frame `t_unix` (UTC epoch, capture start) + `t_dur_ms` (span)
and adds `iso` to `emit_event`; keep that convention.

### Audio squelch-gating + transmission recording (RTL only)
- `RtlBackend.on_hold(ch, thr)` receives the channel dict + its squelch threshold.
  In `listen`'s `iq_cb`, `open_ = live_power >= thr - SQUELCH_HYST` is the single
  "signal present" decision. With `mute_squelch`, closed blocks output silence
  (no static on the hold-after-loss tail).
- `recorder.py`: `WavRecorder` writes 48 kHz/16-bit mono WAVs with CLEAN CUTS —
  only `open_` blocks are written, so the WAV ends at the signal drop. Metadata
  goes to `RecordingsDB` (`recordings.sqlite`, same conventions as `HeatmapDB`) +
  one `recordings.events.jsonl` line; everything lives in a `recordings/` subdir
  next to the app (`recorder.APPDIR`), not in `~/.config/gqrx`.
  Sample-accurate stop time = `start + n_frames/48000`. A future playback panel
  reads `RecordingsDB.list()/get()` and plays `wav_path`.
- GQRX backend has no audio samples → no `on_hold`, no recording (control hidden).

### Heatmap (`heatmap.py`)
A second app tab and a standalone module. It sweeps a contiguous start→stop range
over time into a time × frequency **activity** heatmap (the 2026 `rtl_power` +
`heatmap.py`). Independent of the bookmark/channel model.
- **Pipeline:** `SweepConfig` (range/FFT geometry) → a `SweepSource`
  (`FakeSweepSource` synthetic, or `RtlSweepSource` over `RtlBackend.capture_iq`)
  → per-window Welch FFT → crop/DC-null/stitch → one dBFS row per sweep.
  `HeatmapRecorder` is the engine thread (same shape as `Scanner`: lock-guarded
  `ui`, `rowq`/`logq`/`actions` queues, broad-except never-die loop).
- **Persistence:** `HeatmapDB` (`heatmap.sqlite`, WAL) stores one **quantised
  uint8 power row per sweep** (+ per-sweep `ref/scale`, `t_unix`, `t_dur_ms`);
  `load_matrix()` reconstructs the exact heatmap. `sessions` / `power` /
  `activity` / `iq_dumps` tables.
- **Detection:** per-bin min-hold-with-leak floor; `row > floor + margin` →
  active; contiguous active bins cluster into detected ranges (duty %).
- **Rendering:** live pure-Tk `HeatmapView` waterfall (no matplotlib needed);
  offline `render_session_png` / `_draw_heatmap_fig` use matplotlib (lazy).
- **Scale caveat:** `window_power_dbfs` is a per-FFT-bin PSD — a DIFFERENT scale
  from `channel_power_dbfs`. Don't transfer a scanner squelch threshold to it;
  the heatmap only compares to its own floor/auto-range.
- **Headless / agent:** `python -m heatmap scan|render|list|info` (machine JSON
  on stdout via `--json`) and `run_scan(...)`. All operational defaults are the
  real dongle (`--device 0`). Errors (e.g. `DongleBusy`) come back as structured
  JSON, not tracebacks.
- **Synthetic source is TESTING ONLY.** `FakeSweepSource` / `--device fake`
  fabricates a spectrum with no hardware — for `test_heatmap.py` and no-dongle CI
  dry runs only. It is not in the GUI Device dropdown and is never an operational
  source. Keep it that way.
- **Verify headless (no dongle):** `.venv/bin/python test_heatmap.py` — unit
  (tiling/quantise), DB round-trip + activity, dongle-coordination, borrow
  (mock backend), and a GUI smoke. All pass on the `FakeSweepSource`.
- **Shared dongle:** see the *Shared-dongle invariant* above (borrow + auto-pause,
  enforced single owner). Heatmap timestamps use `clock.py`.

## Things that are intentionally NOT done (GQRX backend)

- No tone (CTCSS/DPL) squelch (GQRX remote-protocol limitation; the RTL backend
  *could* add it since it owns the audio — that's a planned phase).
- No P25/DMR/NXDN demodulation.
- No writing to GQRX's bookmark file — RF HotScan reads it, never edits it.
