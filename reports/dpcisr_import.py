"""Parses an xperf `-a dpcisr -summary` text report and attaches a
per-CPU interrupt-load summary to a session.

Why: our own telemetry samples per-core CPU % at 4-5Hz, which is far too
coarse to see a burst of USB interrupt handling (a curb-strike FFB update
storm, for example) that stalls a single core for microseconds to low
milliseconds - exactly the kind of thing that can stall a render thread
without ever showing up as "high CPU usage" in an averaged sample. This
is the same class of data LatencyMon shows (which core is absorbing
DPC/ISR time, and which driver is responsible), captured instead via
Windows' own built-in Windows Performance Toolkit (xperf) so it can run
unattended - see presentmon_auto_capture.py, which drives both captures
together.

Usage:
    python -m reports.dpcisr_import <session_id> <report.txt>
"""

import json
import re
import sys
import time

from vrmon import store

_SECTION_RE = re.compile(r"^--------------------------\n(DPC Info|Interrupt Info)\n", re.MULTILINE)
_CPU_HEADER_RE = re.compile(r"CPU (\d+) Usage")

# The USB/HID driver framework - what the Reddit-documented fix targets.
_WDF_MODULE = "Wdf01000.sys"


def _parse_module_table(block: str) -> dict[int, dict[str, float]]:
    """One "CPU Usage Summing By Module" table -> {cpu_index: {module: usec}}."""
    lines = block.splitlines()
    header_idx = next((i for i, ln in enumerate(lines) if "CPU 0 Usage" in ln), None)
    if header_idx is None:
        return {}
    n_cpus = len(_CPU_HEADER_RE.findall(lines[header_idx]))

    result: dict[int, dict[str, float]] = {cpu: {} for cpu in range(n_cpus)}
    # Data rows start 2 lines after the "CPU N Usage" header (units line in between).
    for line in lines[header_idx + 2:]:
        if not line.strip():
            break
        parts = line.split(",")
        if len(parts) < n_cpus + 1:
            continue
        module = parts[-1].strip()
        if not module:
            continue
        for cpu in range(n_cpus):
            fields = parts[cpu].split()
            if not fields:
                continue
            try:
                usec = float(fields[0])
            except ValueError:
                continue
            if usec:
                result[cpu][module] = result[cpu].get(module, 0.0) + usec
    return result


def parse_report(text: str) -> dict[int, dict[str, float]]:
    """Combines the DPC and Interrupt module tables into one
    {cpu_index: {module: usec}} - added together, since both represent
    time that CPU couldn't spend on anything else."""
    sections = {}
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        name = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[name] = text[start:end]

    combined: dict[int, dict[str, float]] = {}
    for section_text in sections.values():
        table = _parse_module_table(section_text)
        for cpu, modules in table.items():
            bucket = combined.setdefault(cpu, {})
            for module, usec in modules.items():
                bucket[module] = bucket.get(module, 0.0) + usec
    return combined


def summarize(per_cpu: dict[int, dict[str, float]]) -> dict:
    cpu_totals = {cpu: sum(modules.values()) for cpu, modules in per_cpu.items()}
    grand_total = sum(cpu_totals.values())

    dominant_cpu = max(cpu_totals, key=cpu_totals.get) if cpu_totals else None
    dominant_pct = (cpu_totals[dominant_cpu] / grand_total * 100.0) if dominant_cpu is not None and grand_total else None
    dominant_top_module = None
    if dominant_cpu is not None and per_cpu.get(dominant_cpu):
        dominant_top_module = max(per_cpu[dominant_cpu], key=per_cpu[dominant_cpu].get)

    wdf_per_cpu = {cpu: modules.get(_WDF_MODULE, 0.0) for cpu, modules in per_cpu.items()}
    wdf_total = sum(wdf_per_cpu.values())
    wdf_dominant_cpu = max(wdf_per_cpu, key=wdf_per_cpu.get) if wdf_total else None
    wdf_dominant_usec = wdf_per_cpu.get(wdf_dominant_cpu) if wdf_dominant_cpu is not None else None

    return {
        "cpu_totals_usec": cpu_totals,
        "dominant_cpu": dominant_cpu,
        "dominant_cpu_pct_of_total": dominant_pct,
        "dominant_cpu_top_module": dominant_top_module,
        "wdf_total_usec": wdf_total,
        "wdf_dominant_cpu": wdf_dominant_cpu,
        "wdf_dominant_cpu_usec": wdf_dominant_usec,
    }


def import_report(session_id: int, report_path: str) -> dict:
    session = store.get_session(session_id)
    if session is None:
        raise SystemExit(f"no such session: {session_id}")

    with open(report_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    per_cpu = parse_report(text)
    if not per_cpu:
        raise SystemExit(f"could not find any CPU usage tables in {report_path} - unexpected xperf report format")

    summary = summarize(per_cpu)

    store.insert_dpcisr_summary(
        session_id=session_id,
        imported_ts=time.time(),
        report_path=report_path,
        dominant_cpu=summary["dominant_cpu"],
        dominant_cpu_pct_of_total=summary["dominant_cpu_pct_of_total"],
        dominant_cpu_top_module=summary["dominant_cpu_top_module"],
        wdf_total_usec=summary["wdf_total_usec"],
        wdf_dominant_cpu=summary["wdf_dominant_cpu"],
        wdf_dominant_cpu_usec=summary["wdf_dominant_cpu_usec"],
        per_cpu_json=json.dumps(summary["cpu_totals_usec"]),
    )
    return summary


def main():
    if len(sys.argv) < 3:
        raise SystemExit("usage: python -m reports.dpcisr_import <session_id> <report.txt>")
    session_id = int(sys.argv[1])
    report_path = sys.argv[2]
    summary = import_report(session_id, report_path)
    print(f"session {session_id}: dominant CPU is core {summary['dominant_cpu']} "
          f"({summary['dominant_cpu_pct_of_total']:.1f}% of all DPC/ISR time, "
          f"top driver: {summary['dominant_cpu_top_module']})")
    if summary["wdf_total_usec"]:
        print(f"  Wdf01000.sys (USB/HID): {summary['wdf_total_usec']:.0f}us total, "
              f"heaviest on core {summary['wdf_dominant_cpu']} ({summary['wdf_dominant_cpu_usec']:.0f}us)")


if __name__ == "__main__":
    main()
