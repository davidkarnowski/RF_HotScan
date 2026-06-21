#!/usr/bin/env python3
"""Headless tests for heatmap.py — no dongle required (FakeSweepSource).

Run:  .venv/bin/python test_heatmap.py
"""
import os
import time
import numpy as np

import heatmap


def test_planning():
    cfg = heatmap.SweepConfig(460e6, 466e6, bin_hz=3000, crop=0.20, device="fake")
    assert cfg.n_bins > 0
    assert np.all(np.diff(cfg.freq_grid) > 0)            # monotonic axis
    half = cfg.usable / 2.0
    # first window covers the low edge, last covers the high edge
    assert cfg.hops[0] - half <= cfg.f_start + 1
    assert cfg.hops[-1] + half >= cfg.f_stop - 1
    # fft geometry sane
    assert cfg.fft_size & (cfg.fft_size - 1) == 0        # power of two
    print("  planning: %d bins, %d hops, fft=%d, bin=%.1f Hz OK"
          % (cfg.n_bins, len(cfg.hops), cfg.fft_size, cfg.bin_hz))


def test_quantize_roundtrip():
    rng = np.random.default_rng(0)
    row = (rng.random(2000) * 60 - 110).astype(np.float32)
    codes, ref, scale = heatmap.quantize_row(row)
    back = heatmap.dequantize(codes, ref, scale)
    err = float(np.max(np.abs(back - row)))
    assert err <= scale / 2 + 1e-3, err
    assert codes.dtype == np.uint8
    print("  quantize: max err %.4f dB (scale %.4f) OK" % (err, scale))


def test_db_roundtrip_and_activity():
    cfg = heatmap.SweepConfig(460e6, 466e6, device="fake", duration_s=0,
                              max_sweeps=80, margin_db=8)
    db = heatmap.HeatmapDB(":memory:")
    src = heatmap.make_source(cfg)
    live = []
    sid, ranges, n = heatmap.run_capture(
        cfg, src, db, on_row=lambda s, r, m: live.append(r.copy()))
    assert n == 80, n
    matrix, meta = db.load_matrix(sid)
    assert matrix.shape == (80, cfg.n_bins), matrix.shape
    # DB reproduces the live matrix within quantisation error
    diff = float(np.max(np.abs(matrix - np.vstack(live))))
    assert diff < 1.0, diff
    # detected the bursty + intermittent transmitters (~463.3 and ~464.8 MHz)
    cf = sorted((r["f_lo"] + r["f_hi"]) / 2e6 for r in ranges)
    assert len(ranges) >= 2, ranges
    assert any(abs(c - 463.3) < 0.05 for c in cf), cf
    assert any(abs(c - 464.8) < 0.05 for c in cf), cf
    print("  db+activity: %d sweeps, DB-vs-live max %.3f dB, detected %s MHz OK"
          % (n, diff, ["%.3f" % c for c in cf]))


def test_render_png():
    cfg = heatmap.SweepConfig(460e6, 463e6, device="fake", duration_s=0,
                              max_sweeps=40)
    db = heatmap.HeatmapDB(":memory:")
    src = heatmap.make_source(cfg)
    sid, ranges, n = heatmap.run_capture(cfg, src, db)
    out = heatmap.render_session_png(db, sid, "/tmp/hm_unit.png")
    assert out and os.path.exists(out) and os.path.getsize(out) > 1000
    print("  render: %s (%d bytes) OK" % (out, os.path.getsize(out)))


def test_dongle_coordination():
    # Single-owner enforcement (no real device needed — exercise the guard).
    import rtl_backend as rb
    a, b = object(), object()
    rb._acquire_dongle(a, "scanner-rtl")
    assert rb.dongle_owner() == "scanner-rtl"
    try:
        rb._acquire_dongle(b, "heatmap")
        assert False, "second owner must be refused"
    except rb.DongleBusy:
        pass
    rb._acquire_dongle(a, "scanner-rtl")          # same instance re-acquire OK
    rb._release_dongle(a)
    assert rb.dongle_owner() == ""
    rb._acquire_dongle(b, "heatmap")              # free now -> handoff
    assert rb.dongle_owner() == "heatmap"
    rb._release_dongle(b)
    print("  dongle coordination: refuse 2nd owner, idempotent reconnect, "
          "clean handoff OK")


def test_borrow_capture():
    # Borrow path: run a full capture through a BORROWED backend (mock RtlBackend
    # producing synthetic IQ) — no real device, no second open, not closed by us.
    import rtl_backend as rb
    cfg = heatmap.SweepConfig(460e6, 463e6, device="0", duration_s=0,
                              max_sweeps=40, margin_db=8)

    class MockRtl(rb.RtlBackend):
        def __init__(self, cfg):
            super().__init__(sample_rate=cfg.samp_rate, sweep_nsamp=cfg.win_nsamp)
            self._fake = heatmap.FakeSweepSource(cfg)
            self.closed = False

        @property
        def connected(self):
            return not self.closed

        def capture_iq(self, center_hz, nsamp=None):
            return self._fake._capture_window(center_hz)

        def close(self):
            self.closed = True

    mock = MockRtl(cfg)
    src = heatmap.RtlSweepSource(cfg, backend=mock)
    db = heatmap.HeatmapDB(":memory:")
    sid, ranges, n = heatmap.run_capture(cfg, src, db)
    assert n == 40, n
    assert mock.connected and not mock.closed, "borrowed backend must NOT be closed"
    assert src.backend is None, "source should release its borrowed reference"
    matrix, meta = db.load_matrix(sid)
    assert matrix.shape == (40, cfg.n_bins), matrix.shape   # real data captured
    assert float(matrix.std()) > 0, "captured spectrum should not be flat"
    print("  borrow capture: %d sweeps via borrowed backend, left open, "
          "DB %dx%d OK" % (n, matrix.shape[0], matrix.shape[1]))


def test_coordinator():
    import threading
    import rf_hotscan as g
    import rtl_backend as rb

    class MockRtl(rb.RtlBackend):
        def __init__(self):
            super().__init__()

        @property
        def connected(self):
            return True

        def on_resume(self):
            pass

        def stop_audio(self):
            pass

    client = MockRtl()

    class _Scanner:
        def __init__(self):
            self.client = client
            self.run = threading.Event()
            self.logs = []

        def log(self, m):
            self.logs.append(m)

    class _App:
        def __init__(self):
            self.scanner = _Scanner()

    app = _App()
    app.scanner.run.set()                       # Scanner is actively scanning
    coord = g.SdrShareCoordinator(app)
    borrowed = coord.begin_external_use("heatmap")
    assert borrowed is client, "should lend the connected RTL backend"
    assert not app.scanner.run.is_set(), "scan must be paused during capture"
    coord.end_external_use()
    assert app.scanner.run.is_set(), "scan must resume after capture"
    # If the Scanner was NOT running, it should stay paused after.
    app.scanner.run.clear()
    coord.begin_external_use("heatmap")
    coord.end_external_use()
    assert not app.scanner.run.is_set(), "idle scanner stays idle"
    print("  coordinator: pause on capture, resume after (only if was running) OK")


def test_gui_smoke():
    try:
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
    except Exception as e:
        print("  gui smoke: SKIPPED (no display: %s)" % e)
        return
    root.withdraw()
    nb = ttk.Notebook(root)
    nb.pack()
    tab = heatmap.HeatmapTab(nb)
    nb.add(tab, text="HM")
    tab.vars["device"].set("fake")
    tab.vars["start_mhz"].set("460")
    tab.vars["stop_mhz"].set("463")
    tab.vars["duration"].set("1.5")
    tab._start()
    t0 = time.time()
    while time.time() - t0 < 3.5:
        root.update()
        time.sleep(0.03)
    assert tab.view.have > 0, "no rows reached the live view"
    sid = tab.recorder.last_session
    assert sid is not None, "session did not finish"
    rows = tab.tree.get_children()
    out = heatmap.render_session_png(tab.recorder.db, sid, "/tmp/hm_gui.png")
    assert out and os.path.exists(out)
    print("  gui smoke: session %s, view rows=%d, tree=%d, png OK"
          % (sid, tab.view.have, len(rows)))
    root.destroy()


if __name__ == "__main__":
    tests = [test_planning, test_quantize_roundtrip,
             test_db_roundtrip_and_activity, test_render_png,
             test_dongle_coordination, test_borrow_capture, test_coordinator,
             test_gui_smoke]
    fails = 0
    for t in tests:
        print(t.__name__)
        try:
            t()
        except Exception:
            import traceback
            traceback.print_exc()
            fails += 1
    print("\n%s — %d/%d passed" %
          ("ALL OK" if not fails else "FAILURES", len(tests) - fails, len(tests)))
    raise SystemExit(1 if fails else 0)
