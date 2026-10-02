"""iRacing telemetry via the SDK shared-memory interface (irsdk / pyirsdk).

There is no SDK variable for the in-game CPUMeter's C/R millisecond
readouts (that overlay is visual-only) - FrameRate is the closest
machine-readable stand-in for "the sim is stalling", combined with our
own system/process CPU sampling.
"""

import logging
import time

import irsdk

log = logging.getLogger(__name__)

_WANTED_VARS = ["FrameRate", "SessionTime", "Speed", "Lap", "IsOnTrack", "LapDistPct"]

# How often to re-check the driver roster for joins/leaves - DriverInfo
# is a YAML block (heavier to parse than a plain telemetry var) and
# roster changes don't need frame-level resolution, so this is throttled
# independent of the main per-tick sample rate.
_ROSTER_CHECK_INTERVAL_S = 2.0


class IRacingCollector:
    def __init__(self):
        self.ir = irsdk.IRSDK()
        self._was_connected = False
        self._track_name = None
        self._track_config = None
        self._car_name = None
        self._known_car_idxs: set[int] = set()
        self._last_roster_check = 0.0

    def _refresh_session_context(self) -> None:
        """WeekendInfo/DriverInfo can still be empty for a tick or two
        right as is_connected first flips true (the session info YAML is
        parsed on its own schedule, separate from the shared-memory
        connection state) - so this keeps getting called every tick
        until it actually finds a track, not just once at the connection
        edge, to avoid permanently caching an empty read."""
        try:
            weekend = self.ir["WeekendInfo"] or {}
            driver_info = self.ir["DriverInfo"] or {}
            track_name = weekend.get("TrackDisplayName") or weekend.get("TrackName")
            if not track_name:
                return
            self._track_name = track_name
            self._track_config = weekend.get("TrackConfigName")
            my_idx = driver_info.get("DriverCarIdx")
            for d in driver_info.get("Drivers") or []:
                if d.get("CarIdx") == my_idx:
                    self._car_name = d.get("CarScreenName")
                    break
        except Exception:
            log.debug("failed reading track/car context", exc_info=True)

    def _check_roster(self) -> list[dict]:
        """Compares the live driver roster (DriverInfo.Drivers, the same
        source RaceLab/CrewChief-style apps use for standings) against
        what was known last check - returns [{type, car_idx, name}] for
        anyone who joined or left since then. Throttled independent of
        the main sample rate since this re-parses a YAML block."""
        now = time.monotonic()
        if now - self._last_roster_check < _ROSTER_CHECK_INTERVAL_S:
            return []
        self._last_roster_check = now

        try:
            driver_info = self.ir["DriverInfo"] or {}
            drivers = driver_info.get("Drivers") or []
        except Exception:
            log.debug("failed reading DriverInfo for roster check", exc_info=True)
            return []

        current = {}
        for d in drivers:
            name = d.get("UserName")
            car_idx = d.get("CarIdx")
            # Empty/pace-car/safety-car slots have no real UserName -
            # not a driver joining or leaving.
            if name and car_idx is not None:
                current[car_idx] = name

        current_idxs = set(current)
        joined = current_idxs - self._known_car_idxs
        left = self._known_car_idxs - current_idxs

        events = []
        # First real read after connecting populates the whole grid at
        # once - that's the starting field, not N simultaneous "joins".
        if self._known_car_idxs or not current_idxs:
            for idx in joined:
                events.append({"type": "joined", "car_idx": idx, "name": current[idx], "num_drivers": len(current_idxs)})
            for idx in left:
                events.append({"type": "left", "car_idx": idx, "name": None, "num_drivers": len(current_idxs)})

        self._known_car_idxs = current_idxs
        return events

    def _ensure_connected(self) -> bool:
        if self.ir.is_initialized and self.ir.is_connected:
            return True
        try:
            self.ir.shutdown()
        except Exception:
            pass
        try:
            return bool(self.ir.startup())
        except Exception:
            log.debug("irsdk startup failed", exc_info=True)
            return False

    def is_running(self) -> bool:
        return self._ensure_connected() and self.ir.is_connected

    def _safe_get(self, name: str):
        try:
            if name in self.ir.var_headers_names:
                return self.ir[name]
        except Exception:
            log.debug("irsdk var read failed for %s", name, exc_info=True)
        return None

    def sample(self) -> dict:
        connected = self._ensure_connected() and self.ir.is_connected
        if not connected:
            if self._was_connected:
                # Dropped - clear cached track/car so a reconnect (to
                # possibly a different session) re-reads fresh instead
                # of keeping stale data forever.
                self._track_name = None
                self._track_config = None
                self._car_name = None
                self._known_car_idxs = set()
            self._was_connected = False
            return {"connected": False}

        if not self._track_name:
            self._refresh_session_context()
        self._was_connected = True

        roster_events = self._check_roster()

        self.ir.freeze_var_buffer_latest()
        try:
            values = {name: self._safe_get(name) for name in _WANTED_VARS}
        finally:
            self.ir.unfreeze_var_buffer_latest()

        return {
            "connected": True,
            "frame_rate": values.get("FrameRate"),
            "session_time": values.get("SessionTime"),
            "speed": values.get("Speed"),
            "lap": values.get("Lap"),
            "lap_dist_pct": values.get("LapDistPct"),
            "on_track": values.get("IsOnTrack"),
            "track_name": self._track_name,
            "track_config": self._track_config,
            "car_name": self._car_name,
            "roster_events": roster_events,
        }
