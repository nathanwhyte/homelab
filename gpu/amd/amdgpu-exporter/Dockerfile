FROM ubuntu:24.04

# amd-smi-lib depends on Python3 and the Debian libdrm-amdgpu runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3-pip \
        libdrm2 \
        libdrm-amdgpu1 \
    && rm -rf /var/lib/apt/lists/*

# ROCm amdsmi library + CLI bundled from the host.  The tarball is produced
# by `gpu/amd/build-rocm-amdsmi-tar.sh` on a node with ROCm installed.
COPY rocm-amdsmi.tar.gz /tmp/
RUN tar -xzf /tmp/rocm-amdsmi.tar.gz -C / && rm /tmp/rocm-amdsmi.tar.gz

ENV LD_LIBRARY_PATH=/opt/rocm/lib:/opt/rocm/lib64:/usr/lib/x86_64-linux-gnu
ENV ROCM_PATH=/opt/rocm

# Install the pure-Python amdsmi wrapper.  The package will find
# libamd_smi.so under /opt/rocm/lib via ROCM_PATH / LD_LIBRARY_PATH.
RUN pip install --no-cache-dir --break-system-packages amdsmi

WORKDIR /app
