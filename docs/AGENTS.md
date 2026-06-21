# RF HotScan — Notes for AI Agents

This file orients an AI coding agent (or any new contributor) working on RF HotScan. Read [`ARCHITECTURE.md`](ARCHITECTURE.md) first for the design; this file is about *how to work on it productively and safely*.

## What this project is

A modular Python/Tkinter application that acts as a direct RTL-SDR bookmark scanner, audio recorder, and speech-to-text transcriber, with a legacy TCP fallback mode to drive GQRX. 

## Ground truth: read these signals, don't guess

- **The verbose log** is your primary observability tool:
  `./scanner.log` (next to the app). It records every frequency hop
  (`HOP #n freq tag s=<dBFS> thr=<dBFS> ** ACTIVE **`), state transitions
  (`STATE x -> y`), holds, action processing, squelch read-backs, and full tracebacks on error.
  Tail it while reproducing:
  ```sh
  tail -f scanner.log    # in the app dir
  ```
  A robust pattern for headless observation: record the current line count, run/repro, then read only the new lines.
- **The GUI log pane** shows the same events at INFO level (no per-hop spam).

## Verifying behavior WITHOUT clicking the GUI

The engine (`Scanner`) can be driven headlessly — this is the fastest way to reproduce and confirm a fix. The GUI just calls these same methods.

```python
import time, rf_hotscan as g              # run from the repo dir
tags, chans = g.load_bookmarks(g.BOOKMARKS)
for i, c in enumerate(chans):             # the GUI assigns cid; do the same
    c["cid"] = i
bands = g.cluster_bands(chans)
# Swap with rtl_backend.RtlBackend() to test the direct backend
client = g.GqrxClient() 
sc = g.Scanner(client, tags, chans, bands)
sc.request("reconnect"); time.sleep(0.6)  # connect
sc.set_cfg(enabled_tags={"LBPD"}, squelch_mode="global", global_sql=-30.0)
sc.run.set()                              # == pressing Start
time.sleep(3)
print(sc.snapshot_ui()["state"], sc._hops)
sc.run.clear(); sc.alive = False; client.close()
```

You can also build the GUI headlessly to test widget logic without a long-lived window:

```python
import tkinter as tk, rf_hotscan as g
root = tk.Tk(); gui = g.ScannerGUI(root); root.update_idletasks()
gui._toggle_tag("LBPD"); root.update_idletasks()
print(len(gui.tree.get_children("")))     # visible rows
root.destroy()
```

To smoke-test the full window without it lingering, schedule a destroy:
`root.after(2500, root.destroy); root.mainloop()`.

---

## Hard invariants — do not break these

1. **Only the engine thread touches the backend or socket.** From the GUI, mutate state via `scanner.set_cfg(...)`, fire work via `scanner.request(name, **kw)`, and use the `run`/`skip` Events.
2. **Key per-channel state by `cid`, never by frequency.** Duplicate-frequency bookmarks exist. Frequency keys silently collide.
3. **Keep the scan hot-path lean:** one tune + one power/strength read per hop. Put verification steps at transitions, not in the hot loop.
4. **The scan thread must never die silently.** Keep the broad `except` in `_loop()` that logs a traceback and continues.
5. **Detection is level-only.** No tone (CTCSS/DPL) gating is available via the hardware sweeps or remote protocols. Tones live in channel names as reference only.
6. **`cfg`/`ui` access goes through the lock** (`set_cfg`/`get_cfg`/`_set_ui`/`snapshot_ui`). Don't read or mutate those dicts directly across threads.

---

## Backends & RTL-SDR Invariants

- **`RtlBackend`** (in `rtl_backend.py`) — direct dongle via pyrtlsdr. Implements `sweep()` (so `Scanner._sweep_pass` runs, ~35–77 ch/s) and streams audio on `on_hold` via `FMDemod`.
- **`GqrxClient`** (in `rf_hotscan.py`) — GQRX remote client fallback.
- **Run from the project `.venv`** (`.venv/bin/python rf_hotscan.py`) to run with direct RTL support.

### Shared-dongle invariant (CRITICAL)
The RTL dongle is a **single-owner USB device.**
- Only one of {GQRX running, RtlBackend connected, heatmap sweeping} may hold the dongle at once.
- **This is enforced** by a process-wide owner in `rtl_backend.py`: `connect()` calls `_acquire_dongle(self, owner_label)` and `close()` releases it; a second owner trying to open the dongle raises `DongleBusy`.
- **Borrow + auto-pause (in-app coordination).** When both tabs live in one process, a Heatmap capture does NOT open a second dongle: `SdrShareCoordinator` (in `rf_hotscan.py`) lends the Scanner's already-connected `RtlBackend` to the heatmap and pauses the Scanner's scan + audio (`run.clear()` + `on_resume()`) for the duration of the capture.
- **Never call `cancel_read_async()` on an idle dongle** — it corrupts the next `center_freq` (LIBUSB_ERROR_IO). Guard with the `_streaming` flag.

### Audio squelch-gating + transmission recording (RTL only)
- `RtlBackend.on_hold(ch, thr)` receives the channel dict + its squelch threshold. In `listen`'s `iq_cb`, `open_ = live_power >= thr - SQUELCH_HYST` is the single "signal present" decision.
- `recorder.py`: `WavRecorder` writes mono WAVs with CLEAN CUTS — only `open_` blocks are written, so the WAV ends immediately at the signal drop.
- GQRX backend has no audio sample access, so recording/playback/STT features are disabled when GQRX backend is selected.
