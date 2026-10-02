"""System-wide CPU (busiest core, plus a P/E core group split on hybrid
Intel CPUs) + RAM + disk + network collector."""

import time
from types import SimpleNamespace

import psutil

from vrmon.config import NETWORK_ADAPTER_EXCLUDE_SUBSTRINGS, NETWORK_ADAPTER_NAME
from vrmon.cpu_topology import build_core_groups


def _is_physical_adapter(name: str) -> bool:
    lowered = name.lower()
    return not any(sub in lowered for sub in NETWORK_ADAPTER_EXCLUDE_SUBSTRINGS)


class SystemCollector:
    def __init__(self):
        self.core_groups = build_core_groups()
        # Prime psutil's internal counters - the first call after this
        # always returns 0.0 for everything.
        psutil.cpu_percent(percpu=True)

        self._last_disk = psutil.disk_io_counters()
        self._last_net = self._read_net_counters()
        self._last_ts = time.time()

    def _read_net_counters(self):
        counters = psutil.net_io_counters(pernic=True)
        if NETWORK_ADAPTER_NAME != "auto":
            return counters.get(NETWORK_ADAPTER_NAME)
        # Sum every connected physical adapter rather than guessing which
        # one carries the stream - whichever it is, its traffic is in here.
        stats = psutil.net_if_stats()
        picked = [c for name, c in counters.items()
                  if _is_physical_adapter(name) and name in stats and stats[name].isup]
        if not picked:
            return None
        return SimpleNamespace(
            bytes_sent=sum(c.bytes_sent for c in picked),
            bytes_recv=sum(c.bytes_recv for c in picked),
        )

    def sample(self) -> dict:
        now = time.time()
        dt = max(now - self._last_ts, 1e-6)

        per_cpu = psutil.cpu_percent(percpu=True)
        e_idx = self.core_groups.get("E", [])
        # The P/E split only means something on a hybrid CPU - on anything
        # else "P" is just every core, i.e. a duplicate of the total.
        cpu_p_pct = _avg(per_cpu, self.core_groups.get("P", [])) if e_idx else None
        cpu_e_pct = _avg(per_cpu, e_idx) if e_idx else None
        cpu_total_pct = sum(per_cpu) / len(per_cpu) if per_cpu else None
        # A single saturated thread (e.g. the sim's main thread) is the
        # classic CPU bottleneck, and it barely moves the all-core average.
        cpu_max_core_pct = max(per_cpu) if per_cpu else None

        vm = psutil.virtual_memory()
        # On Windows, psutil's "swap" is the page file - there's no
        # separate swap partition/file the way Linux has one.
        pf = psutil.swap_memory()

        disk = psutil.disk_io_counters()
        disk_read_bps = disk_write_bps = None
        if disk and self._last_disk:
            disk_read_bps = (disk.read_bytes - self._last_disk.read_bytes) / dt
            disk_write_bps = (disk.write_bytes - self._last_disk.write_bytes) / dt

        net = self._read_net_counters()
        net_sent_bps = net_recv_bps = None
        if net and self._last_net:
            net_sent_bps = (net.bytes_sent - self._last_net.bytes_sent) / dt
            net_recv_bps = (net.bytes_recv - self._last_net.bytes_recv) / dt

        self._last_disk = disk
        self._last_net = net
        self._last_ts = now

        return {
            "ts": now,
            "cpu_total_pct": cpu_total_pct,
            "cpu_p_pct": cpu_p_pct,
            "cpu_e_pct": cpu_e_pct,
            "cpu_max_core_pct": cpu_max_core_pct,
            "ram_used_mb": vm.used / (1024 * 1024),
            "ram_total_mb": vm.total / (1024 * 1024),
            "pagefile_used_mb": pf.used / (1024 * 1024),
            "pagefile_total_mb": pf.total / (1024 * 1024),
            "disk_read_bps": disk_read_bps,
            "disk_write_bps": disk_write_bps,
            "net_sent_bps": net_sent_bps,
            "net_recv_bps": net_recv_bps,
        }


def _avg(values: list[float], indexes: list[int]):
    picked = [values[i] for i in indexes if i < len(values)]
    return sum(picked) / len(picked) if picked else None
