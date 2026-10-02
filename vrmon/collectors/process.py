"""Tracks the iRacing sim process specifically: is it running, and its
overall CPU%/RSS.

cpu_pct is psutil's per-process convention: % of ONE core, so it goes
past 100 on a multi-core CPU (600 = six cores' worth). It's stored that
way for continuity with older sessions; the dashboard divides by the
logical CPU count to show a share of the whole CPU.

This is deliberately NOT per-core - attributing a
specific thread to a specific P-core/E-core at a given instant needs ETW
tracing, which is out of scope for v1 (see plan notes). Correlate this
collector's cpu_pct against system.py's system-wide cpu_p_pct/cpu_e_pct
split to spot "E-core group busy while P-cores had headroom" windows.
"""

import psutil

from vrmon.config import IRACING_PROCESS_NAMES


class ProcessCollector:
    def __init__(self):
        self._proc: psutil.Process | None = None

    def _find_process(self) -> psutil.Process | None:
        if self._proc is not None:
            try:
                if self._proc.is_running() and self._proc.name() in IRACING_PROCESS_NAMES:
                    return self._proc
            except psutil.NoSuchProcess:
                pass
            self._proc = None

        for proc in psutil.process_iter(["pid", "name"]):
            if proc.info["name"] in IRACING_PROCESS_NAMES:
                self._proc = proc
                # Prime cpu_percent - first call always returns 0.0.
                try:
                    self._proc.cpu_percent()
                except psutil.NoSuchProcess:
                    self._proc = None
                return self._proc
        return None

    def is_running(self) -> bool:
        return self._find_process() is not None

    def sample(self) -> dict | None:
        proc = self._find_process()
        if proc is None:
            return None
        try:
            cpu_pct = proc.cpu_percent()
            rss_mb = proc.memory_info().rss / (1024 * 1024)
            pid = proc.pid
        except psutil.NoSuchProcess:
            self._proc = None
            return None

        return {"pid": pid, "cpu_pct": cpu_pct, "rss_mb": rss_mb}
