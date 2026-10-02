"""Classify logical CPUs into P-core / E-core groups on hybrid Intel CPUs
(e.g. the 12700K) using the documented GetLogicalProcessorInformationEx
Win32 API (EfficiencyClass field). This does NOT tell us which core a
specific thread is running on at a given instant (that needs ETW) - it
only tells us, once, which logical CPU indices belong to "performance"
cores vs "efficiency" cores, so we can later track per-group utilization
over time via psutil.cpu_percent(percpu=True).
"""

import ctypes
from ctypes import wintypes
import logging

log = logging.getLogger(__name__)

_RELATION_PROCESSOR_CORE = 0
_HEADER_SIZE = 8  # Relationship (DWORD) + Size (DWORD)
_GROUP_AFFINITY_SIZE = 16  # Mask (8) + Group (2) + Reserved[3] (6)


def _kernel32():
    return ctypes.windll.kernel32


def _raw_processor_core_records():
    k32 = _kernel32()
    buf_len = wintypes.DWORD(0)
    k32.GetLogicalProcessorInformationEx(_RELATION_PROCESSOR_CORE, None, ctypes.byref(buf_len))
    if buf_len.value == 0:
        return b""
    buf = ctypes.create_string_buffer(buf_len.value)
    ok = k32.GetLogicalProcessorInformationEx(_RELATION_PROCESSOR_CORE, buf, ctypes.byref(buf_len))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return buf.raw[: buf_len.value]


def get_core_efficiency_map() -> dict[int, int]:
    """Return {logical_cpu_index: efficiency_class}.

    Higher EfficiencyClass means a higher-performance core (P-core on a
    hybrid part). On a non-hybrid CPU every entry will share the same
    class. Returns {} if the query fails for any reason - callers should
    treat that as "topology unknown, don't split by core group".
    """
    try:
        data = _raw_processor_core_records()
    except OSError:
        log.warning("GetLogicalProcessorInformationEx failed", exc_info=True)
        return {}

    result: dict[int, int] = {}
    offset = 0
    while offset + _HEADER_SIZE <= len(data):
        relationship = int.from_bytes(data[offset : offset + 4], "little")
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        if size == 0:
            break
        record = data[offset : offset + size]

        if relationship == _RELATION_PROCESSOR_CORE and len(record) >= 32:
            efficiency_class = record[9]
            group_count = int.from_bytes(record[30:32], "little")
            base = 32
            for i in range(group_count):
                start = base + i * _GROUP_AFFINITY_SIZE
                end = start + _GROUP_AFFINITY_SIZE
                if end > len(record):
                    break
                mask = int.from_bytes(record[start : start + 8], "little")
                bit = 0
                while mask:
                    if mask & 1:
                        result[bit] = efficiency_class
                    mask >>= 1
                    bit += 1

        offset += size

    return result


def build_core_groups() -> dict[str, list[int]]:
    """Return {"P": [...], "E": [...]} logical CPU index lists.

    On a CPU that isn't hybrid (or if the query fails), everything lands
    in a single "P" group so downstream code doesn't need a special case.
    """
    eff_map = get_core_efficiency_map()
    if not eff_map:
        import os

        return {"P": list(range(os.cpu_count() or 0)), "E": []}

    classes = sorted(set(eff_map.values()))
    if len(classes) < 2:
        return {"P": sorted(eff_map.keys()), "E": []}

    # Highest EfficiencyClass value = performance cores.
    high = classes[-1]
    p_cores = sorted(cpu for cpu, cls in eff_map.items() if cls == high)
    e_cores = sorted(cpu for cpu, cls in eff_map.items() if cls != high)
    return {"P": p_cores, "E": e_cores}


if __name__ == "__main__":
    groups = build_core_groups()
    print(f"P-cores (logical CPUs): {groups['P']}")
    print(f"E-cores (logical CPUs): {groups['E']}")
