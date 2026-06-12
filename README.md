# RF HotScan

**A tag-aware bookmark scanner for [GQRX](https://www.gqrx.dk/).**

GQRX is an excellent SDR receiver, but it has no built-in *scanner* — it cannot
sweep your bookmarks and stop on whichever channel is active. RF HotScan adds
exactly that, by driving GQRX through its remote-control (rigctl-compatible) TCP
interface. It reads your existing GQRX bookmark file, sweeps the channels,
measures signal strength on each, and parks on any channel whose signal rises
above a squelch threshold — with hold-after-loss, per-tag filtering, priority
channels, lockouts, and an auto-calibrated noise-floor squelch.

It is a single-file Python app with a dark, color-coded Tkinter GUI and **no
third-party dependencies** (standard library only).

> Status: working tool, actively developed. Built and tested on macOS with
> GQRX 2.17.x and an RTL-SDR, but the remote protocol is standard so it should
> work anywhere GQRX runs.

---

## At a glance

- **Scans your GQRX bookmarks** grouped by acronym **tags** (e.g. police, fire,
  port, schools). Toggle tags on/off to show/hide and include/exclude them live.
- **Per-channel enable/disable** — untick any individual bookmark to drop it
  from the sweep; all channels are enabled by default.
- **Hold-after-loss** — parks on an active channel and resumes a configurable
  number of seconds after the signal drops.
- **Priority channels** — flag one or more channels (★) that get checked on an
  interval even while parked elsewhere, and pre-empt the held channel.
- **Lockout** — temporarily skip a chatty channel for the session.
- **Auto-Noise-Floor** — samples empty in-band frequencies, measures the noise
  floor per band, and sets the squelch relative to the live RF environment.
  Scanning pauses and a clear `CALIBRATING` indicator shows RF HotScan driving
  GQRX across the bands.
- **Two-way squelch sync** — the global squelch slider mirrors GQRX's squelch:
  change it in either app and both stay in agreement.
- **Audio-gain slider** — set GQRX's AF gain from the scanner.
- **Live dBFS meter** with the active threshold marked, color-coded state
  (`SCANNING` / `HOLDING` / `CALIBRATING` / `DISCONNECTED`), and a connection
  indicator dot (green = reachable, red = not).
- **Verbose, tailable log** at `./scanner.log` for debugging and
  for AI agents to inspect scan behavior.
- **RF activity heatmap** (second tab) — captures a time × frequency heatmap over
  a band using **direct RTL-SDR control**, stores every sweep in SQLite
  (re-renderable), flags active frequencies, and exports PNGs. Also drivable
  headless via `python -m heatmap` for scripts/agents. See
  [Heatmap](#heatmap-rf-activity-over-time).

---

## How it works

GQRX exposes a small, human-readable, line-based TCP server (default
`127.0.0.1:7356`) that mirrors the [Hamlib `rigctld`
protocol](https://hamlib.github.io/). RF HotScan opens one connection and uses a
handful of commands:

| Command | Meaning |
| --- | --- |
| `F <Hz>` / `f` | set / get frequency |
| `M <mode> <passband>` / `m` | set / get demodulator mode |
| `l STRENGTH` | read current signal level (dBFS) — the core of detection |
| `l SQL` / `L SQL <dBFS>` | get / set squelch threshold |
| `l AF` / `L AF <dB>` | get / set audio (AF) gain |
| `c` | close |

The scan loop is simply: **set frequency → wait a few ms to settle → read
`STRENGTH` → compare to a threshold → if above, hold and keep polling until the
signal has been gone for the hold time, then resume.** Detection is
**level-only**: GQRX's remote interface does not decode CTCSS/DPL sub-audible
tones, so RF HotScan cannot gate on tone. Tones from the source data are
preserved in channel names for reference only.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the internals and
[`docs/AGENTS.md`](docs/AGENTS.md) for an orientation aimed at AI coding agents.

---

## Heatmap (RF activity over time)

A second tab, **Heatmap**, is the 2026 take on the classic `rtl_power` +
`heatmap.py`: it sweeps a start→stop frequency range over a time window using
**direct RTL-SDR control** and answers *what was transmitting, where, and when*
across the band. (Implemented in `heatmap.py`, built on the direct
`rtl_backend.RtlBackend`.)

- **Direct dongle sweep** — tiles the range into ~2 MHz windows, FFTs each, and
  stitches one full per-bin power row per sweep. Lots of knobs: start/stop,
  sample rate, FFT bin width, gain/AGC, PPM, dwell, averaged blocks, crop %, hop
  overlap, duration, activity margin, colormap.
- **Every sweep persisted to SQLite** (`heatmap.sqlite`, one quantised power row
  per sweep) so a heatmap can be **re-created offline, exactly** — re-render or
  export a PNG from any past session.
- **Activity detection** — a per-bin noise floor flags bins that key up, and
  contiguous active bins are clustered into detected frequency ranges (with duty
  %), so intermittent transmissions stand out from a constant floor.
- **Live + polished views** — a fast pure-Tk waterfall during capture; a
  matplotlib re-render (colormaps, colorbar, MHz/time axes, pan/zoom, PNG export)
  for stored sessions.
- **Opt-in raw IQ dumps** (`.cf32`) per window — manual or auto-on-activity.

### Shared dongle

The RTL dongle is single-owner. The Scanner (in RTL mode) and the Heatmap cannot
hold it at once, so running a Heatmap capture **borrows the Scanner's connected
backend and pauses the Scanner's scan for the duration of the capture**, then
resumes it — tied to *running a capture*, not to switching tabs. If the Scanner
is on the GQRX backend (or no dongle is connected), the Heatmap opens its own
(quitting GQRX first). A second owner trying to open the dongle gets a clear
error rather than a corrupted tune.

### Headless / agent use

The same engine runs without the GUI, for scripts or AI agents:

```sh
# capture 88–108 MHz for 10 s; print JSON (session id, detected ranges, ...)
.venv/bin/python -m heatmap scan --start 88e6 --stop 108e6 --duration 10 \
    --device 0 --gain auto --json
.venv/bin/python -m heatmap render <session_id> --png out.png   # re-render a session
.venv/bin/python -m heatmap list                                 # sessions as JSON
.venv/bin/python -m heatmap info <session_id>                     # params + detected
```

`heatmap.run_scan(...)` is the equivalent Python API.

> **Testing only:** `--device fake` selects a built-in *synthetic* source
> (`FakeSweepSource`) that fabricates a deterministic spectrum with no dongle. It
> exists solely for the test suite (`test_heatmap.py`) and no-hardware CI/dry
> runs — it produces invented signals, not real RF, and is **not** offered in the
> GUI. All operational paths (GUI and CLI) default to the real dongle (`0`).

---

## Requirements

- **GQRX 2.15+** (developed against 2.17.7) with a working SDR device.
- **Python 3.9+** with **Tkinter**.
  - macOS (Homebrew): `brew install python-tk@3.14` (or match your Python
    version). Apple's `/usr/bin/python3` already ships Tkinter but with an older
    Tk that renders poorly on Retina displays.
  - Debian/Ubuntu: `sudo apt install python3-tk`.
- No `pip` packages required for the GQRX scanner path.
- **For the direct RTL-SDR backend and the Heatmap tab** (optional): `librtlsdr`
  (`brew install rtl-sdr`) plus `numpy`, `scipy`, `pyrtlsdr`, `sounddevice`,
  `matplotlib` in a project `.venv` — see
  [`requirements-rtl.txt`](requirements-rtl.txt) or run `./setup_rtl_env.sh`.
  Then launch with the venv interpreter (`.venv/bin/python rf_hotscan.py`).

---

## Setup

1. **Enable GQRX remote control:** in GQRX, open **Tools → Remote control**
   (and **Tools → Remote control settings** to confirm host `127.0.0.1`, port
   `7356`). The scanner connects to this.
2. **Disable AGC / use a fixed gain** in GQRX for stable dBFS readings —
   otherwise the noise floor drifts as AGC pumps and squelch thresholds become
   unreliable.
3. **Have some bookmarks.** RF HotScan reads GQRX's own bookmark file at
   `~/.config/gqrx/bookmarks.csv`. See [Bookmarks](#bookmarks) below and
   [`examples/long_beach_bookmarks.csv`](examples/long_beach_bookmarks.csv).
4. **Run it:**
   ```sh
   python3 rf_hotscan.py            # GQRX scanner path (stdlib only)
   .venv/bin/python rf_hotscan.py   # + direct RTL-SDR backend & Heatmap tab
   ```
   The window has two tabs: **Scanner** (GQRX / RTL bookmark scanner) and
   **Heatmap** (RF activity heatmap — see [above](#heatmap-rf-activity-over-time)).
   Run from the repo directory so it finds `heatmap.py` / `rtl_backend.py` /
   `clock.py`.

### First-run workflow

1. Press **📈 Auto-Noise-Floor** once (squelch mode defaults to *Auto*) so
   thresholds match current conditions.
2. Pick the tags you want with the chips / **All** / **None** buttons.
3. Press **▶ Scan**.

---

## Bookmarks

RF HotScan does not maintain its own channel database — it reads **GQRX's
bookmark file** (`~/.config/gqrx/bookmarks.csv`) directly, so anything you can
bookmark in GQRX, the scanner can sweep.

### Bundled sample: a whole-city monitoring set

The repo ships a ready-to-use example at
[`examples/long_beach_bookmarks.csv`](examples/long_beach_bookmarks.csv) — a
**city-monitoring set** for Long Beach, CA. Rather than a handful of random
frequencies, it is organized to cover *one city's* conventional analog radio
landscape across its public agencies, so you can drop it in and immediately
scan — or heatmap — what a whole city is saying on the air.

- **79 channels across 10 agency tags**, spanning VHF/UHF (≈150–935 MHz):

  | Tag | Agency / service | Tag | Agency / service |
  | --- | --- | --- | --- |
  | `LBPD` | Police | `LBFD` | Fire / lifeguard / marine |
  | `POLB` | Port of Long Beach | `USCG` | Coast Guard (harbor) |
  | `LBC` | City gov / utilities / works | `LBT` | Transit |
  | `LBUSD` | Unified School District | `CSULB` | Cal State Long Beach |
  | `LBCC` | City College | `LBMH` | Memorial paramedic base-hospital |

- **Tag-per-agency, color-coded**, so you can filter the scan (or read the
  heatmap) by service — watch just police + fire, or everything at once.
- **Tones carried in channel names** (`[D031]`, `[PL 151.4]`) as a reference,
  and **analog FM only** (digital P25/DMR/NXDN are noted but not demodulable).

**Use it:** copy it into place as GQRX's bookmark file, then launch:

```sh
cp examples/long_beach_bookmarks.csv ~/.config/gqrx/bookmarks.csv
```

(If `~/.config/gqrx/bookmarks.csv` is absent, RF HotScan also falls back to a
`bookmarks.csv` next to the app.) It is meant as a **template**: swap in your own
city's agencies/frequencies following the same tag-per-agency, tone-in-name
conventions — see [how it was built](#how-the-bundled-long-beach-bookmark-set-was-built)
below.

### The bookmark file format

GQRX's `bookmarks.csv` has two sections — a tag table and a channel table:

```
# Tag name          ;  color
LBPD                ; #1e90ff
LBFD                ; #ff0000
...

# Frequency ; Name                     ; Modulation          ;  Bandwidth; Tags
   460125000; LB PD U1 Dispatch South / Outside Access [D031]; Narrow FM ;  10000; LBPD
   153950000; LB FD V-1 Fire Dispatch [D132]                 ; Narrow FM ;  10000; LBFD
```

- **Frequency** is in Hz. **Bandwidth** is in Hz. **Tags** is a single tag name
  matching the tag table (which assigns each tag a color).
- RF HotScan parses both sections: tag colors drive the UI color-coding, and the
  channels become the scan list.

### How the bundled Long Beach bookmark set was built

The example set ([`examples/long_beach_bookmarks.csv`](examples/long_beach_bookmarks.csv),
also the working `~/.config/gqrx/bookmarks.csv`) was **compiled from publicly
referenced channel listings** for the Long Beach, CA area (the kind of
conventional-frequency, license, and CTCSS/DPL data published in public
radio-reference databases and FCC ULS records). That public information was
**transcribed directly into GQRX's bookmark CSV** — i.e. we edited the GQRX
bookmark file itself rather than introducing a separate database. The editing
followed a few deliberate conventions:

- **Acronym tags per agency**, each with a distinct color, so you can filter by
  service:

  | Tag | Agency / service |
  | --- | --- |
  | `LBC` | City of Long Beach — general gov / utilities / public works |
  | `LBPD` | Long Beach Police Department |
  | `LBFD` | Long Beach Fire Dept (incl. lifeguards / marine) |
  | `POLB` | Port of Long Beach |
  | `LBCC` | Long Beach City College |
  | `LBUSD` | Long Beach Unified School District |
  | `CSULB` | Cal State Long Beach |
  | `USCG` | Coast Guard Sector LA–Long Beach + harbor marine |
  | `LBT` | Long Beach Transit |
  | `LBMH` | Long Beach Memorial paramedic base-hospital channels |

- **CTCSS/DPL tones embedded in the channel name** (e.g. `[D031]` = DPL 031,
  `[PL 151.4]` = 151.4 Hz CTCSS). GQRX bookmarks have no tone field, and the
  remote protocol can't gate on tone, so the tone is carried in the name as a
  manual reference for setting tone squelch in GQRX if desired.
- **Analog FM channels only.** Digital channels (P25 / DMR / NXDN) are noted
  where included but GQRX cannot demodulate them to voice; pure data/telemetry
  channels were omitted.
- **Duplicate frequencies are allowed.** Some frequencies carry two distinct
  uses (different tone/agency) and appear as two bookmarks at the same Hz. RF
  HotScan shows both rows but visits the frequency once per sweep (a level-only
  scanner cannot tell them apart on the air).

You can adapt the same scheme to any locale: tag by agency, embed tones in the
name, and let RF HotScan handle the rest.

---

## Runtime files

| Path | Purpose |
| --- | --- |
| `~/.config/gqrx/bookmarks.csv` | GQRX's bookmark file — the scan source (read-only; falls back to `./bookmarks.csv` if absent). |
| `./scanner.log` | Verbose, appendable activity log next to the app. `tail -f` it. |
| `./scanner_settings.json` | Persisted UI settings (tags, lockouts, disabled channels, sliders). |
| `./recordings/` | Transmission WAVs + `recordings.sqlite` + `recordings.events.jsonl`. |
| `./heatmap.sqlite` | Heatmap sessions + per-sweep power rows (re-renderable). |
| `./heatmap.log` / `./heatmap.events.jsonl` | Heatmap verbose log + agent-readable JSONL event stream. |
| `./iq/` | Opt-in raw IQ dumps (`.cf32`) from heatmap captures. |

---

## Troubleshooting

- **`DISCONNECTED` / red dot:** GQRX remote control isn't enabled or isn't on
  `127.0.0.1:7356`. Enable it under **Tools → Remote control** and press
  **⟳ Reconnect GQRX**.
- **Never stops on signals:** squelch too low (threshold too high). Run
  **Auto-Noise-Floor**, or lower the global squelch in *Global* mode. Confirm
  AGC is off.
- **Stops on noise constantly:** raise the auto margin, or raise global squelch.
- **No audio when parked:** GQRX's own squelch must also be open. RF HotScan
  sets GQRX's squelch as it tunes, but verify the GQRX audio output / volume.
- **Digital channel, no audio:** P25/DMR/NXDN can't be demodulated by GQRX.

---

## Dependencies & licenses

The **GQRX scanner core** (`rf_hotscan.py`, `clock.py`, `recorder.py`'s metadata
layer) uses only the Python standard library (Python + Tkinter, PSF license) — no
third-party code is imported or redistributed by this repo.

The **direct RTL-SDR backend and the Heatmap** are optional and rely on
third-party runtime dependencies that *you* install (they are **not** bundled or
redistributed here). Their licenses:

| Dependency | Used for | License |
| --- | --- | --- |
| Python + Tkinter | runtime + GUI | PSF (permissive) |
| numpy | FFT / array math | BSD-3-Clause |
| scipy | DSP filters, Welch PSD | BSD-3-Clause |
| matplotlib | heatmap render + PNG export | Matplotlib license (BSD-style, PSF-based) |
| sounddevice | audio playback | MIT |
| PortAudio (via sounddevice) | audio I/O | MIT-style |
| **pyrtlsdr** | RTL-SDR Python binding | **GPL-3.0** |
| **librtlsdr** / `rtl-sdr` | native USB driver | **GPL-2.0-or-later** |

**Copyleft note (read before distributing binaries).** `pyrtlsdr` (GPL-3.0) and
the native `librtlsdr` (GPL) are copyleft. This repository distributes **only its
own source** and neither bundles nor links those libraries — they are installed
separately by the user — so the project's own `LICENSE` can be chosen
independently. However, **distributing a *combined* or packaged/binary work** that
includes the RTL stack (e.g. a frozen app, a wheel vendoring these libs) would
place that combined work under the GPL. The stdlib-only GQRX path carries no such
obligation. Keep the RTL backend an optional, separately-installed component, and
pick the project license below with this in mind.

Each dependency remains under its own license; installing them (e.g. via
[`requirements-rtl.txt`](requirements-rtl.txt)) is your acceptance of those terms.

---

## License

License TBD before public release. See **Dependencies & licenses** above for the
copyleft implications of the optional RTL stack when choosing it.
