#!/bin/bash
# Reproduction script for CVE-2022-0240 in mruby
# NULL pointer dereference in prepare_singleton_class (src/class.c:360)
# Commit: 171d32c0071d776207174a40a8fa26def3dbb931

set -e

MRUBY_BIN="/src/mruby/build/host/bin/mruby"
POC="/testcase/poc"

echo "[*] Running PoC against mruby (ASan build) to reproduce CVE-2022-0240..."
echo "[*] Binary: $MRUBY_BIN"
echo "[*] PoC: $POC"
echo ""

ASAN_OPTIONS=halt_on_error=1:detect_leaks=0 "$MRUBY_BIN" "$POC" 2>&1
EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -ne 0 ]; then
    echo "[+] CRASH CONFIRMED (exit code: $EXIT_CODE) - CVE-2022-0240 reproduced"
else
    echo "[-] No crash (exit code: $EXIT_CODE) - binary may be patched"
fi
