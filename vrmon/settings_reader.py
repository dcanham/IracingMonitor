"""On-demand snapshots of iRacing's graphics settings (the flat-monitor
renderer config, rendererDX11Monitor.ini).

These are read once (at session start, or on demand for the dashboard),
not polled continuously - they're config files, not telemetry.
"""

import configparser
import logging

from vrmon import config

log = logging.getLogger(__name__)


def read_iracing_renderer_settings(path=None) -> dict:
    """Flat {"Section.Key": value} dict from an iRacing renderer ini.
    Returns {} if the file doesn't exist yet (the sim has never been run)."""
    path = path or config.IRACING_RENDERER_INI
    if not path.exists():
        return {}

    parser = configparser.ConfigParser(inline_comment_prefixes=(";",), strict=False)
    parser.optionxform = str  # preserve key case (e.g. "MSAASamples", not "msaasamples")
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error:
        log.warning("failed to parse %s", path, exc_info=True)
        return {}

    flat = {}
    for section in parser.sections():
        for key, value in parser.items(section):
            flat[f"{section}.{key}"] = value.strip()
    return flat


def read_all_settings() -> dict:
    return {"iracing_renderer": read_iracing_renderer_settings()}


def flatten_session_settings(settings: dict) -> dict:
    """Namespaces a session snapshot's settings into one flat dict so they
    can be diffed or tabulated together. (Sessions recorded back when VR
    was supported also stored Virtual Desktop settings - those are left
    out here.)"""
    return {f"iRacing.{key}": value for key, value in settings.get("iracing_renderer", {}).items()}


def diff_settings(old: dict, new: dict) -> list[tuple[str, object, object]]:
    """Returns [(key, old_value, new_value), ...] for every key that
    differs (added, removed, or changed) between two flat dicts."""
    keys = sorted(set(old) | set(new))
    changes = []
    for key in keys:
        ov, nv = old.get(key), new.get(key)
        if ov != nv:
            changes.append((key, ov, nv))
    return changes
