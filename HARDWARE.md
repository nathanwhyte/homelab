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

Timmy's RX 9070 XT (`gfx1201`) runs Ollama **and** `embedder-qwen` (Qwen3-Embedding-4B, ROCm backend, primary since 2026-07-06 — migrated back from wemby CUDA after wemby's power kept dropping; `embedder-qwen-cuda` on wemby is the replicas=0 rollback). The card hosts both (~5 GB embedder incl. KV alongside Ollama). Rules for maintaining the ROCm serving path (apply to both Ollama and `embedder-qwen`):

| Rule                       | Detail                                                                                                                                                                                                                                                     |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| GPU visibility             | `HIP_VISIBLE_DEVICES=0` is mandatory in all ROCm manifests. Prevents iGPU (`gfx1036`) selection.                                                                                                                                                           |
| `HSA_OVERRIDE_GFX_VERSION` | Do NOT set. The 9070 XT is natively `gfx1201`; overriding masks real compatibility failures.                                                                                                                                                               |
| rocBLAS hostPath           | Mount `/opt/rocm-7.2.1/lib/rocblas/library` only. Do NOT mount hipBLASLt (`/opt/rocm-7.2.1/lib/hipblaslt/library`) without a version-aligned benchmark.                                                                                                    |
| `ROCBLAS_USE_HIPBLASLT`    | Do NOT set. Benchmarked 2026-06-28: no performance difference, no warnings to suppress.                                                                                                                                                                    |
| Image upgrades             | Before bumping any ROCm image tag, run the gfx1201 validation commands in `llama/docs/2026-06-28-rocm-gfx1201-validation-baseline.md`.                                                                                                                     |
| vLLM                       | Experimental only — use a separate manifest/branch, not production Ollama. Verify `torch.cuda.is_available()` and `torch.cuda.device_count()` inside the container. For PyTorch/vLLM, also set `CUDA_VISIBLE_DEVICES=0` alongside `HIP_VISIBLE_DEVICES=0`. |
| ROCm tooling               | Prefer `amd-smi` over legacy `rocm-smi`; prefer `rocprofv3` over legacy `rocprof`.                                                                                                                                                                         |
| Host ROCm                  | Current baseline is ROCm 7.2.1. Do not upgrade without a benchmark justification.                                                                                                                                                                          |
