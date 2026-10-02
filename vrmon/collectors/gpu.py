"""GPU collector. Tracks utilization, encoder/decoder utilization (e.g. a
stream or recording encode running alongside the sim), VRAM, and - given
the iRacing process's PID - how much of the load and VRAM is iRacing's
own (vs. anything else running concurrently).

Sources, combined in priority order - each fills in whatever the ones
before it couldn't report:
- On NVIDIA, NVML (nvidia-ml-py): temperature, clocks, power draw and
  throttle reasons as well as load. Preferred where it has a value, for
  continuity with sessions recorded before the other sources existed.
- Otherwise (AMD, Intel), LibreHardwareMonitor (gpu_lhm.py): temperature,
  power draw and core clock, if setup has installed it.
- Windows' own GPU performance counters (gpu_windows.py), any vendor:
  load, iRacing's share, encoder/decoder, VRAM - including per-process
  VRAM, which NVML doesn't provide under WDDM.

"gpu_stats" in data/settings.json: "auto" (the above), "nvml" or
"windows" (that source only), or "lhm" (LibreHardwareMonitor + Windows
counters even on NVIDIA - to compare sources or test the AMD/Intel path).
"""

import logging
import time

import pynvml

from vrmon import config
from vrmon.collectors.gpu_lhm import LhmGpuCollector
from vrmon.collectors.gpu_windows import WindowsGpuCollector

log = logging.getLogger(__name__)

# Reasons that mean "something is actively capping clocks below what the
# workload wants" - worth flagging. GpuIdle/DisplayClockSetting/
# ApplicationsClocksSetting/UserDefinedClocks reflect idle state or an
# explicit clock policy, not an unexpected limiter, so they're excluded
# from the "throttled" flag (still visible in the raw reason list).
_LIMITING_REASONS = {
    "SwPowerCap": pynvml.nvmlClocksThrottleReasonSwPowerCap,
    "HwSlowdown": pynvml.nvmlClocksThrottleReasonHwSlowdown,
    "HwThermalSlowdown": pynvml.nvmlClocksThrottleReasonHwThermalSlowdown,
    "SwThermalSlowdown": pynvml.nvmlClocksThrottleReasonSwThermalSlowdown,
    "HwPowerBrakeSlowdown": pynvml.nvmlClocksThrottleReasonHwPowerBrakeSlowdown,
    "SyncBoost": pynvml.nvmlClocksThrottleReasonSyncBoost,
}


class GpuCollector:
    """Combines the available sources per the gpu_stats setting (see the
    module docstring). Raises if none can be opened."""

    def __init__(self):
        choice = config.GPU_STATS
        nvml = lhm = windows = None

        if choice in ("auto", "nvml"):
            try:
                nvml = NvmlGpuCollector()
            except Exception:
                if choice == "nvml":
                    raise
                log.info("NVML unavailable (not an NVIDIA GPU, or no driver)")

        if choice in ("auto", "windows", "lhm"):
            try:
                windows = WindowsGpuCollector()
            except Exception:
                if nvml is None and choice != "lhm":
                    raise
                log.warning("Windows GPU counters unavailable", exc_info=True)

        # NVML already has temperature/power/clocks, so LibreHardwareMonitor
        # is only needed without it (or when explicitly asked for).
        if choice == "lhm" or (choice == "auto" and nvml is None):
            try:
                lhm = LhmGpuCollector(prefer_name=windows.name if windows else None)
            except Exception as e:
                if choice == "lhm":
                    raise
                log.info("GPU temperature/power unavailable without NVIDIA's NVML: %s", e)

        self._sources = [s for s in (nvml, lhm, windows) if s is not None]
        if not self._sources:
            raise RuntimeError("no GPU stats source could be opened")
        self.name = (nvml or windows or lhm).name
        self.backend = "+".join(s.backend for s in self._sources)

    def sample(self, target_pid: int | None = None) -> dict:
        result = {}
        for source in self._sources:
            try:
                values = source.sample(target_pid)
            except Exception:
                log.debug("%s GPU sample failed", source.backend, exc_info=True)
                continue
            for key, value in values.items():
                if result.get(key) is None:
                    result[key] = value
        result.setdefault("throttle_reasons", [])
        return result

    def shutdown(self) -> None:
        for source in self._sources:
            source.shutdown()


class NvmlGpuCollector:
    backend = "nvml"

    def __init__(self, device_index: int = 0):
        pynvml.nvmlInit()
        self.handle = pynvml.nvmlDeviceGetHandleByIndex(device_index)
        name = pynvml.nvmlDeviceGetName(self.handle)
        self.name = name.decode() if isinstance(name, bytes) else name
        self._last_proc_util_ts = time.time()

    def sample(self, target_pid: int | None = None) -> dict:
        util = pynvml.nvmlDeviceGetUtilizationRates(self.handle)
        mem = pynvml.nvmlDeviceGetMemoryInfo(self.handle)
        temp = pynvml.nvmlDeviceGetTemperature(self.handle, pynvml.NVML_TEMPERATURE_GPU)
        clock = pynvml.nvmlDeviceGetClockInfo(self.handle, pynvml.NVML_CLOCK_SM)

        try:
            encoder_util, _ = pynvml.nvmlDeviceGetEncoderUtilization(self.handle)
        except pynvml.NVMLError:
            encoder_util = None
        try:
            decoder_util, _ = pynvml.nvmlDeviceGetDecoderUtilization(self.handle)
        except pynvml.NVMLError:
            decoder_util = None

        throttled, throttle_reasons = self._throttle_state()
        iracing_sm_util, iracing_enc_util = self._process_util(target_pid)
        iracing_vram_mb = self._process_vram(target_pid)

        try:
            power_draw_w = pynvml.nvmlDeviceGetPowerUsage(self.handle) / 1000.0
        except pynvml.NVMLError:
            power_draw_w = None

        return {
            "gpu_util_pct": util.gpu,
            "mem_util_pct": util.memory,
            "encoder_util_pct": encoder_util,
            "decoder_util_pct": decoder_util,
            "vram_used_mb": mem.used / (1024 * 1024),
            "vram_total_mb": mem.total / (1024 * 1024),
            "temp_c": temp,
            "clock_mhz": clock,
            "power_draw_w": power_draw_w,
            "throttled": throttled,
            "throttle_reasons": throttle_reasons,
            "iracing_sm_util_pct": iracing_sm_util,
            "iracing_enc_util_pct": iracing_enc_util,
            "iracing_vram_mb": iracing_vram_mb,
        }

    def _throttle_state(self) -> tuple[bool | None, list[str]]:
        try:
            mask = pynvml.nvmlDeviceGetCurrentClocksThrottleReasons(self.handle)
        except pynvml.NVMLError:
            return None, []
        active = [name for name, bit in _LIMITING_REASONS.items() if mask & bit]
        return bool(active), active

    def _process_util(self, target_pid: int | None) -> tuple[float | None, float | None]:
        if target_pid is None:
            return None, None
        try:
            # NVML only keeps a short rolling history of per-process
            # samples - looking back further than ~1s than our own poll
            # interval just re-returns the same recent sample.
            since_usec = int((self._last_proc_util_ts - 1.0) * 1e6)
            samples = pynvml.nvmlDeviceGetProcessUtilization(self.handle, since_usec)
        except pynvml.NVMLError:
            return None, None
        finally:
            self._last_proc_util_ts = time.time()

        matches = [s for s in samples if s.pid == target_pid]
        if not matches:
            return None, None
        latest = max(matches, key=lambda s: s.timeStamp)
        return float(latest.smUtil), float(latest.encUtil)

    def _process_vram(self, target_pid: int | None) -> float | None:
        """iRacing's own VRAM footprint, not just the system-wide total -
        a game process shows up under the "graphics" running-processes
        list, not "compute", so that's the one checked here."""
        if target_pid is None:
            return None
        try:
            procs = pynvml.nvmlDeviceGetGraphicsRunningProcesses(self.handle)
        except pynvml.NVMLError:
            return None
        for p in procs:
            if p.pid == target_pid and p.usedGpuMemory is not None:
                return p.usedGpuMemory / (1024 * 1024)
        return None

    def shutdown(self):
        try:
            pynvml.nvmlShutdown()
        except pynvml.NVMLError:
            pass
