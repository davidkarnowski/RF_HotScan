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
  `~/.config/gqrx/scanner.log`. It records every frequency hop
  (`HOP #n freq tag s=<dBFS> thr=<dBFS> ** ACTIVE **`), state transitions
  (`STATE x -> y`), holds, action processing, squelch read-backs (`VERIFY`,
  `Squelch set ... read-back ... OK/MISMATCH`), and full tracebacks on error.
  Tail it while reproducing:
  ```sh
  tail -f ~/.config/gqrx/scanner.log
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

## Things that are intentionally NOT done

- No tone (CTCSS/DPL) squelch (protocol limitation).
- No P25/DMR/NXDN demodulation (GQRX can't; such channels are documented only).
- No writing to GQRX's bookmark file — RF HotScan reads it, never edits it.
