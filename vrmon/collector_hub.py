"""Owns one background thread per collector, writes samples to SQLite,
and keeps an in-memory "latest snapshot" dict that the web server polls
for the live dashboard.

Recording to disk is gated on iRacing's sim process running - one
session per sim launch. Outside of that window the collectors still
sample (cheaply, in-memory only) so the live dashboard stays responsive,
but nothing is written - so leaving this running while the PC idles does
not grow the database.
"""

import logging
import os
import subprocess
import sys
import threading
import time
import traceback

from vrmon import config, settings_reader, store
from vrmon.collectors.gpu import GpuCollector
from vrmon.collectors.iracing import IRacingCollector
from vrmon.collectors.process import ProcessCollector
from vrmon.collectors.system import SystemCollector
from vrmon.collectors.top_processes import TopProcessCollector
from vrmon.collectors.windows_events import WindowsEventCollector

log = logging.getLogger(__name__)


class CollectorHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._latest = {
            "system": None,
            "gpu": None,
            "gpu_available": None,  # None = still starting up, then True/False
            "gpu_info": None,  # {"name", "backend"} once the GPU collector opens
            "process": None,
            "top_processes": [],
            "iracing": None,
            "recording": False,
            "session_id": None,
            "server_started_ts": time.time(),
            # Per-process CPU is reported as % of one core (100 = one core
            # fully busy); the dashboard divides by this to show % of the
            # whole CPU.
            "cpu_count": os.cpu_count(),
        }
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._heartbeats: dict[str, float] = {}
        self._thread_idents: dict[str, int] = {}
        self._session_id: int | None = None
        self._iracing_sim_running = False
        self._recording = False
        self._events_since: float | None = None

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._latest)

    def _set(self, key: str, value) -> None:
        with self._lock:
            self._latest[key] = value

    def _heartbeat(self, name: str) -> None:
        with self._lock:
            self._heartbeats[name] = time.time()

    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    def _current_iracing_pid(self) -> int | None:
        with self._lock:
            process = self._latest["process"]
            return process["pid"] if process else None

    def start(self) -> None:
        store.init_db()

        # Anything still open means the last run never shut down cleanly -
        # most likely the PC itself crashed mid-session.
        for sid in store.close_orphaned_sessions():
            log.warning("session %s was left open by an unclean shutdown - closed at its last recorded sample", sid)
        # Look back to when we last recorded anything, so a hard crash's
        # own event-log records (written as Windows boots back up) still
        # get captured even though vrmon wasn't running to see them.
        self._events_since = store.last_activity_ts()

        loops = [
            ("system", self._system_loop),
            ("gpu", self._gpu_loop),
            ("process", self._process_loop),
            ("top_processes", self._top_process_loop),
            ("iracing", self._iracing_loop),
            ("windows_events", self._windows_events_loop),
        ]
        for name, target in loops:
            t = threading.Thread(target=self._guarded, args=(name, target), daemon=True)
            t.start()
            self._threads.append(t)

        wd = threading.Thread(target=self._watchdog_loop, daemon=True)
        wd.start()
        self._threads.append(wd)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)
        if self._session_id is not None:
            store.end_session(self._session_id)

    def _guarded(self, name: str, target) -> None:
        with self._lock:
            self._thread_idents[name] = threading.get_ident()
        self._heartbeat(name)
        try:
            target()
        except Exception:
            log.exception("collector loop %s crashed", name)

    # Loops are stalled rather than crashed when a native call (NVML,
    # psutil, etc.) blocks forever without raising - _guarded's try/except
    # never fires in that case. This periodically checks each loop's last
    # heartbeat and, if one's gone stale, dumps that thread's current
    # Python stack so we can see exactly which call it's stuck in.
    def _watchdog_loop(self) -> None:
        thresholds = {
            "system": 5.0,
            "gpu": 5.0,
            "process": 5.0,
            "top_processes": 15.0,
            "iracing": 5.0,
            "windows_events": 90.0,  # first poll looks back over days of logs
        }
        warned: set[str] = set()
        while not self._stop.wait(5.0):
            now = time.time()
            with self._lock:
                heartbeats = dict(self._heartbeats)
            for name, last in heartbeats.items():
                limit = thresholds.get(name, 10.0)
                age = now - last
                if age > limit:
                    if name not in warned:
                        log.error("collector loop %r stalled: no heartbeat in %.1fs (limit %.1fs)", name, age, limit)
                        self._dump_thread_stack(name)
                        warned.add(name)
                else:
                    warned.discard(name)

    def _dump_thread_stack(self, name: str) -> None:
        ident = self._thread_idents.get(name)
        if ident is None:
            return
        frame = sys._current_frames().get(ident)
        if frame is None:
            return
        log.error("stack for stalled loop %r:\n%s", name, "".join(traceback.format_stack(frame)))

    # --- recording gate: one session per iRacing sim launch ---------------

    def _update_recording_state(self, ts: float) -> None:
        just_started_session_id = None
        with self._lock:
            should_record = self._iracing_sim_running
            if should_record and not self._recording:
                self._session_id = store.start_session(ts)
                just_started_session_id = self._session_id
                log.info("iRacing active - recording started (session %s)", self._session_id)
            elif not should_record and self._recording and self._session_id is not None:
                store.end_session(self._session_id, ts)
                log.info("recording stopped - session %s ended", self._session_id)
                self._session_id = None

            self._recording = should_record
            self._latest["recording"] = should_record
            self._latest["session_id"] = self._session_id

        # Done outside the lock, in its own thread - it keeps polling for
        # track/car for as long as the session lasts, and must never stall
        # the process polling loop that triggered it.
        if just_started_session_id is not None:
            self._trigger_presentmon_capture()
            threading.Thread(
                target=self._capture_settings_snapshot,
                args=(just_started_session_id, ts),
                daemon=True,
            ).start()

    def _trigger_presentmon_capture(self) -> None:
        """Asks the (non-elevated) OS scheduler to run the pre-authorized,
        elevated PresentMon capture task - this call itself needs no
        elevation, only the one-time task creation did. Silently no-ops
        if the task was never set up (schtasks errors are expected until
        then), so this never blocks or breaks recording."""
        try:
            subprocess.Popen(
                ["schtasks", "/run", "/tn", config.PRESENTMON_TASK_NAME],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=config.NO_WINDOW,
            )
        except OSError:
            log.debug("failed to trigger PresentMon capture task", exc_info=True)

    def _capture_settings_snapshot(self, session_id: int, ts: float) -> None:
        try:
            snap = settings_reader.read_all_settings()
            store.insert_session_settings(session_id, ts, snap["iracing_renderer"])
            log.info("session %s: settings snapshot captured (track/car pending)", session_id)
        except Exception:
            log.warning("failed to capture settings snapshot for session %s", session_id, exc_info=True)

        # Track/car has no reliable deadline - getting from "process
        # launched" to "actually in a session with WeekendInfo populated"
        # can take anywhere from seconds to several minutes depending on
        # menu navigation, and there's no fixed timeout that's honest
        # about that. So instead of a bounded one-shot attempt, this just
        # keeps checking the *already continuously running* iRacing
        # collector (self._latest["iracing"], updated every tick by
        # _iracing_loop for as long as the session is recording) until it
        # reports a track, or the session ends - the same "no deadline,
        # just keep polling" approach a long-running tool naturally gets
        # for free by always being on.
        try:
            while not self._stop.is_set():
                current = self.snapshot()
                if current.get("session_id") != session_id:
                    break  # this session ended (or another started) before track/car showed up
                iracing = current.get("iracing") or {}
                track_name = iracing.get("track_name")
                if track_name:
                    store.update_session_track_car(
                        session_id, track_name, iracing.get("track_config"), iracing.get("car_name")
                    )
                    log.info("session %s: track/car filled in - %s / %s", session_id, track_name, iracing.get("car_name"))
                    break
                self._stop.wait(config.SETTINGS_CAPTURE_POLL_INTERVAL_S)
        except Exception:
            log.warning("failed to backfill track/car for session %s", session_id, exc_info=True)

    # --- individual loops -------------------------------------------------

    def _system_loop(self) -> None:
        collector = SystemCollector()
        while not self._stop.is_set():
            self._heartbeat("system")
            sample = collector.sample()
            if self.is_recording():
                store.insert_system_sample(**sample)
            self._set("system", sample)
            self._stop.wait(config.SYSTEM_POLL_INTERVAL)

    def _gpu_loop(self) -> None:
        try:
            collector = GpuCollector()
        except Exception:
            log.warning("GPU collector unavailable (no GPU stats source could be opened)", exc_info=True)
            self._set("gpu_available", False)
            return
        log.info("GPU: %s (stats via %s)", collector.name, collector.backend)
        self._set("gpu_info", {"name": collector.name, "backend": collector.backend})
        self._set("gpu_available", True)
        try:
            while not self._stop.is_set():
                self._heartbeat("gpu")
                ts = time.time()
                sample = collector.sample(target_pid=self._current_iracing_pid())
                if self.is_recording():
                    store.insert_gpu_sample(ts, **sample)
                self._set("gpu", {"ts": ts, **sample})
                self._stop.wait(config.GPU_POLL_INTERVAL)
        finally:
            collector.shutdown()

    def _process_loop(self) -> None:
        collector = ProcessCollector()
        while not self._stop.is_set():
            self._heartbeat("process")
            ts = time.time()
            sample = collector.sample()

            self._iracing_sim_running = sample is not None
            self._update_recording_state(ts)

            if sample is not None and self.is_recording():
                store.insert_process_sample(ts, **sample)
            self._set("process", {"ts": ts, **sample} if sample is not None else None)

            self._stop.wait(config.PROCESS_POLL_INTERVAL)

    def _top_process_loop(self) -> None:
        collector = TopProcessCollector()
        while not self._stop.is_set():
            self._heartbeat("top_processes")
            ts = time.time()
            try:
                rows = collector.sample()
            except Exception:
                log.debug("top-process sample failed", exc_info=True)
                rows = []
            if self.is_recording():
                store.insert_top_process_samples(ts, rows)
            self._set("top_processes", rows)
            self._stop.wait(config.TOP_PROCESS_POLL_INTERVAL)

    def _iracing_loop(self) -> None:
        collector = IRacingCollector()
        while not self._stop.is_set():
            self._heartbeat("iracing")
            ts = time.time()
            try:
                sample = collector.sample()
            except Exception:
                log.debug("iracing sample failed", exc_info=True)
                sample = {"connected": False}
            roster_events = sample.pop("roster_events", None) or []
            if self.is_recording():
                store.insert_iracing_sample(ts, **sample)
                if roster_events:
                    for e in roster_events:
                        e["ts"] = ts
                    store.insert_roster_events(roster_events)
                    for e in roster_events:
                        log.info("roster: %s car_idx=%s name=%s (%s drivers)", e["type"], e["car_idx"], e.get("name"), e.get("num_drivers"))
            self._set("iracing", {"ts": ts, **sample})
            self._stop.wait(config.IRACING_POLL_INTERVAL)

    def _windows_events_loop(self) -> None:
        collector = WindowsEventCollector(since=self._events_since)
        while not self._stop.is_set():
            self._heartbeat("windows_events")
            try:
                events = collector.poll()
            except Exception:
                log.debug("windows event poll failed", exc_info=True)
                events = []
            # Stored whether or not a session is recording - they're rare,
            # and a crash's records land after the session it ended.
            for e in store.insert_windows_events(events):
                log.warning("Windows event: %s/%s (%s): %s", e["provider"], e["event_id"], e["level"], e["message"][:200])
            self._stop.wait(config.WINDOWS_EVENTS_POLL_INTERVAL)
