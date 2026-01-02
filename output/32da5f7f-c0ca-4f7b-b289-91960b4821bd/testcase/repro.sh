#!/bin/bash
set -e

# Navigate to the directory where this script resides
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

# Test 1: No leak file present. Backup if exists.
if [ -f leak.jng ]; then
    mv leak.jng leak.jng.bak
fi

echo "=== Test 1: Running Docker command without leak file ==="

# Run the docker command; replace 'imagick_leak' with the appropriate Docker image name if needed.
set +e
OUTPUT="$(bash ./docker run --rm imagick_leak 2>OUTPUT=$(bash ./docker run --rm imagick_leak 2>&1)1)"
status=$?
set -e

if [ $status -eq 0 ]; then
    echo "Error: Expected Docker command to fail (non-zero exit code) when leak file is missing."
    exit 1
else
    echo "Test 1 passed: Non-zero exit code as expected."
fi

# Test 2: Leak file present.

echo "=== Test 2: Creating leak file and running Docker command ==="

echo "dummy" > leak.jng

set +e
OUTPUT="$(bash ./docker run --rm imagick_leak 2>OUTPUT=$(bash ./docker run --rm imagick_leak 2>&1)1)"
status=$?
set -e

if [ $status -ne 0 ]; then
    echo "Error: Expected Docker command to succeed (exit code 0) when leak file is present, but got exit code $status."
    exit 1
fi

if echo "$OUTPUT" | grep -q "ReadOneJNGImage"; then
    echo "Test 2 passed: LSAN leak report contains 'ReadOneJNGImage'."
else
    echo "Error: 'ReadOneJNGImage' not found in LSAN leak report."
    exit 1
fi

# Cleanup: Remove the dummy leak file and restore backup if it existed.
rm -f leak.jng
[ -f leak.jng.bak ] && mv leak.jng.bak leak.jng

exit 0
