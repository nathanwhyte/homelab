# Cluster Hardware Inventory

Audited 2026-03-18. 5-node K3s cluster running Ubuntu 24.04 LTS.

## Nodes

### manu (Worker)
| Component | Details |
|-----------|---------|
| **CPU** | AMD Ryzen 7 1700 (8C/16T) |
| **RAM** | 16 GB |
| **Storage** | 2x Samsung SSD 860 EVO 1TB (SATA) |
| **GPU** | NVIDIA GeForce GTX 1080 (8 GB VRAM) |
| **Kernel** | 6.17.0-19-generic |

### patty (Worker)
| Component | Details |
|-----------|---------|
| **CPU** | Intel Core i5-7200U (2C/4T, 2.50 GHz) |
| **RAM** | 8 GB |
| **Storage** | Seagate ST1000LM035 1TB (SATA HDD) |
| **GPU** | None |
| **Kernel** | 6.8.0-101-generic |

### steph (Worker)
| Component | Details |
|-----------|---------|
| **CPU** | Intel Core i5-10210U (4C/8T, 1.60 GHz) |
| **RAM** | 12 GB |
| **Storage** | Samsung MZVLB256HBHQ 256 GB (NVMe) |
| **GPU** | None |
| **Kernel** | 6.8.0-106-generic |

### timmy (Worker)
| Component | Details |
|-----------|---------|
| **CPU** | AMD Ryzen 7 7800X3D (8C/16T) |
| **RAM** | 32 GB |
| **Storage** | WD Green SN3000 2TB (NVMe) |
| **GPU** | AMD Radeon RX 9070 XT (16 GB VRAM, RDNA 4) |
| **Kernel** | 6.17.0-19-generic |

### wemby (Worker + Control Plane)
| Component | Details |
|-----------|---------|
| **CPU** | Intel Core i7-8750H (6C/12T, 2.20 GHz) |
| **RAM** | 16 GB |
| **Storage** | WDC PC SN520 256 GB (NVMe) + Seagate ST1000LM035 1TB (SATA HDD) |
| **GPU** | NVIDIA GeForce GTX 1060 (6 GB VRAM) |
| **Kernel** | 6.8.0-106-generic |

## Cluster Totals

| Resource | Total |
|----------|-------|
| **CPU Threads** | 56 |
| **RAM** | ~84 GB |
| **Raw Storage** | ~6.5 TB |
| **Discrete GPUs** | 3 (GTX 1080, GTX 1060, RX 9070 XT) |
| **Total VRAM** | 30 GB |
