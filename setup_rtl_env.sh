#!/usr/bin/env bash
# Set up the optional direct RTL-SDR backend environment for RF HotScan.
# Safe to re-run. The GQRX-remote path does NOT need any of this.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Native librtlsdr + CLI tools (Homebrew)"
brew list rtl-sdr >/dev/null 2>&1 || brew install rtl-sdr

echo "==> Python venv (.venv) with project deps"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements-rtl.txt

echo "==> pyrtlsdr compatibility patch for Osmocom librtlsdr 2.0.x"
# pyrtlsdr targets the rtl-sdr-blog fork, which exports extra symbols
# (set_dithering, some GPIO calls) that Osmocom librtlsdr 2.0.x lacks. Guard the
# eager bindings and the open()-time set_dithering call so import/open succeed.
.venv/bin/python - <<'PY'
import re, ctypes, glob, os, sys
site = glob.glob(".venv/lib/python*/site-packages/rtlsdr")[0]
# locate the dylib the same way pyrtlsdr will
dylib = None
for p in ("/opt/homebrew/lib/librtlsdr.dylib", "/usr/local/lib/librtlsdr.dylib"):
    if os.path.exists(p):
        dylib = p; break
lib = ctypes.CDLL(dylib) if dylib else None

# 1) guard missing single-line symbol bindings in librtlsdr.py
F = os.path.join(site, "librtlsdr.py")
src = open(F).read().splitlines(keepends=True)
if "Osmocom-compat guard" not in "".join(src):
    out, i = ["# Osmocom-compat guard applied by setup_rtl_env.sh\n"], 0
    pat = re.compile(r'^f = librtlsdr\.(rtlsdr_\w+)\s*$')
    while i < len(src):
        m = pat.match(src[i]); missing = False
        if m and lib is not None:
            try: getattr(lib, m.group(1))
            except AttributeError: missing = True
        if (missing and i+1 < len(src) and src[i+1].startswith("f.")
                and (i+2 >= len(src) or not src[i+2].lstrip().startswith(("POINTER","c_")))):
            out += ["try:\n", "    "+src[i], "    "+src[i+1], "except AttributeError:\n    pass\n"]
            i += 2; continue
        out.append(src[i]); i += 1
    open(F, "w").write("".join(out))
    print("   patched", F)

# 2) guard the open()-time set_dithering call in rtlsdr.py
G = os.path.join(site, "rtlsdr.py")
txt = open(G).read()
needle = "result = librtlsdr.rtlsdr_set_dithering(self.dev_p, int(dithering_enabled))"
if needle in txt and "Osmocom-compat" not in txt.split(needle)[0][-200:]:
    txt = txt.replace(
        "        " + needle + "\n        if result < 0:\n"
        "            raise IOError('Error code %d when setting PLL dithering mode'\\\n"
        "                           % (result))",
        "        try:  # Osmocom-compat\n"
        "            " + needle + "\n            if result < 0:\n"
        "                raise IOError('Error code %d when setting PLL dithering mode' % (result))\n"
        "        except AttributeError:\n            pass")
    open(G, "w").write(txt); print("   patched", G)
print("   pyrtlsdr patch OK")
PY

echo "==> Verify"
.venv/bin/python -c "from rtlsdr import RtlSdr; import numpy, scipy, sounddevice; print('RTL backend env ready')"
echo "Done. Close GQRX (single-owner dongle), then:  .venv/bin/python rtl_backend.py"
