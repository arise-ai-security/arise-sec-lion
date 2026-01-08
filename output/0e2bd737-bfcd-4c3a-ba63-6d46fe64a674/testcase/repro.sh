#!/bin/bash

# Ensure the output directory exists
mkdir -p testcase

# Set the Docker image name; update this if the supplied image name differs
DOCKER_IMAGE="exiv2_sanitizer:latest"

# Run the exiv2 command inside the Docker container, capturing AddressSanitizer output
# The current directory is mounted into /workspace and used as the working directory
# ASan evidence (trimmed):
# ==2826==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x60300000edb9 ... READ of size 1

docker run --rm -v "$(pwd)":/workspace -w /workspace $DOCKER_IMAGE ./exiv2 poc.png > testcase/asan_log.txt 2>&1
