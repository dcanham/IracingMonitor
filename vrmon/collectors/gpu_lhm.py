"""GPU temperature, power draw and core clock for AMD and Intel GPUs (and
NVIDIA, though NVML already covers those) via LibreHardwareMonitor - the
open-source sensor library behind tools like FanControl - which reads
each vendor's own interface (AMD's ADL, Intel's IGCL, NVIDIA's NVAPI).

setup downloads its .NET Framework build into tools/lhm; it's loaded
through pythonnet. Reading the sensors takes ~60ms, too slow for the GPU
loop's 4x/s, so a background thread refreshes them once a second and
sample() returns the latest values.

Not available here: throttle reasons (no vendor-neutral source), so the
"GPU power/thermal limited" report rule only works on NVIDIA.
"""

import logging
import threading

from vrmon import config

log = logging.getLogger(__name__)

_REFRESH_S = 1.0
_GPU_TYPES = ("GpuAmd", "GpuIntel", "GpuNvidia")


def _pick(sensors, sensor_type: str, preferred: tuple[str, ...]):
    """Value of the first sensor of this type whose name is in preferred
    (in order), else the first of that type at all."""
    candidates = [s for s in sensors if str(s.SensorType) == sensor_type and s.Value is not None]
    for name in preferred:
        for s in candidates:
            if s.Name == name:
                return float(s.Value)
    return float(candidates[0].Value) if candidates else None


class LhmGpuCollector:
    backend = "lhm"

    def __init__(self, prefer_name: str | None = None):
        lib = config.LHM_DIR / "LibreHardwareMonitorLib.dll"
        if not lib.exists():
            raise FileNotFoundError(f"LibreHardwareMonitor not installed ({lib}) - run setup.bat")
        import sys
        import pythonnet
        if str(config.LHM_DIR) not in sys.path:
            sys.path.append(str(config.LHM_DIR))
        try:
            pythonnet.load("netfx")
        except RuntimeError:
            pass  # already loaded in this process
        import clr
        clr.AddReference(str(lib))
        from LibreHardwareMonitor.Hardware import Computer

        self._computer = Computer()
        self._computer.IsGpuEnabled = True
        self._computer.Open()
        gpus = [h for h in self._computer.Hardware if str(h.HardwareType) in _GPU_TYPES]
        if not gpus:
            self._computer.Close()
            raise RuntimeError("LibreHardwareMonitor found no GPU")
        # The same GPU the other sources picked (by name), else the first
        # discrete one LHM lists.
        self._gpu = next((g for g in gpus if prefer_name and g.Name == prefer_name), gpus[0])
        self.name = str(self._gpu.Name)

        self._lock = threading.Lock()
        self._latest = {"temp_c": None, "power_draw_w": None, "clock_mhz": None}
        self._stop = threading.Event()
        self._refresh()
        self._thread = threading.Thread(target=self._run, name="lhm-gpu", daemon=True)
        self._thread.start()

    def _refresh(self) -> None:
        self._gpu.Update()
        sensors = list(self._gpu.Sensors)
        values = {
            "temp_c": _pick(sensors, "Temperature", ("GPU Core", "GPU Hot Spot")),
            # "GPU Package" is total board/package power where the vendor
            # reports it; AMD also exposes "GPU Core"/"GPU SoC" parts.
            "power_draw_w": _pick(sensors, "Power", ("GPU Package", "GPU Core")),
            "clock_mhz": _pick(sensors, "Clock", ("GPU Core",)),
        }
        with self._lock:
            self._latest = values

    def _run(self) -> None:
        while not self._stop.wait(_REFRESH_S):
            try:
                self._refresh()
            except Exception:
                log.debug("LibreHardwareMonitor refresh failed", exc_info=True)

    def sample(self, target_pid: int | None = None) -> dict:
        with self._lock:
            return dict(self._latest)

    def shutdown(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        try:
            self._computer.Close()
        except Exception:
            pass
