#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
CARGO="${CARGO:-$HOME/.cargo/bin/cargo}"
if [ ! -x "$CARGO" ]; then
    echo "cargo not found at $CARGO — set CARGO or install via rustup" >&2
    exit 1
fi
"$CARGO" build --release --manifest-path chip_rs/Cargo.toml
cp chip_rs/target/release/libchip_rs.so chip_rs.so
echo "built chip_rs.so"