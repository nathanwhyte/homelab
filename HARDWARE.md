# Cluster Hardware Inventory

Audited 2026-07-02. 3-node K3s cluster running Ubuntu 24.04.4 LTS, K3s v1.35.5+k3s1 (timmy, wemby) / v1.35.2+k3s1 (manu).

## Nodes

### manu (Agent)

| Component   | Details                             |
| ----------- | ----------------------------------- |
| **CPU**     | AMD Ryzen 7 1700 (8C/16T)           |
| **RAM**     | 16 GB                               |
| **Storage** | 2x Samsung SSD 860 EVO 1TB (SATA)   |
| **GPU**     | NVIDIA GeForce GTX 1080 (8 GB VRAM) |
| **Kernel**  | 6.17.0-35-generic                   |

### timmy (Server — Control Plane + Worker)

| Component   | Details                                    |
| ----------- | ------------------------------------------ |
| **CPU**     | AMD Ryzen 7 7800X3D (8C/16T)               |
| **RAM**     | 32 GB                                      |
| **Storage** | WD Green SN3000 2TB (NVMe)                 |
| **GPU**     | AMD Radeon RX 9070 XT (16 GB VRAM, RDNA 4) |
| **Kernel**  | 6.17.0-35-generic                          |

### wemby (Agent)

| Component   | Details                                                         |
| ----------- | --------------------------------------------------------------- |
| **CPU**     | Intel Core i7-8750H (6C/12T, 2.20 GHz)                          |
| **RAM**     | 16 GB                                                           |
| **Storage** | WDC PC SN520 256 GB (NVMe) + Seagate ST1000LM035 1TB (SATA HDD) |
| **GPU**     | NVIDIA GeForce GTX 1060 (6 GB VRAM)                             |
| **Kernel**  | 6.8.0-124-generic                                               |

## Retired Nodes

### patty (Removed 2026-03-20)

| Component   | Details                               |
| ----------- | ------------------------------------- |
| **CPU**     | Intel Core i5-7200U (2C/4T, 2.50 GHz) |
| **RAM**     | 8 GB                                  |
| **Storage** | Seagate ST1000LM035 1TB (SATA HDD)    |
| **GPU**     | None                                  |
| **Kernel**  | 6.8.0-101-generic                     |

### steph (Removed 2026-03-20)

| Component   | Details                                |
| ----------- | -------------------------------------- |
| **CPU**     | Intel Core i5-10210U (4C/8T, 1.60 GHz) |
| **RAM**     | 12 GB                                  |
| **Storage** | Samsung MZVLB256HBHQ 256 GB (NVMe)     |
| **GPU**     | None                                   |
| **Kernel**  | 6.8.0-106-generic                      |

## Cluster Totals

| Resource          | Total                              |
| ----------------- | ---------------------------------- |
| **CPU Threads**   | 44                                 |
| **RAM**           | ~64 GB                             |
| **Raw Storage**   | ~5.25 TB                           |
| **Discrete GPUs** | 3 (GTX 1080, GTX 1060, RX 9070 XT) |
| **Total VRAM**    | 30 GB                              |

## Workbook (operator workstation)

MacBook Pro (M4 Pro) used as a secondary development/Ollama host. Runs macOS 25.5.0; not a K3s cluster node.

| Component   | Details                                         |
| ----------- | ----------------------------------------------- |
| **CPU**     | Apple M4 Pro (12-core: 8P + 4E)                 |
| **RAM**     | 24 GB unified                                   |
| **Storage** | 494 GB SSD (~120 GB free, ~76% used)            |
| **GPU**     | Apple M4 Pro 16-core integrated / Neural Engine |
| **Display** | PG32UCDP 4K @ 120 Hz                            |
| **Network** | 192.168.1.16 (LAN), WiFi + Ethernet             |

## AMD / RDNA4 guardrails (timmy's RX 9070 XT)

Timmy's RX 9070 XT (`gfx1201`) serves Ollama over **Vulkan/RADV**. ROCm was removed
from the host on 2026-09-04 (IMPR-1088): all 66 ROCm packages plus `amdgpu-dkms`
were purged, and the kernel driver is now the **in-tree** `amdgpu`, not the ROCm
DKMS build that broke kernel 7.0 in BUG-1052. `embedder-qwen` (the ROCm-image
Deployment on this node) is `replicas=0` and is not the embedder — the primary is
`embedder-qwen-cuda` on **manu**'s GTX 1080 since 2026-07-17 (IMPR-1077).

Rules for the current Vulkan serving path:

| Rule                       | Detail                                                                                                                                                                                                                                                                                                                                   |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| GPU visibility             | `GGML_VK_VISIBLE_DEVICES=0` selects the discrete card. Vulkan/RADV enumerates in **PCI order**, so the 9070 XT (`03:00.0`, `renderD128`) is device 0 and the Raphael iGPU (`13:00.0`, `renderD129`) is device 1 — the same order as ROCm, **not** reversed. A wrong index fails **silently as a CPU fallback**, not an error (BUG-1094). |
| Verify after any change    | `ollama ps` must show `100% GPU`, and the logs must show `library=Vulkan … description="AMD Radeon RX 9070 XT (RADV GFX1201)" type=discrete`. `ollama ps` alone cannot distinguish backends — always confirm in the logs.                                                                                                                |
| Vulkan implementation      | RADV from stock Ubuntu `mesa-vulkan-drivers` (owns `/usr/share/vulkan/icd.d/radeon_icd.json`). AMD ships no alternative — upstream removed the `amdvlk` and `pro` options, leaving `--vulkan=radv` only.                                                                                                                                 |
| VRAM reporting             | Ollama needs root or `cap_perfmon` to read real available VRAM; without it, it falls back to approximate model sizes for scheduling. The in-cluster pod has it — if logs start showing round approximations rather than figures like `available="15.4 GiB"`, that capability was lost.                                                   |
| Kernel driver              | In-tree `amdgpu` only. Do **not** reinstall `amdgpu-dkms` — it is the BUG-1052 mechanism and is what pinned this node off the HWE kernel track.                                                                                                                                                                                          |
| `HSA_OVERRIDE_GFX_VERSION` | Do NOT set. The 9070 XT is natively `gfx1201`; overriding masks real compatibility failures.                                                                                                                                                                                                                                             |
| vLLM                       | Experimental only — use a separate manifest/branch, not production Ollama. Needs ROCm, which is no longer installed; reinstating it would reintroduce the DKMS kernel pin. INFO-1071 recommends holding until native gfx1201 FP8 kernels land.                                                                                           |

**If ROCm is ever reinstated** (it would re-pin this node off the HWE track), the
pre-2026-09-04 rules were: `HIP_VISIBLE_DEVICES=0` mandatory in every ROCm
manifest; mount `/opt/rocm-7.2.1/lib/rocblas/library` only, never hipBLASLt,
without a version-aligned benchmark; do not set `ROCBLAS_USE_HIPBLASLT`
(benchmarked 2026-06-28, no difference); run the gfx1201 validation commands in
`llama/docs/2026-06-28-rocm-gfx1201-validation-baseline.md` before bumping any
ROCm image tag; prefer `amd-smi`/`rocprofv3` over `rocm-smi`/`rocprof`; baseline
was ROCm 7.2.1.
