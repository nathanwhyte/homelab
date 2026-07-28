"""GPU and model-state metric helpers for the benchmarking harness.

All queries are async so they can be sampled concurrently with a benchmark run.
The module supports:

- AMD GPU metrics via Prometheus (amdgpu-exporter)
- AMD GPU metrics via the local amdsmi Python library (RDNA4/RDNA3)
- NVIDIA GPU metrics via Prometheus (DCGM)
- Ollama /api/ps as a VRAM fallback
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class GpuSample:
    timestamp_ms: int
    vram_bytes: Optional[int] = None
    vram_total_bytes: Optional[int] = None
    power_watts: Optional[float] = None
    temp_celsius: Optional[float] = None
    temp_mem_celsius: Optional[float] = None
    gpu_util_percent: Optional[float] = None
    mem_util_percent: Optional[float] = None
    fan_rpm: Optional[int] = None
    throttle: Optional[bool] = None
    voltage_gfx_mv: Optional[int] = None
    voltage_mem_mv: Optional[int] = None
    clock_gfx_mhz: Optional[int] = None
    clock_mem_mhz: Optional[int] = None
    source: str = "unknown"


@dataclass
class GpuSeries:
    metric: str
    instance: Optional[str] = None
    gpu: Optional[str] = None
    samples: list[GpuSample] = field(default_factory=list)


async def _prom_query(
    session: Any,
    prom_url: str,
    query: str,
    timeout: float = 10.0,
) -> Optional[list[dict]]:
    """Run an instant PromQL query and return the 'result' list."""
    try:
        import aiohttp
    except ImportError as exc:  # pragma: no cover - runtime dependency hint
        raise SystemExit(
            "aiohttp is required. Run: uv run --with aiohttp python ..."
        ) from exc

    try:
        async with session.get(
            f"{prom_url}/api/v1/query",
            params={"query": query},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            data = await resp.json()
    except Exception:
        return None
    if data.get("status") != "success":
        return None
    return data.get("data", {}).get("result", [])


async def sample_amd_gpu(
    session: Any,
    prom_url: str,
    instance_label: Optional[str] = None,
) -> GpuSeries:
    """Sample AMD GPU VRAM, power, and temperature from amdgpu-exporter."""
    matchers = ""
    if instance_label:
        matchers += f',instance=~"{instance_label}"'

    vram_results = await _prom_query(
        session, prom_url, f"amdgpu_vram_used_bytes{{{matchers.lstrip(',')}}}"
    )
    power_results = await _prom_query(
        session, prom_url, f"amdgpu_power_watts{{{matchers.lstrip(',')}}}"
    )
    temp_results = await _prom_query(
        session,
        prom_url,
        f"amdgpu_junction_temperature_celsius{{{matchers.lstrip(',')}}}",
    )

    def _first_value(results: Optional[list[dict]]) -> Optional[float]:
        if not results:
            return None
        try:
            return float(results[0]["value"][1])
        except (KeyError, ValueError, IndexError):
            return None

    return GpuSeries(
        metric="amdgpu",
        instance=instance_label,
        samples=[
            GpuSample(
                timestamp_ms=_now_ms(),
                vram_bytes=int(v)
                if (v := _first_value(vram_results)) is not None
                else None,
                power_watts=_first_value(power_results),
                temp_celsius=_first_value(temp_results),
                source="prometheus:amdgpu-exporter",
            )
        ],
    )


async def sample_nvidia_gpu(
    session: Any,
    prom_url: str,
    instance_label: Optional[str] = None,
) -> GpuSeries:
    """Sample NVIDIA GPU VRAM, power, and temperature from DCGM."""
    matchers = ""
    if instance_label:
        matchers += f',instance=~"{instance_label}"'

    vram_results = await _prom_query(
        session, prom_url, f"DCGM_FI_DEV_FB_USED{{{matchers.lstrip(',')}}}"
    )
    power_results = await _prom_query(
        session, prom_url, f"DCGM_FI_DEV_POWER_USAGE{{{matchers.lstrip(',')}}}"
    )
    temp_results = await _prom_query(
        session, prom_url, f"DCGM_FI_DEV_GPU_TEMP{{{matchers.lstrip(',')}}}"
    )

    def _first_value(results: Optional[list[dict]]) -> Optional[float]:
        if not results:
            return None
        try:
            return float(results[0]["value"][1])
        except (KeyError, ValueError, IndexError):
            return None

    return GpuSeries(
        metric="dcgm",
        instance=instance_label,
        samples=[
            GpuSample(
                timestamp_ms=_now_ms(),
                vram_bytes=int(v)
                if (v := _first_value(vram_results)) is not None
                else None,
                power_watts=_first_value(power_results),
                temp_celsius=_first_value(temp_results),
                source="prometheus:dcgm",
            )
        ],
    )


def _find_discrete_amd_gpu():
    """Return the first discrete AMD GPU handle using amdsmi VRAM size.

    Returns None if amdsmi is unavailable or no discrete GPU is found.
    Skips APUs/iGPUs by requiring > 1 GiB of VRAM.
    """
    try:
        import amdsmi
    except Exception:
        return None

    try:
        amdsmi.amdsmi_init()
        handles = amdsmi.amdsmi_get_processor_handles()
        for handle in handles:
            try:
                vram_info = amdsmi.amdsmi_get_gpu_vram_info(handle)
                vram_mb = vram_info.get("vram_size") or 0
                if vram_mb > 1024:
                    return handle
            except Exception:
                continue
        return None
    except Exception:
        return None


def _sample_amdsmi_once(handle: Any) -> Optional[GpuSample]:
    """Synchronous sample of one AMD GPU via amdsmi."""
    try:
        import amdsmi
    except Exception:
        return None

    try:
        bdf = amdsmi.amdsmi_get_gpu_device_bdf(handle)
        metrics = amdsmi.amdsmi_get_gpu_metrics_info(handle)
        vram_info = amdsmi.amdsmi_get_gpu_vram_info(handle)
    except Exception:
        return None

    def _n(v):
        """Return a numeric value or None if it's 'N/A' or missing."""
        if v is None or v == "N/A":
            return None
        try:
            return float(v) if isinstance(v, (int, float, str)) else None
        except (TypeError, ValueError):
            return None

    def _b(v):
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return GpuSample(
        timestamp_ms=_now_ms(),
        vram_total_bytes=_b((vram_info.get("vram_size") or 0) * 1024 * 1024)
        if vram_info
        else None,
        power_watts=_n(metrics.get("average_socket_power")),
        temp_celsius=_n(metrics.get("temperature_hotspot")),
        temp_mem_celsius=_n(metrics.get("temperature_mem")),
        gpu_util_percent=_n(metrics.get("average_gfx_activity")),
        mem_util_percent=_n(metrics.get("average_umc_activity")),
        fan_rpm=_b(metrics.get("current_fan_speed")),
        throttle=metrics.get("throttle_status")
        if metrics.get("throttle_status") is not None
        else None,
        voltage_gfx_mv=_b(metrics.get("voltage_gfx")),
        voltage_mem_mv=_b(metrics.get("voltage_mem")),
        clock_gfx_mhz=_b(metrics.get("current_gfxclk")),
        clock_mem_mhz=_b(metrics.get("current_uclk")),
        source=f"amdsmi:{bdf}",
    )


async def sample_amdsmi_gpu(handle: Optional[Any] = None) -> GpuSeries:
    """Sample AMD GPU metrics via the local amdsmi Python library.

    If ``handle`` is not provided, the first discrete GPU (VRAM > 1 GiB) is
    selected. This avoids picking up the integrated APU on systems like timmy.
    Returns an empty series if amdsmi is unavailable or fails.
    """
    if handle is None:
        handle = _find_discrete_amd_gpu()

    sample = None
    if handle is not None:
        sample = await asyncio.to_thread(_sample_amdsmi_once, handle)

    return GpuSeries(
        metric="amdsmi",
        gpu=None,
        samples=[sample] if sample is not None else [],
    )


async def sample_ollama_ps(
    session: Any,
    ollama_url: str,
    model_name: Optional[str] = None,
) -> GpuSeries:
    """Sample Ollama /api/ps VRAM for the requested model (or all models)."""
    try:
        import aiohttp
    except ImportError as exc:  # pragma: no cover - runtime dependency hint
        raise SystemExit(
            "aiohttp is required. Run: uv run --with aiohttp python ..."
        ) from exc

    total_vram = None
    try:
        async with session.get(
            f"{ollama_url}/api/ps", timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
        models = data.get("models", [])
        if model_name:
            for m in models:
                name = m.get("name", "")
                if model_name in name or name.startswith(model_name.split(":")[0]):
                    total_vram = m.get("size_vram")
                    break
        else:
            total_vram = sum((m.get("size_vram") or 0) for m in models)
    except Exception:
        pass

    return GpuSeries(
        metric="ollama_ps",
        instance=ollama_url,
        samples=[
            GpuSample(
                timestamp_ms=_now_ms(),
                vram_bytes=int(total_vram) if total_vram is not None else None,
                source="ollama:/api/ps",
            )
        ],
    )


async def sample_all(
    session: Any,
    prom_url: Optional[str] = None,
    ollama_url: Optional[str] = None,
    model_name: Optional[str] = None,
    instance_label: Optional[str] = None,
    use_amdsmi: bool = False,
) -> list[GpuSeries]:
    """Sample every available metric source concurrently."""
    tasks = []
    if use_amdsmi:
        tasks.append(sample_amdsmi_gpu())
    if prom_url:
        tasks.append(sample_amd_gpu(session, prom_url, instance_label))
        tasks.append(sample_nvidia_gpu(session, prom_url, instance_label))
    if ollama_url:
        tasks.append(sample_ollama_ps(session, ollama_url, model_name))
    if not tasks:
        return []
    return await asyncio.gather(*tasks)


async def sample_during(
    session: Any,
    duration_s: float,
    interval_s: float,
    prom_url: Optional[str] = None,
    ollama_url: Optional[str] = None,
    model_name: Optional[str] = None,
    instance_label: Optional[str] = None,
    use_amdsmi: bool = False,
    stop_event: Optional[asyncio.Event] = None,
):
    """Sample GPU metrics repeatedly during a benchmark window.

    Returns one GpuSeries per metric, each containing all samples merged by
    metric name. The caller can use the raw sample list for time-series plots.

    Pass `stop_event` to run this concurrently with the workload and end
    sampling the moment the workload does: callers cannot know the window
    length in advance, and sampling *after* the workload measures an idle GPU.
    Samples collected before the stop are returned normally.
    """
    merged: dict[str, GpuSeries] = {}
    end = time.monotonic() + duration_s
    while time.monotonic() < end:
        if stop_event is not None and stop_event.is_set():
            break
        series_list = await sample_all(
            session, prom_url, ollama_url, model_name, instance_label, use_amdsmi
        )
        for series in series_list:
            if series.metric not in merged:
                merged[series.metric] = GpuSeries(
                    metric=series.metric,
                    instance=series.instance,
                    gpu=series.gpu,
                )
            merged[series.metric].samples.extend(series.samples)
        # Interruptible sleep. A plain asyncio.sleep() would run the full
        # interval before noticing the stop, and the caller awaits this task
        # before closing its timing window — so an uninterruptible sleep adds
        # up to a whole interval of idle time to every measured repeat.
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
                break  # stop_event fired during the interval
            except asyncio.TimeoutError:
                continue  # interval elapsed normally, keep sampling
        await asyncio.sleep(interval_s)
    return list(merged.values())
