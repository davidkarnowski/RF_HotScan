**Status:** Complete (2026-07-15) — see Execution log at the end of this document.
Remaining open item: live end-to-end healing verification with the dongle (WP3.6,
user-assisted; the pipeline is verified offline).

# RF HotScan — Phase 3: Remediation & Documentation Alignment

> **Persistence note (first execution step):** This plan must live in the repo, not
> only in Claude's plan cache. **WP0** below copies this document verbatim to
> `docs/plans/phase_3_remediation.md`. All subsequent progress updates (checkboxes,
> deviation notes) are made to the repo copy so any agent can resume from it.

## Context

A full code↔documentation alignment audit (2026-07-14/15, session on branch
`feature/phase2-stt-healing`) found that the code is healthy but the in-flight
Phase 2 (LLM transcript healing) has one functional gap and several polish issues,
and the four documentation files have fallen two phases behind the code, with a
handful of outright factual errors that actively mislead (a fabricated headless
API in AGENTS.md, a wrong TCP port in ARCHITECTURE.md, a "Whisper / Vosk"
description of an STT module that ships neither).

**Headline defect:** the channel-description (`desc`) context pipeline — the
entire point of the Phase 2 bookmark-description plumbing (bookmarks CSV →
channel dict → `on_hold` → recorder → SQLite) — dead-ends one hop before the LLM:
`TranscriptionService.enqueue` drops `desc` from the job dict and `_do` builds the
healer context without it, so every healing prompt runs with name+tag only and
`_build_prompt`'s "Additional Context" branch is dead code.

**Decisions already made by the user (do not re-litigate):**
1. Fix **all** observed code issues, not just critical ones.
2. Junk/"no-speech" transcripts **should** become eligible for dual-STT healing
   when agentic fallback is enabled (restores the original Phase 2 plan intent).
3. The CID-keying invariant is resolved **docs-side**: lockout/priority/last-active
   stay frequency-keyed in code (correct for a level-only scanner); the docs are
   rewritten to describe reality.
4. Work is committed in **staged logical commits** on the current branch
   `feature/phase2-stt-healing` (note: `healer.py` is currently *untracked* and
   must be `git add`ed in the first commit).

## Conventions that bind all changes

- Time: persist UTC epoch via `clock.now_unix()`; never `time.time()` for stored
  timestamps (AGENTS.md time-base rule).
- Threading: GUI ↔ engine only via `set_cfg` / `request` / queues; `cfg` reads
  from other threads must go through `Scanner`'s lock (via `Scanner.get_cfg`).
- Only the RTL backend produces audio/recordings; GQRX path stays stdlib-only.
- `healer.py` must remain stdlib-only (urllib), like the GQRX core.
- Code style: match surrounding comment density and idiom; no "what changed"
  comments.

---

## WP0 — Persist this plan into the repo

1. Copy this document to `docs/plans/phase_3_remediation.md` (create as-is, then
   maintain checkboxes there as work proceeds).
2. Add a status header line: `**Status:** In progress (started 2026-07-15)`.

---

## WP1 — Code fixes

### WP1.1 `stt.py` — complete the `desc` pipeline (the headline fix)

*Why:* healer prompts never receive the channel description that Phase 2 built
end-to-end plumbing for.

1. In `TranscriptionService.enqueue` (~line 463), add to the normalized job dict:
   `"desc": rec.get("desc", "")`.
2. In `TranscriptionService._do` (~line 553), build the healer context as
   `context = {"name": job.get("name"), "tag": job.get("tag"), "desc": job.get("desc", "")}`.

### WP1.2 `recorder.py` — include `desc` in the `on_start` payload

*Why:* `_on_recording_started` in rf_hotscan.py already does `m.get("desc", "")`
but the recorder's onset payload never includes it, so it is always empty.

- In `WavRecorder._open_wav` (~line 287), add `"desc": self._meta.get("desc", "")`
  to the `on_start` dict (alongside name/tag/freq_hz/unix_start/iso_start).

### WP1.3 `rf_hotscan.py` — carry `desc` through the re-transcribe path

*Why:* `_retranscribe_key` (~line 2119) rebuilds a job dict from `_txn_items`,
which never stores `desc`, so re-transcribed recordings would heal without context
even after WP1.1.

1. In `_txn_apply` (~line 2726), store `desc` on the item for both `start` and
   `stop` events (same pattern as `name`/`tag`: set on start, backfill on stop).
2. In `_retranscribe_key`'s `svc.enqueue({...})` call, add
   `"desc": it.get("desc", "")`.

### WP1.4 `stt.py` — make junk transcripts eligible for healing (user decision #2)

*Why:* `is_junk(text)` currently returns from `_do` before the healing block, so
garbage transcriptions — the strongest case for a second STT opinion — never get
healed. The Phase 2 plan specified `is_junk(text) or len < 3` as the fallback
trigger.

Restructure `_do` after the raw transcript is produced (~lines 533-573):

1. Compute `text` as today.
2. Extract the healing block into a helper on the service, e.g.
   `_heal(job, text, audio) -> (healed_text_or_None, eng_label_or_None)`, which:
   - resolves the healer via `make_healer` (unchanged);
   - triggers the dual-STT fallback when
     `self.cfg_get("agentic_fallback") and (is_junk(text) or len(text.split()) < 3)`
     (see WP1.7 for `cfg_get`);
   - uses the **cached** fallback provider (WP1.5);
   - returns the healed text (or `None` if unchanged/failed).
3. New control flow:
   - If healing is enabled: call `_heal` **before** the junk short-circuit.
   - If `text` was junk but the healed result is non-junk: write the raw
     transcript row as today (raw text may be empty-string for junk — keep
     writing `transcript=""` in that case so DB semantics don't change), write
     the healed columns, `_emit` the healed text, log one line, return.
   - If `text` was junk and healing produced nothing usable: fall through to the
     existing no-speech path unchanged (`transcript=""`, `_emit(..., "no_speech")`).
   - Non-junk text: same as current behavior (raw write → heal → healed write →
     emit), just routed through the helper.
4. Healed-column write uses `healed_at=clock.now_unix()` (WP1.6).

### WP1.5 `stt.py` — cache the fallback STT provider across jobs

*Why:* `_do` currently calls `make_provider(fb_eng, fb_mod)` per job; for Voxtral
this loads a 3B model from disk on every short recording.

- Add to `TranscriptionService.__init__`: `self._fb_prov = None`,
  `self._fb_key = None`.
- In the healing helper: if `(fb_eng, fb_mod) != self._fb_key`, build the
  provider, call `ensure_ready()` + `warm_up()` once, store it; on failure store
  `(key, None)` so a broken engine isn't retried every job. Reuse thereafter.
- Guard as today: skip if `fb_prov.name == self.provider.name`.

### WP1.6 `stt.py` — clock convention

- Replace `healed_at=time.time()` with `healed_at=clock.now_unix()` (~line 572).

### WP1.7 `stt.py` + `rf_hotscan.py` — cfg access through the scanner lock

*Why:* the GUI currently passes the raw live `self.scanner.cfg` dict into
`TranscriptionService`, which reads it lock-free on the worker thread — the first
violation of AGENTS.md invariant #6.

- Change `TranscriptionService.__init__(provider, db, log=None, cfg=None)` to
  accept `cfg_get=None` instead: a callable `cfg_get(key) -> value`. Store as
  `self.cfg_get = cfg_get or (lambda _k: None)`.
- Replace all `self.cfg.get("X", default)` reads in `_do`/helper with
  `self.cfg_get("X")` (all healing keys exist in `Scanner.cfg` defaults, so no
  fallback defaults are needed; treat `None`/falsy as off).
- In `rf_hotscan.py` `_apply_stt` (~line 1996), pass
  `cfg_get=self.scanner.get_cfg` instead of `cfg=self.scanner.cfg`.
  (`Scanner.get_cfg` at rf_hotscan.py:459 already takes the lock — reuse it.)

### WP1.8 `stt.py` — healer logging cleanup

*Why:* the current code logs the full multi-line prompt and response through
`self.log(...)`, spamming the GUI event pane, and calls the private
`hp._build_prompt(...)` externally just to log it (duplicate prompt build).

- Delete the `prompt_text = hp._build_prompt(...)` line and both
  `--- HEALER PROMPT/RETURN ---` log calls.
- After a successful heal, log exactly one line:
  `self.log(f"HEAL {job.get('name', '')}: {healed[:60]}")`.
- If the healer reported a failure (see WP1.9), log one line:
  `self.log(f"Healer error ({hp.name}): {hp.last_error}")`.

### WP1.9 `healer.py` — honest availability, surfaced errors, key-gated options

*Why:* `OllamaHealerProvider.available()` returns `True` unconditionally;
`heal()` swallows all exceptions silently (auth failures look like "healing did
nothing"); `engine_options()` lists OpenAI models even without an API key
(unlike `stt.engine_options()`, which gates on credentials); `gpt-3.5-turbo` is a
dated offering.

1. `HealerProvider`: add `self.last_error = None` (set in `__init__` of both
   subclasses or as a class attribute pattern matching `SttProvider`).
2. `OllamaHealerProvider.available()`: GET `http://localhost:11434/api/tags` with
   `timeout=1`; return `True` on success, `False` on any exception (replacing the
   hardcoded `True`).
3. Both `heal()` implementations: on exception, set
   `self.last_error = str(e)` before returning `text` unchanged; clear it to
   `None` at the start of each call.
4. `engine_options()`: emit the OpenAI entries only when
   `os.environ.get("OPENAI_API_KEY")` is set (the module already runs
   `stt._load_dotenv()` at import, so `.env` keys are visible). Drop the
   `gpt-3.5-turbo` entry; keep `gpt-4o-mini` and `gpt-4o`.

### WP1.10 `rf_hotscan.py` — retire the stale GQRX-first branding

*Why:* the module docstring still opens with "a tag-aware bookmark scanner GUI
for GQRX" with a `/opt/homebrew/bin/python3` run line, and the window title says
"RF HotScan — GQRX Bookmark Scanner". The app is RTL-first with GQRX as legacy
fallback (per README/STATE).

1. Rewrite the module docstring header (lines 1-23): describe the app as a direct
   RTL-SDR bookmark scanner with recording/STT/healing/heatmap and a legacy GQRX
   remote fallback; run line `.venv/bin/python rf_hotscan.py`; keep the
   feature-bullet style and the `tail -f ./scanner.log` tip.
2. `_build_style` (~line 1260): `self.root.title("RF HotScan — SDR Bookmark Scanner")`.

---

## WP2 — Documentation alignment

General rule for this WP: docs describe the **post-WP1 code**. Where a doc uses
`L###` line anchors, replace them with class/function names (e.g. "`GqrxClient`
(rf_hotscan.py)") — line anchors are the main rot vector found in the audit.

### WP2.1 `docs/AGENTS.md`

1. **Fix the fabricated headless smoke-test** (the most harmful error — the
   documented `from rf_hotscan import ScanEngine, ScanConfig` API does not
   exist). Replace with a *verified-runnable* snippet using the real API, e.g.:
   ```python
   from rf_hotscan import Scanner, GqrxClient, load_bookmarks, cluster_bands, BOOKMARKS
   tags, chans = load_bookmarks(BOOKMARKS)
   for i, c in enumerate(chans): c["cid"] = i
   sc = Scanner(GqrxClient(), tags, chans, cluster_bands(chans))
   # ... observe scanner.log; sc.alive = False to stop ...
   ```
   The implementer MUST actually run the final snippet before committing it
   (WP3.3).
2. Rewrite invariant #2 honestly: *disabled state* is CID-keyed and persisted by
   `freq:name` signature; *lockout, priority, and last-active are
   frequency-keyed by design* (a level-only scanner cannot distinguish two
   bookmarks sharing a frequency).
3. Update invariant #6 with the sanctioned pattern: worker threads read cfg via
   `Scanner.get_cfg` (as `TranscriptionService.cfg_get` now does).
4. Module table: refresh line counts (`rf_hotscan.py` ~3,0xx, `stt.py` ~6xx,
   `rtl_backend.py` 737, `recorder.py` 312 — recount at commit time), add
   `healer.py` row ("LLM transcript healing providers; stdlib urllib; Ollama +
   OpenAI").
5. STT section: add a short "Transcript healing" subsection — `HealerProvider`
   interface, `make_healer`, `engine_options()` (Ollama models discovered live;
   OpenAI gated on `OPENAI_API_KEY`), the healing flow in
   `TranscriptionService._do` incl. junk-eligible dual-STT fallback, healed
   columns in `recordings.sqlite`.
6. Mention the Recordings tab in the intro line ("three tabs: Scanner /
   Recordings / Heatmap").

### WP2.2 `docs/ARCHITECTURE.md`

1. **Port fix:** GqrxClient speaks rigctl on **7356** — fix §2 text *and* the
   block diagram ("TCP/4532" → "TCP/7356").
2. File table: fix `stt.py` role ("STT providers: Parakeet-MLX, Whisper-MLX,
   Voxtral, OpenAI + TranscriptionService with LLM healing" — remove "Vosk");
   add `healer.py`; refresh line counts.
3. §3 config table: `mute_squelch` default `True`; `stt_engine` default `"auto"`
   with real provider names; add the six healing keys (`enable_healing`,
   `healer_engine`, `healer_model`, `agentic_fallback`, `fallback_stt_engine`,
   `fallback_stt_model`); correct `lockout` → "frequencies to skip" and
   `priority_freqs` → "frequencies to check more often".
4. §3 Auto-Noise-Floor: "cap at ~15 samples" → 20 (code: `max_samples = 20`).
5. Invariants: same CID rewrite as WP2.1.2.
6. Block diagram + §4: add the Recordings tab (`RecordingsView`, own WAL
   connection to `recordings.sqlite`, manual ↻ refresh) between Scanner and
   Heatmap.
7. §5 Heatmap corrections from the audit: note the effective stitched window is
   ~1.44 MHz at defaults (2.4 MS/s × (1 − 2×0.20 crop)), not a literal 2 MHz;
   note `heatmap_settings.json` persisted GUI settings; note that the non-borrow
   `RtlSweepSource.open` path will quit a running GQRX to free the dongle; note
   power-frame stamping lives in the DB row (`t_unix`, `t_dur_ms`) while
   `emit_event` stamps events with `t`/`iso`.
8. Strip/replace all stale `L###–###` anchors per the general rule.

### WP2.3 `docs/STATE.md` — full refresh (this doc decays by design)

Rewrite as of the commit date:
1. §1: mention three tabs and the healing subsystem.
2. §2 Git status: branch `feature/phase2-stt-healing`, current HEAD after WP4
   commits, recent-commits table refreshed.
3. §4 module map: refreshed counts + `healer.py` row.
4. §7 STT: add healing wiring (cfg keys, UI section "HEALING (LLM)", healed
   columns, junk-eligible fallback).
5. §8 tags: fix the count contradiction — the **live**
   `~/.config/gqrx/bookmarks.csv` has **15 tags** (incl. USFS); the bundled
   `examples/long_beach_bookmarks.csv` has **14** (no USFS). Say both explicitly.
6. New §: Recordings tab (columns, playback transport, double-click-to-edit
   healed cell, own DB connection, manual refresh).
7. §11 known issues: remove items fixed by WP1; add remaining known limitations
   (e.g. Recordings tab needs manual refresh; healing requires Ollama running or
   an OpenAI key; GQRX rewriting `bookmarks.csv` from its own UI would strip the
   private 6th-column/3rd-column `desc` extensions — document this data-loss risk
   prominently).

### WP2.4 `README.md`

1. "At a glance": add a **Recordings tab** bullet (browse/play/edit past
   transmissions) and a **Transcript healing** bullet (optional LLM cleanup of
   STT output via local Ollama or OpenAI; agentic dual-STT fallback for
   poor/junk transcripts).
2. Bookmarks section: document the optional `desc` extension (3rd field on tag
   lines, 6th on channel lines) feeding healer context, **with the warning** that
   editing bookmarks inside GQRX rewrites the CSV and will strip these fields.
3. Runtime-files table: add `./heatmap_settings.json`.
4. Requirements: one line noting healing needs either a running Ollama
   (`localhost:11434`) or `OPENAI_API_KEY`; `healer.py` itself is stdlib-only.

### WP2.5 Plan documents

1. `docs/plans/phase_1_recordings_viewer.md`: `Status: Complete` (commit
   `3e1ba2a`); add a short "Deviations" note (own DB connection; extra UI:
   pause/resume, progress bar, engine columns, inline healed-cell editing).
2. `docs/plans/phase_2_stt_healing.md`: `Status: Implemented — remediated &
   verified in Phase 3`; add "Deviations" note (configurable healer model +
   fallback engine; OpenAI healer via urllib not the SDK; desc context pipeline
   added; junk-eligible fallback restored in Phase 3).

---

## WP3 — Verification (before each commit; full pass before the docs commit)

1. **Syntax/import gate:**
   `for f in rf_hotscan.py rtl_backend.py stt.py recorder.py healer.py; do python3 -c "import ast; ast.parse(open('$f').read())"; done`
   and `.venv/bin/python -c "import stt, healer, recorder, player, clock"`.
2. **Heatmap regression:** `.venv/bin/python test_heatmap.py` (uses
   `FakeSweepSource`; no dongle needed).
3. **AGENTS.md smoke-test truth:** run the exact snippet written in WP2.1.1; it
   must execute without ImportError (GQRX connection may fail — that's fine, the
   engine thread must start and log).
4. **Healing pipeline offline test** (script in the session scratchpad, not the
   repo): construct a `TranscriptionService` with a stub provider (returns a
   fixed junk string, then a fixed short string) and a stub healer module or a
   monkeypatched `make_healer`; assert
   (a) `desc` survives `enqueue` → `_do` → healer context,
   (b) junk + agentic fallback reaches the healer,
   (c) fallback provider is constructed once across two jobs,
   (d) DB row receives `healed_transcript` / `healed_by_engine` / `healed_at`
   (epoch float from `clock.now_unix()`).
5. **desc source check:** `.venv/bin/python -c` one-liner calling
   `load_bookmarks` on `~/.config/gqrx/bookmarks.csv` and printing a channel with
   a non-empty `desc` (live file already contains them).
6. **Live end-to-end (user-assisted, optional):** run the app with the dongle,
   enable Record + Transcribe + Healing (Ollama running), hold a transmission,
   confirm: one-line `HEAL …` log entry, healed text in the transcript pane, and
   the Recordings tab showing raw + healed columns after ↻ Refresh. Requires the
   RTL dongle; skip if hardware is absent and note it in the plan doc.

---

## WP4 — Staged commits (branch `feature/phase2-stt-healing`)

Run WP3 gates 1-2 before every commit; gates 3-5 before commit 3.

1. **Commit 1 — Phase 2 core fixes:** `stt.py`, `healer.py` (**`git add` — it is
   untracked**), `recorder.py`, `rf_hotscan.py` (WP1.1-WP1.9 + the existing
   uncommitted Phase 2 work these files already carry).
   Message: `fix(stt): healing gets desc context, junk-eligible dual-STT fallback, cached fallback provider, locked cfg access`
2. **Commit 2 — branding:** rf_hotscan.py docstring + window title (WP1.10).
   Message: `chore: retire GQRX-first branding (docstring, window title)`
3. **Commit 3 — docs:** all WP2 files + `docs/plans/phase_3_remediation.md`
   (WP0, updated with final status).
   Message: `docs: sync all docs with post-remediation code (port 7356, real headless API, CID/freq keying truth, Recordings tab, healing)`

Each commit message ends with the standard `Co-Authored-By: Claude Fable 5
<noreply@anthropic.com>` trailer. No pushes, no PR — branch stays local unless
the user asks.

## Explicitly out of scope (recorded so future agents don't "helpfully" do them)

- Changing lockout/priority to CID keying (user decision #3: docs-side fix).
- Vosk / Parakeet-v3 providers, CTCSS tone squelch, digital demod (long-standing
  "intentionally not done" items).
- Repo hygiene for heavy runtime artifacts (119 MB `heatmap.sqlite`, 18 MB
  `scanner.log`, `recordings/`) — gitignored already; cleanup is a separate task.
- Moving `desc` out of the GQRX CSV into an app-owned file (real design question,
  deferred; the data-loss risk is documented instead, WP2.3.7/WP2.4.2).

---

## Execution log (2026-07-15)

- [x] **WP0** — plan persisted to this file.
- [x] **WP1.1–1.9** — all code fixes applied (`stt.py`, `healer.py`,
      `recorder.py`, `rf_hotscan.py`). Commit `2364cac`.
- [x] **WP1.10** — branding cleanup (docstring + window title). Commit `70142c3`.
- [x] **WP2.1–2.5** — AGENTS.md, ARCHITECTURE.md, STATE.md (full rewrite),
      README.md, both phase-plan statuses + deviation notes. Committed with this
      file (docs commit).
- [x] **WP3.1** — syntax + import gates: all 5 modules parse; `.venv` imports OK.
- [x] **WP3.2** — `test_heatmap.py`: 8/8 passed.
- [x] **WP3.3** — new AGENTS.md headless snippet executed for real: engine
      thread starts, state `DISCONNECTED` without GQRX, clean exit.
- [x] **WP3.4** — offline healing-pipeline test (stubs, scratchpad): 13/13
      assertions passed — desc survives enqueue→_do→healer context; junk +
      agentic fallback reaches the healer with a second_text; fallback provider
      built/warmed exactly once across jobs; healed columns written with epoch
      `healed_at`; unrescued junk falls through to `no_speech` with no healed
      columns.
- [x] **WP3.5** — desc source check on the live bookmarks file: 206/206
      channels carry a non-empty `desc` (tag-level descriptions cascade).
- [ ] **WP3.6** — live end-to-end with the dongle (Record + Transcribe +
      Healing on a real transmission): **pending, user-assisted** — needs the
      RTL dongle attached and Ollama running.
- [x] **WP4** — commits 1 & 2 as planned; commit 3 (docs) includes this log.

### Notes / deviations from the plan

- Commit 1 also carries the pre-existing uncommitted Phase 2 diff in
  `rtl_backend.py` (async-stream guards), per WP4.1's "existing uncommitted
  Phase 2 work these files already carry".
- `rf_hotscan.py` contained both core fixes and branding hunks; the branding
  hunks were temporarily reverted for commit 1 and re-applied for commit 2 to
  keep the history split clean.
- STATE.md §12 records a small newly-noticed limitation (fallback STT guard
  compares provider names only), left as-is — out of scope.
