#!/usr/bin/env bash
# Build the native acceleration core used by chip_web.py.
set -euo pipefail
cd "$(dirname "$0")"
g++ -O2 -std=c++17 -fPIC -shared -pthread chip_native.cpp -o libchip_native.so
echo "built: $(pwd)/libchip_native.so"