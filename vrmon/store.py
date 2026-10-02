"""SQLite storage for all collected samples.

Each collector writes to its own table on its own timeline (all sharing a
`ts` unix-epoch-seconds column), rather than forcing everything into one
synchronized row. The dashboard/report layer joins by nearest timestamp
when it needs to correlate across tables.
"""

import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from vrmon.config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts REAL NOT NULL,
    end_ts REAL
);

CREATE TABLE IF NOT EXISTS system_samples (
    ts REAL NOT NULL,
    cpu_total_pct REAL,
    cpu_p_pct REAL,
    cpu_e_pct REAL,
    ram_used_mb REAL,
    ram_total_mb REAL,
    disk_read_bps REAL,
    disk_write_bps REAL,
    net_sent_bps REAL,
    net_recv_bps REAL
);
CREATE INDEX IF NOT EXISTS idx_system_ts ON system_samples(ts);

CREATE TABLE IF NOT EXISTS gpu_samples (
    ts REAL NOT NULL,
    gpu_util_pct REAL,
    encoder_util_pct REAL,
    decoder_util_pct REAL,
    vram_used_mb REAL,
    vram_total_mb REAL,
    temp_c REAL,
    clock_mhz REAL,
    mem_util_pct REAL,
    throttled INTEGER,
    throttle_reasons TEXT,
    iracing_sm_util_pct REAL,
    iracing_enc_util_pct REAL,
    power_draw_w REAL,
    iracing_vram_mb REAL
);
CREATE INDEX IF NOT EXISTS idx_gpu_ts ON gpu_samples(ts);

CREATE TABLE IF NOT EXISTS process_samples (
    ts REAL NOT NULL,
    pid INTEGER,
    cpu_pct REAL,
    rss_mb REAL
);
CREATE INDEX IF NOT EXISTS idx_process_ts ON process_samples(ts);

CREATE TABLE IF NOT EXISTS top_process_samples (
    ts REAL NOT NULL,
    pid INTEGER,
    name TEXT,
    cpu_pct REAL,
    rss_mb REAL
);
CREATE INDEX IF NOT EXISTS idx_top_process_ts ON top_process_samples(ts);

CREATE TABLE IF NOT EXISTS iracing_roster_events (
    ts REAL NOT NULL,
    event_type TEXT,
    car_idx INTEGER,
    driver_name TEXT,
    num_drivers INTEGER
);
CREATE INDEX IF NOT EXISTS idx_roster_ts ON iracing_roster_events(ts);

CREATE TABLE IF NOT EXISTS windows_events (
    ts REAL NOT NULL,
    provider TEXT,
    event_id TEXT,
    level TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_windows_events_ts ON windows_events(ts);

CREATE TABLE IF NOT EXISTS iracing_samples (
    ts REAL NOT NULL,
    connected INTEGER,
    frame_rate REAL,
    session_time REAL,
    speed REAL,
    lap INTEGER,
    lap_dist_pct REAL,
    on_track INTEGER
);
CREATE INDEX IF NOT EXISTS idx_iracing_ts ON iracing_samples(ts);

CREATE TABLE IF NOT EXISTS session_settings (
    session_id INTEGER PRIMARY KEY,
    captured_ts REAL NOT NULL,
    iracing_renderer_json TEXT,
    track_name TEXT,
    track_config TEXT,
    car_name TEXT
);

CREATE TABLE IF NOT EXISTS session_presentmon (
    session_id INTEGER PRIMARY KEY,
    imported_ts REAL NOT NULL,
    csv_path TEXT,
    frame_count INTEGER,
    dropped_count INTEGER,
    p50_ms REAL,
    p90_ms REAL,
    p95_ms REAL,
    p99_ms REAL,
    max_ms REAL,
    pct_over_694 REAL,
    pct_over_833 REAL,
    per_second_json TEXT
);

CREATE TABLE IF NOT EXISTS session_dpcisr (
    session_id INTEGER PRIMARY KEY,
    imported_ts REAL NOT NULL,
    report_path TEXT,
    dominant_cpu INTEGER,
    dominant_cpu_pct_of_total REAL,
    dominant_cpu_top_module TEXT,
    wdf_total_usec REAL,
    wdf_dominant_cpu INTEGER,
    wdf_dominant_cpu_usec REAL,
    per_cpu_json TEXT
);
"""

_local = threading.local()
_write_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        _local.conn = _connect()
    return _local.conn


# Columns added after a table already existed in the wild - CREATE TABLE
# IF NOT EXISTS silently no-ops on an existing table, so new columns need
# an explicit ALTER TABLE migration instead.
_COLUMN_MIGRATIONS = {
    "gpu_samples": {
        "mem_util_pct": "REAL",
        "throttled": "INTEGER",
        "throttle_reasons": "TEXT",
        "iracing_sm_util_pct": "REAL",
        "iracing_enc_util_pct": "REAL",
        "power_draw_w": "REAL",
        "iracing_vram_mb": "REAL",
    },
    "system_samples": {
        "pagefile_used_mb": "REAL",
        "pagefile_total_mb": "REAL",
        "cpu_max_core_pct": "REAL",
    },
    "session_settings": {
        "track_name": "TEXT",
        "track_config": "TEXT",
        "car_name": "TEXT",
    },
    "iracing_samples": {
        "lap_dist_pct": "REAL",
    },
    "windows_events": {
        # One per real event: "<log>|<record id>", or "WER|<report id>" for
        # Windows Error Reporting, which re-logs the same crash report on
        # every upload retry. NULL on rows recorded before this existed.
        "record_key": "TEXT",
    },
}


def _migrate_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, col_type in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


def init_db() -> None:
    conn = get_conn()
    with _write_lock:
        conn.executescript(_SCHEMA)
        _migrate_columns(conn)
        # Needs record_key, which only exists once the migration has run.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_windows_events_key ON windows_events(record_key)")
        conn.commit()


@contextmanager
def _write():
    conn = get_conn()
    with _write_lock:
        yield conn
        conn.commit()


# --- session lifecycle ----------------------------------------------------


def start_session(ts: float | None = None) -> int:
    ts = ts if ts is not None else time.time()
    with _write() as conn:
        cur = conn.execute("INSERT INTO sessions (start_ts) VALUES (?)", (ts,))
        return cur.lastrowid


def end_session(session_id: int, ts: float | None = None) -> None:
    ts = ts if ts is not None else time.time()
    with _write() as conn:
        conn.execute("UPDATE sessions SET end_ts = ? WHERE id = ?", (ts, session_id))


def close_orphaned_sessions() -> list[int]:
    """Ends any session still open from a previous run - which only happens
    when vrmon didn't get to shut down cleanly (e.g. the PC hard-crashed
    mid-session) - at its last recorded sample, or its start if it never
    got one. Returns the ids closed."""
    conn = get_conn()
    closed = []
    with _write_lock:
        for sid, start_ts in conn.execute("SELECT id, start_ts FROM sessions WHERE end_ts IS NULL").fetchall():
            last = conn.execute("SELECT MAX(ts) FROM system_samples WHERE ts >= ?", (start_ts,)).fetchone()[0]
            conn.execute("UPDATE sessions SET end_ts = ? WHERE id = ?", (last or start_ts, sid))
            closed.append(sid)
        conn.commit()
    return closed


def last_activity_ts() -> float | None:
    """When vrmon last recorded anything - the point to look back to for
    Windows events it may have missed while it wasn't running."""
    conn = get_conn()
    candidates = [
        conn.execute("SELECT MAX(ts) FROM system_samples").fetchone()[0],
        conn.execute("SELECT MAX(COALESCE(end_ts, start_ts)) FROM sessions").fetchone()[0],
        conn.execute("SELECT MAX(ts) FROM windows_events").fetchone()[0],
    ]
    candidates = [c for c in candidates if c is not None]
    return max(candidates) if candidates else None


def latest_open_session() -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT id, start_ts, end_ts FROM sessions WHERE end_ts IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return {"id": row[0], "start_ts": row[1], "end_ts": row[2]}


def list_sessions() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT id, start_ts, end_ts FROM sessions ORDER BY id DESC").fetchall()
    return [{"id": r[0], "start_ts": r[1], "end_ts": r[2]} for r in rows]


def get_session(session_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT id, start_ts, end_ts FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if not row:
        return None
    return {"id": row[0], "start_ts": row[1], "end_ts": row[2]}


# --- per-session settings snapshot ------------------------------------------


def insert_session_settings(
    session_id: int,
    captured_ts: float,
    iracing_renderer: dict,
    track_name: str | None = None,
    track_config: str | None = None,
    car_name: str | None = None,
) -> None:
    with _write() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO session_settings
               (session_id, captured_ts, iracing_renderer_json, track_name, track_config, car_name)
               VALUES (?,?,?,?,?,?)""",
            (
                session_id,
                captured_ts,
                json.dumps(iracing_renderer),
                track_name,
                track_config,
                car_name,
            ),
        )


def update_session_track_car(session_id: int, track_name: str | None, track_config: str | None, car_name: str | None) -> None:
    """Fills in track/car after the fact, once the SDK actually reports
    it - separate from insert_session_settings because that runs early
    (when recording starts) and track/car can genuinely take much longer
    than the rest of the settings snapshot to become available."""
    with _write() as conn:
        conn.execute(
            "UPDATE session_settings SET track_name = ?, track_config = ?, car_name = ? WHERE session_id = ?",
            (track_name, track_config, car_name, session_id),
        )


def get_session_settings(session_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        """SELECT captured_ts, iracing_renderer_json, track_name, track_config, car_name
           FROM session_settings WHERE session_id = ?""",
        (session_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "captured_ts": row[0],
        "iracing_renderer": json.loads(row[1]) if row[1] else {},
        "track_name": row[2],
        "track_config": row[3],
        "car_name": row[4],
    }


def previous_session_id(session_id: int) -> int | None:
    """The session immediately before this one, chronologically - used to
    diff settings against "the last time you recorded"."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM sessions WHERE id < ? ORDER BY id DESC LIMIT 1", (session_id,)
    ).fetchone()
    return row[0] if row else None


# --- PresentMon frame-time import ------------------------------------------


def insert_presentmon_summary(
    session_id: int,
    imported_ts: float,
    csv_path: str,
    frame_count: int,
    dropped_count: int,
    p50_ms: float,
    p90_ms: float,
    p95_ms: float,
    p99_ms: float,
    max_ms: float,
    pct_over_694: float,
    pct_over_833: float,
    per_second_json: str,
) -> None:
    with _write() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO session_presentmon
               (session_id, imported_ts, csv_path, frame_count, dropped_count,
                p50_ms, p90_ms, p95_ms, p99_ms, max_ms, pct_over_694, pct_over_833, per_second_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id, imported_ts, csv_path, frame_count, dropped_count,
                p50_ms, p90_ms, p95_ms, p99_ms, max_ms, pct_over_694, pct_over_833, per_second_json,
            ),
        )


def get_presentmon_summary(session_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        """SELECT imported_ts, csv_path, frame_count, dropped_count, p50_ms, p90_ms, p95_ms, p99_ms,
                  max_ms, pct_over_694, pct_over_833, per_second_json
           FROM session_presentmon WHERE session_id = ?""",
        (session_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "imported_ts": row[0],
        "csv_path": row[1],
        "frame_count": row[2],
        "dropped_count": row[3],
        "p50_ms": row[4],
        "p90_ms": row[5],
        "p95_ms": row[6],
        "p99_ms": row[7],
        "max_ms": row[8],
        "pct_over_694": row[9],
        "pct_over_833": row[10],
        "per_second": json.loads(row[11]) if row[11] else {"t": [], "avg_ms": [], "max_ms": []},
    }


# --- DPC/ISR (interrupt load) import ----------------------------------------


def insert_dpcisr_summary(
    session_id: int,
    imported_ts: float,
    report_path: str,
    dominant_cpu: int | None,
    dominant_cpu_pct_of_total: float | None,
    dominant_cpu_top_module: str | None,
    wdf_total_usec: float,
    wdf_dominant_cpu: int | None,
    wdf_dominant_cpu_usec: float | None,
    per_cpu_json: str,
) -> None:
    with _write() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO session_dpcisr
               (session_id, imported_ts, report_path, dominant_cpu, dominant_cpu_pct_of_total,
                dominant_cpu_top_module, wdf_total_usec, wdf_dominant_cpu, wdf_dominant_cpu_usec, per_cpu_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id, imported_ts, report_path, dominant_cpu, dominant_cpu_pct_of_total,
                dominant_cpu_top_module, wdf_total_usec, wdf_dominant_cpu, wdf_dominant_cpu_usec, per_cpu_json,
            ),
        )


def get_dpcisr_summary(session_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        """SELECT imported_ts, report_path, dominant_cpu, dominant_cpu_pct_of_total, dominant_cpu_top_module,
                  wdf_total_usec, wdf_dominant_cpu, wdf_dominant_cpu_usec, per_cpu_json
           FROM session_dpcisr WHERE session_id = ?""",
        (session_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "imported_ts": row[0],
        "report_path": row[1],
        "dominant_cpu": row[2],
        "dominant_cpu_pct_of_total": row[3],
        "dominant_cpu_top_module": row[4],
        "wdf_total_usec": row[5],
        "wdf_dominant_cpu": row[6],
        "wdf_dominant_cpu_usec": row[7],
        "per_cpu": json.loads(row[8]) if row[8] else {},
    }


# --- inserts ---------------------------------------------------------------


def insert_system_sample(ts: float, **kw) -> None:
    with _write() as conn:
        conn.execute(
            """INSERT INTO system_samples
               (ts, cpu_total_pct, cpu_p_pct, cpu_e_pct, cpu_max_core_pct, ram_used_mb, ram_total_mb,
                pagefile_used_mb, pagefile_total_mb,
                disk_read_bps, disk_write_bps, net_sent_bps, net_recv_bps)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ts,
                kw.get("cpu_total_pct"),
                kw.get("cpu_p_pct"),
                kw.get("cpu_e_pct"),
                kw.get("cpu_max_core_pct"),
                kw.get("ram_used_mb"),
                kw.get("ram_total_mb"),
                kw.get("pagefile_used_mb"),
                kw.get("pagefile_total_mb"),
                kw.get("disk_read_bps"),
                kw.get("disk_write_bps"),
                kw.get("net_sent_bps"),
                kw.get("net_recv_bps"),
            ),
        )


def insert_gpu_sample(ts: float, **kw) -> None:
    with _write() as conn:
        conn.execute(
            """INSERT INTO gpu_samples
               (ts, gpu_util_pct, encoder_util_pct, decoder_util_pct, vram_used_mb,
                vram_total_mb, temp_c, clock_mhz, mem_util_pct, throttled, throttle_reasons,
                iracing_sm_util_pct, iracing_enc_util_pct, power_draw_w, iracing_vram_mb)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ts,
                kw.get("gpu_util_pct"),
                kw.get("encoder_util_pct"),
                kw.get("decoder_util_pct"),
                kw.get("vram_used_mb"),
                kw.get("vram_total_mb"),
                kw.get("temp_c"),
                kw.get("clock_mhz"),
                kw.get("mem_util_pct"),
                int(kw["throttled"]) if kw.get("throttled") is not None else None,
                ",".join(kw["throttle_reasons"]) if kw.get("throttle_reasons") else None,
                kw.get("iracing_sm_util_pct"),
                kw.get("iracing_enc_util_pct"),
                kw.get("power_draw_w"),
                kw.get("iracing_vram_mb"),
            ),
        )


def insert_process_sample(ts: float, **kw) -> None:
    with _write() as conn:
        conn.execute(
            """INSERT INTO process_samples (ts, pid, cpu_pct, rss_mb)
               VALUES (?,?,?,?)""",
            (
                ts,
                kw.get("pid"),
                kw.get("cpu_pct"),
                kw.get("rss_mb"),
            ),
        )


def insert_top_process_samples(ts: float, rows: list[dict]) -> None:
    if not rows:
        return
    with _write() as conn:
        conn.executemany(
            """INSERT INTO top_process_samples (ts, pid, name, cpu_pct, rss_mb)
               VALUES (?,?,?,?,?)""",
            [(ts, r.get("pid"), r.get("name"), r.get("cpu_pct"), r.get("rss_mb")) for r in rows],
        )


def insert_iracing_sample(ts: float, **kw) -> None:
    with _write() as conn:
        conn.execute(
            """INSERT INTO iracing_samples
               (ts, connected, frame_rate, session_time, speed, lap, lap_dist_pct, on_track)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                ts,
                int(kw.get("connected", False)),
                kw.get("frame_rate"),
                kw.get("session_time"),
                kw.get("speed"),
                kw.get("lap"),
                kw.get("lap_dist_pct"),
                int(kw.get("on_track", False)) if kw.get("on_track") is not None else None,
            ),
        )


def insert_roster_events(rows: list[dict]) -> None:
    if not rows:
        return
    with _write() as conn:
        conn.executemany(
            """INSERT INTO iracing_roster_events (ts, event_type, car_idx, driver_name, num_drivers)
               VALUES (?,?,?,?,?)""",
            [(r["ts"], r.get("type"), r.get("car_idx"), r.get("name"), r.get("num_drivers")) for r in rows],
        )


def insert_windows_events(rows: list[dict]) -> list[dict]:
    """Inserts events not already stored (by record_key). Returns the ones
    that were actually new."""
    new = []
    with _write() as conn:
        for r in rows:
            cur = conn.execute(
                """INSERT OR IGNORE INTO windows_events (ts, provider, event_id, level, message, record_key)
                   VALUES (?,?,?,?,?,?)""",
                (r["ts"], r.get("provider"), r.get("event_id"), r.get("level"), r.get("message"), r.get("record_key")),
            )
            if cur.rowcount:
                new.append(r)
    return new


def get_windows_event(record_key: str) -> sqlite3.Row | None:
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM windows_events WHERE record_key = ?", (record_key,)).fetchone()
    finally:
        conn.row_factory = None


# --- queries (used by reports/build_report.py) ------------------------------


_QUERYABLE_TABLES = {
    "system_samples",
    "gpu_samples",
    "process_samples",
    "top_process_samples",
    "iracing_samples",
    "iracing_roster_events",
    "windows_events",
}


def query_range(table: str, start_ts: float, end_ts: float) -> list[sqlite3.Row]:
    if table not in _QUERYABLE_TABLES:
        raise ValueError(f"unknown table: {table}")
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.execute(f"SELECT * FROM {table} WHERE ts BETWEEN ? AND ? ORDER BY ts", (start_ts, end_ts))
    rows = cur.fetchall()
    conn.row_factory = None
    return rows
