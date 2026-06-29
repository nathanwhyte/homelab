# ROCm gfx1201 Validation Baseline

Recorded 2026-06-28 as part of IMPR-1011 (harden timmy RDNA4 ROCm serving configuration).

## Environment

| Property | Value |
|---|---|
| Node | timmy |
| GPU | AMD Radeon RX 9070 XT (16 GB, `gfx1201`) |
| Host ROCm | 7.2.1 (`/opt/rocm-7.2.1/`) |
| Kernel | 6.17.0-35-generic |
| iGPU | AMD Ryzen 7 7800X3D (`gfx1036`) — hidden by `HIP_VISIBLE_DEVICES=0` |

## Image: `ollama/ollama:0.30.6-rocm`

Ollama bundles its own ROCm at `/usr/lib/ollama/rocm_v7_2/`. System `rocminfo` is not in PATH.

### gfx1201 library support

- `TensileLibrary_lazy_gfx1201.dat`: present at `/usr/lib/ollama/rocm_v7_2/rocblas/library/`
- `Kernels.so-000-gfx1201.hsaco`: present
- 55+ gfx1201 contraction hsaco files present (HH, CC, SS, DD, ZZ, I8I, 4xi8I, BB, BS, HS types)

### hipBLASLt warnings

No hipBLASLt warnings in pod logs at time of validation.

### Notes

- The manifest mounts host rocBLAS at `/opt/rocm/lib/rocblas/library`, but Ollama uses its internal library at `/usr/lib/ollama/rocm_v7_2/rocblas/library/`. The host mount provides a fallback path but may not be actively consumed.
- hipBLASLt library is NOT mounted from the host (by design).

## Image: `ghcr.io/ggml-org/llama.cpp:server-rocm`

Uses system ROCm from the host mount. Full `rocminfo` and HIP runtime available.

### rocminfo output

```text
Name:                    gfx1201
Marketing Name:          AMD Radeon RX 9070 XT
    Name:                    amdgcn-amd-amdhsa--gfx1201
    Name:                    amdgcn-amd-amdhsa--gfx12-generic
```

Also visible (but filtered by `HIP_VISIBLE_DEVICES=0`):

```text
Name:                    gfx1036
Marketing Name:          AMD Ryzen 7 7800X3D 8-Core Processor
    Name:                    amdgcn-amd-amdhsa--gfx1036
```

### rocm_agent_enumerator

```text
gfx1201
gfx1036
```

### llama-server --list-devices

```text
Available devices:
  ROCm0: AMD Radeon RX 9070 XT (16304 MiB, 4816 MiB free)
```

Only the discrete GPU is visible — `HIP_VISIBLE_DEVICES=0` correctly hides the iGPU.

### gfx1201 library support

- `TensileLibrary_lazy_gfx1201.dat`: present at `/opt/rocm-7.2.1/lib/rocblas/library/` (host mount)
- hipBLASLt gfx1201 data also present on host at `/opt/rocm-7.2.1/lib/hipblaslt/library/` (NOT mounted into the container)
- ROCm version: 7.2.1
- HIP runtime: `libamdhip64.so.7.2.70201`

### Notes

- The embedder image uses the host ROCm stack (mounted via hostPath), unlike Ollama which bundles its own.
- Both rocBLAS and hipBLASLt gfx1201 TensileLibrary data exist on the host, but only rocBLAS is mounted.

## Pre-upgrade Validation Commands

Run these before bumping any ROCm image tag:

```bash
# 1. Verify gfx1201 GPU detection
rocminfo | grep -E 'Name:|Marketing Name:|amdgcn-amd-amdhsa--gfx'

# 2. Confirm device enumeration
/opt/rocm/bin/rocm_agent_enumerator 2>/dev/null || true

# 3. Verify llama-server sees only the discrete GPU (embedder only)
/app/llama-server --list-devices 2>&1 | grep -E 'ROCm|gfx1201|9070'

# 4. Check for gfx1201 TensileLibrary data
find /usr /opt -name 'TensileLibrary_lazy_gfx1201.dat' 2>/dev/null

# 5. Check for gfx1201 kernel objects
find /usr /opt -name '*gfx1201*.hsaco' 2>/dev/null | head -5
```

Acceptance: all 5 commands should produce non-empty output. Command 3 should show only the RX 9070 XT, not the iGPU.

## hipBLASLt Benchmark (2026-06-28)

A/B test of `ROCBLAS_USE_HIPBLASLT=0` on Ollama with `gemma4:12b-it-qat` on the RX 9070 XT. Embedder was scaled to 0 to give Ollama exclusive GPU access. Each config ran 3 times with a warm model; median values reported.

| Metric | Baseline (current) | ROCBLAS_USE_HIPBLASLT=0 | Delta |
|---|---|---|---|
| Prompt eval (tok/s) | 1333.2 | 1319.4 | -1.0% |
| Generation (tok/s) | 49.75 | 49.74 | -0.02% |
| hipBLASLt warnings | 0 | 0 | — |

### All runs

Baseline:

| Run | Prompt eval (tok/s) | Gen (tok/s) |
|---|---|---|
| 1 | 1343.6 | 49.95 |
| 2 | 1333.2 | 49.60 |
| 3 | 1306.2 | 49.75 |

ROCBLAS_USE_HIPBLASLT=0:

| Run | Prompt eval (tok/s) | Gen (tok/s) |
|---|---|---|
| 1 | 1319.4 | 49.54 |
| 2 | 1312.1 | 49.91 |
| 3 | 1331.9 | 49.74 |

### Decision

**Do NOT add `ROCBLAS_USE_HIPBLASLT=0` permanently.** The difference is within measurement noise (-1.0% prompt, -0.02% gen). No hipBLASLt warnings were observed in either configuration. Adding the env var would be cargo-cult configuration with no measurable benefit.

The current setup — mounting only host rocBLAS (not hipBLASLt) and relying on the image's internal fallback path — is the correct configuration for `ollama/ollama:0.30.6-rocm` on the RX 9070 XT.
