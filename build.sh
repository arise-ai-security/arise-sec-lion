#!/bin/bash -eu
# Standalone build script with address sanitizer enabled
set -eu

# Export environment variables for sanitizers and compiler
export CC=clang
export CXX=clang++
export CFLAGS="-fsanitize=address -fsanitize-address-use-after-scope"
export CXXFLAGS="-fsanitize=address -fsanitize-address-use-after-scope"

# Create build directory with -p flag to prevent errors
mkdir -p build
cd build

# Clean before building (if target exists)
if [ -f Makefile ]; then
    make clean || echo "Clean failed, continuing"
fi

# Configure project with explicit paths and toolchain
cmake \
  -DCMAKE_C_COMPILER=clang \
  -DCMAKE_CXX_COMPILER=clang++ \
  -DCMAKE_C_FLAGS="${CFLAGS}" \
  -DCMAKE_CXX_FLAGS="${CXXFLAGS}" \
  ..

# Build with multiple processors
make -j$(nproc)