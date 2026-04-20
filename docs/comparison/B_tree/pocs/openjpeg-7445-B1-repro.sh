#!/bin/bash
# Reproducer for CVE-2016-7445 - NULL pointer dereference in openjpeg
# opj_compress: pnmtoimage -> read_pnm_header -> skip_int(NULL) -> skip_white(NULL)
# Vulnerable commit: 53f25200ed696cf5dc71d5fe12faad2570861b20

set -e

OPENJPEG_SRC="${1:-/src/openjpeg}"
POC="${2:-/testcase/openjpeg-nullptr-github-issue-842.ppm}"

cd "$OPENJPEG_SRC"

# Build with ASAN if needed
if [ ! -f "build_asan/bin/opj_compress" ]; then
    echo "[*] Building openjpeg with ASAN..."
    rm -rf build_asan
    mkdir build_asan
    cd build_asan
    CC=clang CXX=clang++ \
        cmake -DCMAKE_BUILD_TYPE=Debug \
              -DBUILD_SHARED_LIBS=OFF \
              -DCMAKE_C_FLAGS='-O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer' \
              -DCMAKE_EXE_LINKER_FLAGS='-fsanitize=address,undefined' \
              .. > /dev/null 2>&1
    make -j4 > /dev/null 2>&1
    cd ..
    echo "[+] Build complete"
fi

echo "[*] Running opj_compress against PoC: $POC"
ASAN_OPTIONS='abort_on_error=0:symbolize=1' \
LSAN_OPTIONS='detect_leaks=0' \
"$OPENJPEG_SRC/build_asan/bin/opj_compress" -i "$POC" -o /tmp/cve_2016_7445_out.j2k 2>&1 | tee /tmp/cve_2016_7445_asan.log

echo "[*] Exit code: $?"
