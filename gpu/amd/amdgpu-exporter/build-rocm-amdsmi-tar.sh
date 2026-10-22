#!/usr/bin/env bash
# Bundle the ROCm amdsmi library and its host-side libdrm dependencies
# so the exporter image can be built on a machine without a full ROCm install.
set -euo pipefail

OUTPUT="${1:-/tmp/rocm-amdsmi.tar.gz}"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

mkdir -p "$TMPDIR/opt/rocm/lib" "$TMPDIR/usr/lib/x86_64-linux-gnu"

# ROCm SMI library from the currently active ROCm stack.
cp -P /opt/rocm/lib/libamd_smi.so* "$TMPDIR/opt/rocm/lib/"

# libdrm runtime dependencies required by libamd_smi.so.
cp -P /usr/lib/x86_64-linux-gnu/libdrm.so* "$TMPDIR/usr/lib/x86_64-linux-gnu/"
cp -P /usr/lib/x86_64-linux-gnu/libdrm_amdgpu.so* "$TMPDIR/usr/lib/x86_64-linux-gnu/"

# C/C++ runtime libraries the bundled libdrm/libamd_smi are linked against.
cp -P /usr/lib/x86_64-linux-gnu/libstdc++.so* "$TMPDIR/usr/lib/x86_64-linux-gnu/"
cp -P /usr/lib/x86_64-linux-gnu/libgcc_s.so.1 "$TMPDIR/usr/lib/x86_64-linux-gnu/"
cp -P /usr/lib/x86_64-linux-gnu/libm.so.6 "$TMPDIR/usr/lib/x86_64-linux-gnu/"

tar -czf "$OUTPUT" -C "$TMPDIR" .
echo "Wrote $OUTPUT"
