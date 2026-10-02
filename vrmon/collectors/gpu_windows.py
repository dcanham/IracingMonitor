"""Vendor-neutral GPU stats from Windows itself - works on NVIDIA, AMD and
Intel alike, with no extra packages.

Utilization and VRAM come from the "GPU Engine" / "GPU Adapter Memory" /
"GPU Process Memory" performance counters (the same source Task Manager's
GPU tab reads), via the PDH API. The adapter's name and total VRAM come
from DXGI. Both are called through ctypes.

What Windows doesn't expose - temperature, clocks, power draw, throttle
reasons - is left as None here; vendor-specific backends fill those in.
"""

import ctypes
import logging
import re
from collections import defaultdict
from ctypes import wintypes

log = logging.getLogger(__name__)

# --- PDH (performance counters) ------------------------------------------

_pdh = ctypes.WinDLL("pdh.dll")

_PDH_FMT_DOUBLE = 0x00000200
_PDH_FMT_NOCAP100 = 0x00008000
_PDH_MORE_DATA = 0x800007D2
_PDH_CSTATUS_VALID_DATA = 0x0
_PDH_CSTATUS_NEW_DATA = 0x1


class _FmtValueUnion(ctypes.Union):
    _fields_ = [("longValue", ctypes.c_long), ("doubleValue", ctypes.c_double), ("largeValue", ctypes.c_longlong)]


class _PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("u", _FmtValueUnion)]


class _PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _PDH_FMT_COUNTERVALUE)]


for _name, _args in {
    "PdhOpenQueryW": [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
    "PdhAddEnglishCounterW": [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
    "PdhCollectQueryData": [ctypes.c_void_p],
    "PdhGetFormattedCounterArrayW": [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p],
    "PdhCloseQuery": [ctypes.c_void_p],
}.items():
    _fn = getattr(_pdh, _name)
    _fn.argtypes = _args
    _fn.restype = wintypes.DWORD


class _PdhQuery:
    """One PDH query holding wildcard counters, collected together.
    Wildcard instances are re-expanded on every collection, so processes
    that start after the query was opened (e.g. the sim) still show up."""

    def __init__(self, paths: list[str]):
        self._query = ctypes.c_void_p()
        status = _pdh.PdhOpenQueryW(None, None, ctypes.byref(self._query))
        if status:
            raise OSError(f"PdhOpenQuery failed: 0x{status:08X}")
        self._counters = {}
        for path in paths:
            counter = ctypes.c_void_p()
            status = _pdh.PdhAddEnglishCounterW(self._query, path, None, ctypes.byref(counter))
            if status:
                raise OSError(f"PdhAddEnglishCounter({path}) failed: 0x{status:08X}")
            self._counters[path] = counter
        _pdh.PdhCollectQueryData(self._query)  # rate counters need a first baseline sample

    def collect(self) -> dict[str, list[tuple[str, float]]]:
        """{counter path: [(instance name, value), ...]} - instances without
        valid data yet (e.g. a process that just appeared) are skipped."""
        _pdh.PdhCollectQueryData(self._query)
        return {path: self._read(counter) for path, counter in self._counters.items()}

    @staticmethod
    def _read(counter) -> list[tuple[str, float]]:
        fmt = _PDH_FMT_DOUBLE | _PDH_FMT_NOCAP100
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        status = _pdh.PdhGetFormattedCounterArrayW(counter, fmt, ctypes.byref(size), ctypes.byref(count), None)
        if status != _PDH_MORE_DATA:
            return []
        buf = (ctypes.c_byte * size.value)()
        status = _pdh.PdhGetFormattedCounterArrayW(counter, fmt, ctypes.byref(size), ctypes.byref(count), buf)
        if status:
            return []
        items = ctypes.cast(buf, ctypes.POINTER(_PDH_FMT_COUNTERVALUE_ITEM_W))
        out = []
        for i in range(count.value):
            item = items[i]
            if item.FmtValue.CStatus in (_PDH_CSTATUS_VALID_DATA, _PDH_CSTATUS_NEW_DATA):
                out.append((item.szName, item.FmtValue.u.doubleValue))
        return out

    def close(self) -> None:
        _pdh.PdhCloseQuery(self._query)


# --- DXGI (adapter name / total VRAM / LUID) -----------------------------

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", ctypes.c_wchar * 128),
        ("VendorId", wintypes.UINT),
        ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT),
        ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("LuidLowPart", wintypes.DWORD),
        ("LuidHighPart", wintypes.LONG),
        ("Flags", wintypes.UINT),
    ]


_IID_IDXGIFactory1 = _GUID(0x770AAE78, 0xF26F, 0x4DBA, (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))
_DXGI_ADAPTER_FLAG_SOFTWARE = 2
_DXGI_ERROR_NOT_FOUND = 0x887A0002
# COM vtable slots (IUnknown 0-2, IDXGIObject 3-6, then the interface's own).
_RELEASE = 2
_FACTORY1_ENUM_ADAPTERS1 = 12
_ADAPTER1_GET_DESC1 = 10

VENDOR_NAMES = {0x10DE: "nvidia", 0x1002: "amd", 0x1022: "amd", 0x8086: "intel"}


def _com_call(obj, slot, restype, argtypes, *args):
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[slot])
    return fn(obj, *args)


def list_adapters() -> list[dict]:
    """Hardware (non-software) adapters: name, vendor, total dedicated VRAM,
    and the LUID string the GPU performance counters use for it."""
    factory = ctypes.c_void_p()
    hr = ctypes.windll.dxgi.CreateDXGIFactory1(ctypes.byref(_IID_IDXGIFactory1), ctypes.byref(factory))
    if hr != 0:
        raise OSError(f"CreateDXGIFactory1 failed: 0x{hr & 0xFFFFFFFF:08X}")
    adapters = []
    try:
        index = 0
        while True:
            adapter = ctypes.c_void_p()
            hr = _com_call(factory, _FACTORY1_ENUM_ADAPTERS1, ctypes.c_long,
                           [wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)], index, ctypes.byref(adapter))
            if (hr & 0xFFFFFFFF) == _DXGI_ERROR_NOT_FOUND:
                break
            index += 1
            if hr != 0:
                continue
            try:
                desc = _DXGI_ADAPTER_DESC1()
                if _com_call(adapter, _ADAPTER1_GET_DESC1, ctypes.c_long,
                             [ctypes.POINTER(_DXGI_ADAPTER_DESC1)], ctypes.byref(desc)) != 0:
                    continue
                if desc.Flags & _DXGI_ADAPTER_FLAG_SOFTWARE:
                    continue
                adapters.append({
                    "name": desc.Description,
                    "vendor": VENDOR_NAMES.get(desc.VendorId, f"0x{desc.VendorId:04X}"),
                    "vram_total_mb": desc.DedicatedVideoMemory / (1024 * 1024),
                    "luid": f"luid_0x{desc.LuidHighPart & 0xFFFFFFFF:08x}_0x{desc.LuidLowPart:08x}",
                })
            finally:
                _com_call(adapter, _RELEASE, wintypes.ULONG, [])
    finally:
        _com_call(factory, _RELEASE, wintypes.ULONG, [])
    return adapters


def pick_main_adapter(adapters: list[dict]) -> dict | None:
    """The discrete GPU the sim almost certainly renders on: the one with
    the most dedicated VRAM (an integrated GPU has little or none)."""
    return max(adapters, key=lambda a: a["vram_total_mb"], default=None)


# --- the collector ----------------------------------------------------------

_ENGINE_RE = re.compile(r"^pid_(\d+)_(luid_0x[0-9a-f]+_0x[0-9a-f]+)_phys_\d+_eng_(\d+)_engtype_(.*)$", re.I)
_PROC_MEM_RE = re.compile(r"^pid_(\d+)_(luid_0x[0-9a-f]+_0x[0-9a-f]+)_phys_\d+$", re.I)
_ADAPTER_MEM_RE = re.compile(r"^(luid_0x[0-9a-f]+_0x[0-9a-f]+)_phys_\d+$", re.I)

_ENGINE_UTIL = r"\GPU Engine(*)\Utilization Percentage"
_ADAPTER_MEM = r"\GPU Adapter Memory(*)\Dedicated Usage"
_PROCESS_MEM = r"\GPU Process Memory(*)\Dedicated Usage"


class WindowsGpuCollector:
    backend = "windows"

    def __init__(self):
        adapter = pick_main_adapter(list_adapters())
        if adapter is None:
            raise RuntimeError("no hardware GPU adapter found")
        self.adapter = adapter
        self.name = adapter["name"]
        self.vendor = adapter["vendor"]
        self._luid = adapter["luid"].lower()
        self._query = _PdhQuery([_ENGINE_UTIL, _ADAPTER_MEM, _PROCESS_MEM])

    def sample(self, target_pid: int | None = None) -> dict:
        data = self._query.collect()

        # Task Manager's model: each engine's load is the sum over every
        # process using it; the GPU's overall load is its busiest engine.
        engine_total = defaultdict(float)       # (engine index, type) -> %
        target_engine = defaultdict(float)      # same, iRacing's process only
        for name, value in data[_ENGINE_UTIL]:
            m = _ENGINE_RE.match(name)
            if not m or m.group(2).lower() != self._luid:
                continue
            key = (m.group(3), m.group(4))
            engine_total[key] += value
            if target_pid is not None and int(m.group(1)) == target_pid:
                target_engine[key] += value

        def busiest(totals, engtype=None):
            values = [v for (_, kind), v in totals.items() if engtype is None or kind.lower() == engtype]
            return min(max(values), 100.0) if values else None

        vram_used_mb = None
        for name, value in data[_ADAPTER_MEM]:
            m = _ADAPTER_MEM_RE.match(name)
            if m and m.group(1).lower() == self._luid:
                vram_used_mb = (vram_used_mb or 0.0) + value / (1024 * 1024)

        iracing_vram_mb = None
        if target_pid is not None:
            for name, value in data[_PROCESS_MEM]:
                m = _PROC_MEM_RE.match(name)
                if m and int(m.group(1)) == target_pid and m.group(2).lower() == self._luid:
                    iracing_vram_mb = (iracing_vram_mb or 0.0) + value / (1024 * 1024)

        iracing_util = busiest(target_engine) if target_engine else (0.0 if target_pid is not None else None)
        return {
            "gpu_util_pct": busiest(engine_total) or 0.0,
            "mem_util_pct": None,
            "encoder_util_pct": busiest(engine_total, "videoencode"),
            "decoder_util_pct": busiest(engine_total, "videodecode"),
            "vram_used_mb": vram_used_mb,
            "vram_total_mb": self.adapter["vram_total_mb"],
            "temp_c": None,
            "clock_mhz": None,
            "power_draw_w": None,
            "throttled": None,
            "throttle_reasons": [],
            "iracing_sm_util_pct": iracing_util,
            "iracing_enc_util_pct": busiest(target_engine, "videoencode") if target_engine else None,
            "iracing_vram_mb": iracing_vram_mb,
        }

    def shutdown(self) -> None:
        self._query.close()
