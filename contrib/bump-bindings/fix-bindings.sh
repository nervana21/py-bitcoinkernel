#!/usr/bin/env bash
#
# Post-processes clang2py output for use as src/pbk/capi/bindings.py.
#
# Replaces the clang2py stub library loader with an import of
# BITCOINKERNEL_LIB from pbk.capi.library, which handles finding
# the bundled shared library at runtime.
#
# Usage: contrib/bump-bindings/fix-bindings.sh src/pbk/capi/bindings.py

set -euo pipefail

if [ -z "${1:-}" ]; then
    echo "Usage: $0 <bindings.py>"
    exit 1
fi

FILE="$1"

python3 - "$FILE" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()

text = re.sub(r"^# TARGET arch is: \[.*\]$", "# TARGET arch is: []", text, flags=re.M)

if "from pbk.capi.library import BITCOINKERNEL_LIB" not in text:
    text = text.replace(
        "import ctypes\n",
        "import ctypes\n\nfrom pbk.capi.library import BITCOINKERNEL_LIB\n",
        1,
    )

text = text.replace("_libraries['FIXME_STUB']", "BITCOINKERNEL_LIB")

pattern = re.compile(
    r"\n*class FunctionFactoryStub:.*?\nBITCOINKERNEL_LIB = FunctionFactoryStub\(\).*?\n",
    re.S,
)
text, n = pattern.subn("\n\n", text, count=1)
if n != 1:
    raise SystemExit(f"expected to remove FunctionFactoryStub once, removed {n}")

path.write_text(text)
print(f"fixed {path}")
PY
