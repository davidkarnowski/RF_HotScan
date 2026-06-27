# RF HotScan

**A direct RTL-SDR bookmark scanner with transmission recording, real-time
speech-to-text, and spectrum heatmapping.**

Python/Tkinter SDR scanner app that uses direct RTL-SDR hardware
(via pyrtlsdr / librtlsdr) to sweep frequency bookmarks, detect
transmissions, record clean audio, transcribe in real-time, and visualize
RF activity on a time × frequency heatmap. Also retains a legacy GQRX
remote-control fallback. Single-file core, no build step, no package
install required for the GQRX path. macOS (Apple Silicon) primary,
Linux/Ubuntu secondary, Windows out of scope.

> **Status:** working tool, actively developed. Primary backend: RTL-SDR
> Blog dongle via direct USB. GQRX remote control retained as fallback.

---

## At a glance

- **Scans your bookmarks** grouped by acronym tags. Toggle tags on/off to
  include or exclude them live. Per-channel enable/disable for fine-grained
  control.

- **Two backends** — *RTL-SDR direct* (default): channelized FFT sweep at
  ~35–77 ch/s with NBFM demod and gapless audio. *GQRX remote* (legacy):
  rigctl TCP control where GQRX handles all audio.

- **Hold-after-loss** — parks on an active channel and resumes scanning a
  configurable number of seconds after the signal drops.

- **Priority channels** — flag channels with ★; they are checked on a
  regular interval even while parked on another channel and will pre-empt
  the held channel.

- **Lockout** — temporarily skip a chatty channel so it is not visited
  during sweeps.

- **Radio playhead** — editable frequency field in the banner with
  ▶ Play / ⏹ Stop and a ● LIVE indicator dot. Type a frequency in MHz,
  Hz, or with commas (~24–1766 MHz). Double-click any station row to
  tune + listen immediately. Last frequency persists across sessions.

- **Per-transmission recording + transcription** (RTL backend) — clean-cut
  WAV file per transmission with swappable STT engines:
  Parakeet-MLX (default, ~35× realtime), Voxtral Mini 3B,
  Whisper-MLX (local), OpenAI cloud (gpt-4o-mini-transcribe,
  gpt-4o-transcribe, whisper-1). Re-transcribe ↻ button per transcript
  row.

- **Auto-Noise-Floor** — samples empty frequencies, measures the noise
  floor per band, and sets squelch relative to live RF conditions.

- **Two-way squelch sync** (GQRX mode) — the global squelch slider
  mirrors GQRX's squelch in both directions.

- **Audio-gain and LNA-gain sliders** for real-time adjustment.

- **Live dBFS meter** with a draggable threshold marker, color-coded
  scanner state (SCANNING / HOLDING / CALIBRATING / DISCONNECTED), and a
  connection dot.

- **Verbose log** at `./scanner.log` for debugging — `tail -f` it during
  operation.

- **RF activity heatmap** (second tab) — time × frequency heatmap driven
  by direct RTL-SDR sweeps, backed by SQLite persistence, with activity
  detection, PNG export, and a headless CLI.

---

## How it works

### RTL fast path (`_sweep_pass`)

`plan_windows` tiles all active channel frequencies into ~2 MHz capture
windows. For each window the backend captures IQ samples, then
`channel_power_dbfs` runs a 127-tap FIR bandpass per channel and computes
mean |x|² → dB. All channels are measured in roughly 8–12 captures
(~1 second total). The sweep picks the strongest active channel, with
priority channels favored.

### GQRX slow path

Sequential tune → settle (350 ms) → `l STRENGTH` → compare against
threshold. A full pass through 77 channels takes ~30 seconds.

### Detection threshold

Both backends use `effective_threshold`:

- **Auto mode** — band noise floor + configurable margin.
- **Global mode** — the slider value is used as-is.

### Hold behavior

While parked on an active channel, the backend polls signal strength at
~80 ms intervals. The channel is released after `hold_s` seconds of
continuous silence. During hold, priority channels are still checked and
can pre-empt the held channel.

### Rigctl command table

| Command | Meaning |
|---|---|
| `F <Hz>` / `f` | set / get frequency |
| `M <mode> <passband>` / `m` | set / get demodulator mode |
| `l STRENGTH` | read current signal level (dBFS) |
| `l SQL` / `L SQL <dBFS>` | get / set squelch threshold |
| `l AF` / `L AF <dB>` | get / set audio (AF) gain |
| `c` | close |

> **Note:** Detection is level-only. No CTCSS/DPL tone gating.

For deeper architectural details see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
[docs/AGENTS.md](docs/AGENTS.md).

---

## Heatmap (RF activity over time)

The second tab provides a modern rtl_power-style heatmap driven by direct
RTL-SDR control.

- **Direct dongle sweep** — tiles the selected range into ~2 MHz windows,
  computes Welch PSD per window, and stitches one full per-bin power row
  per sweep. Configurable knobs: start/stop frequency, sample rate, FFT
  bin width, gain/AGC, PPM correction, dwell time, averaged blocks,
  crop %, hop overlap, duration, activity margin, and colormap.

- **SQLite persistence** (`heatmap.sqlite`) — quantized uint8 power rows,
  re-renderable offline for any stored session.

- **Activity detection** — per-bin min-hold-with-leak noise floor, margin
  mask, and contiguous active bins clustered into detected frequency
  ranges with duty-cycle percentage.

- **Live + polished views** — fast pure-Tk waterfall during capture;
  matplotlib re-render for stored sessions with colormaps, colorbar,
  MHz/time axes, pan/zoom, and PNG export.

- **Opt-in raw IQ dumps** — `.cf32` files per window, saved to `./iq/`.

### Shared dongle

The RTL dongle is single-owner: Scanner and Heatmap cannot hold it
simultaneously. Running a Heatmap capture borrows the Scanner's connected
backend and pauses the scan for the duration, then resumes afterward. This
is tied to capture execution, not tab focus. If the Scanner is using GQRX,
the Heatmap opens its own dongle directly.

### Headless / agent use

```sh
.venv/bin/python -m heatmap scan --start 88e6 --stop 108e6 --duration 10 --device 0 --gain auto --json
.venv/bin/python -m heatmap render <session_id> --png out.png
.venv/bin/python -m heatmap list
.venv/bin/python -m heatmap info <session_id>
```

`heatmap.run_scan(...)` is the Python API.

> **Testing only:** `--device fake` selects `FakeSweepSource` (synthetic
> spectrum, no dongle required). Used by `test_heatmap.py` and no-hardware
> CI. Not offered in the GUI.

---

## Requirements

- **Python 3.9+** with Tkinter
  - macOS: `brew install python-tk@3.14`
  - Debian/Ubuntu: `sudo apt install python3-tk`

- **RTL-SDR dongle** + `librtlsdr`
  - macOS: `brew install rtl-sdr`
  - Debian/Ubuntu: `sudo apt install rtl-sdr librtlsdr-dev`

- **GQRX-only path** — no pip packages required (stdlib only).

- **Direct RTL-SDR + Heatmap** — numpy, scipy, pyrtlsdr, sounddevice,
  matplotlib. See `requirements-rtl.txt` or run `./setup_rtl_env.sh`.

- **Speech-to-text** — see `requirements-stt.txt`.

---

## Setup

1. **Install system packages** (librtlsdr + Tkinter) — see Requirements
   above.

2. **Create the venv:**

   ```sh
   ./setup_rtl_env.sh       # auto-creates .venv, installs deps, patches pyrtlsdr
   # OR manually:
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements-rtl.txt
   ```

3. **(Optional) STT dependencies:**

   ```sh
   pip install -r requirements-stt.txt
   ```

4. **(Optional) API keys** in `.env` (git-ignored — never commit):

   ```env
   OPENAI_API_KEY=your-key
   HF_TOKEN=your-token
   ```

5. **Run:**

   ```sh
   .venv/bin/python rf_hotscan.py
   ```

### First-run workflow

1. Press **📈 Auto-Noise-Floor** (squelch mode defaults to Auto).
2. Pick tags with the chip buttons / **All** / **None**.
3. Press **▶ Scan**.

---

## Bookmarks

RF HotScan reads GQRX's bookmark file
(`~/.config/gqrx/bookmarks.csv`) directly. Falls back to
`./bookmarks.csv` if that path is absent.

### Bundled sample: a whole-city monitoring set

`examples/long_beach_bookmarks.csv` — city monitoring set for Long Beach,
CA. 14 tags, ~200 channels, spanning VHF/UHF (~145–935 MHz).

| Tag | Service | Tag | Service |
|---|---|---|---|
| `LBPD` | Police | `LBFD` | Fire / lifeguard / marine |
| `POLB` | Port of Long Beach | `USCG` | Coast Guard (harbor) |
| `LBC` | City gov / utilities / works | `LBT` | Transit |
| `LBUSD` | School District | `CSULB` | Cal State Long Beach |
| `LBCC` | City College | `LBMH` | Memorial paramedic base-hospital |
| `HAM` | Amateur radio (2m/1.25m/70cm) | `GMRS/FRS` | GMRS + FRS (462/467 MHz) |
| `NWS` | NOAA Weather Radio | `LASD` | LA County Sheriff |

Copy it into place:

```sh
cp examples/long_beach_bookmarks.csv ~/.config/gqrx/bookmarks.csv
```

### The bookmark file format

GQRX `bookmarks.csv` has two sections — a tag table and a channel table:

```
# Tag name          ;  color
LBPD                ; #1e90ff
LBFD                ; #ff0000
...

# Frequency ; Name                     ; Modulation          ;  Bandwidth; Tags
   460125000; LB PD U1 Dispatch South / Outside Access [D031]; Narrow FM ;  10000; LBPD
   153950000; LB FD V-1 Fire Dispatch [D132]                 ; Narrow FM ;  10000; LBFD
```

- **Frequency** in Hz.
- **Bandwidth** in Hz.
- **Tags** = single tag name matching the tag table.

### How the bundled set was built

Compiled from publicly referenced channel listings (RadioReference
databases, FCC ULS records). Conventions:

- Acronym tags per agency, each with a distinct color.
- **HAM** — area repeaters with callsign + shift + tone in the name,
  FM simplex calling channels, full 2 m simplex grid.
- **GMRS/FRS** — full 30-channel FCC plan with channel numbers.
- CTCSS/DPL tones embedded in the channel name
  (`[D031]` = DPL 031, `[PL 151.4]` = 151.4 Hz CTCSS).
- Analog FM only (P25/DMR/NXDN noted but not demodulable).
- Duplicate frequencies allowed (two uses on one Hz are visited once per
  sweep).

---

## Runtime files

| Path | Purpose |
|---|---|
| `~/.config/gqrx/bookmarks.csv` | GQRX bookmark file — scan source (read-only; falls back to `./bookmarks.csv`) |
| `./scanner.log` | Verbose activity log. `tail -f` it. |
| `./scanner_settings.json` | Persisted UI settings (tags, lockouts, disabled channels, sliders). |
| `./recordings/` | WAVs + `recordings.sqlite` + `recordings.events.jsonl`. |
| `./heatmap.sqlite` | Heatmap sessions + per-sweep power rows. |
| `./heatmap.log` / `./heatmap.events.jsonl` | Heatmap verbose log + JSONL event stream. |
| `./iq/` | Opt-in raw IQ dumps (`.cf32`). |

---

## Troubleshooting

- **`DISCONNECTED` / red dot** — GQRX remote control is not enabled or
  not listening on `127.0.0.1:7356`. Enable it under *Tools → Remote
  control*. (GQRX mode only.)

- **Never stops on signals** — squelch threshold is too tight. Run
  Auto-Noise-Floor, or lower the global squelch slider in Global mode.
  Confirm AGC is off.

- **Stops on noise constantly** — raise the auto margin, or raise the
  global squelch value.

- **No audio when parked (GQRX)** — GQRX's own squelch must also be
  open. Verify GQRX audio output and volume.

- **No audio when parked (RTL)** — check system audio output device. The
  RTL backend uses sounddevice (PortAudio) — confirm it is not muted.

- **Digital channel, no audio** — P25/DMR/NXDN cannot be demodulated by
  an analog NBFM receiver.

---

## Dependencies & licenses

The GQRX scanner core (`rf_hotscan.py`, `clock.py`, `recorder.py`'s
metadata layer) uses only the Python standard library (Python + Tkinter,
PSF license) — no third-party code is imported or redistributed. The
direct RTL-SDR backend and Heatmap rely on third-party runtime
dependencies that you install separately (not bundled).

| Dependency | Used for | License |
|---|---|---|
| Python + Tkinter | runtime + GUI | PSF (permissive) |
| numpy | FFT / array math | BSD-3-Clause |
| scipy | DSP filters, Welch PSD | BSD-3-Clause |
| matplotlib | heatmap render + PNG export | Matplotlib license (BSD-style, PSF-based) |
| sounddevice | audio playback | MIT |
| PortAudio (via sounddevice) | audio I/O | MIT-style |
| **pyrtlsdr** | RTL-SDR Python binding | **GPL-3.0** |
| **librtlsdr** / `rtl-sdr` | native USB driver | **GPL-2.0-or-later** |

**Copyleft advisory:** pyrtlsdr (GPL-3.0) and librtlsdr (GPL) are
copyleft. This repo distributes only its own source and neither bundles
nor links those libraries — they are installed separately by the user — so
the project's own LICENSE can be chosen independently. However,
distributing a combined or packaged/binary work (frozen app, wheel
vendoring these libs) would place that combined work under the GPL. Keep
the RTL backend an optional, separately-installed component.

Each dependency remains under its own license; installing them (via
`requirements-rtl.txt`) is your acceptance of those terms.

---

## License

License TBD before public release. See
[Dependencies & licenses](#dependencies--licenses) above for copyleft
implications.
