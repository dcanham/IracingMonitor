"""iRacing telemetry via the SDK shared-memory interface (irsdk / pyirsdk).

There is no SDK variable for the in-game CPUMeter's C/R millisecond
readouts (that overlay is visual-only) - FrameRate is the closest
machine-readable stand-in for "the sim is stalling", combined with our
own system/process CPU sampling.

Also recorded, to tell apart the usual causes of a stutter or "time warp":
- Connection to the iRacing server: ChanQuality / ChanPartnerQuality
  (fraction, 1 = perfect), ChanLatency / ChanAvgLatency and ChanClockSkew
  (seconds). A dip here at a stutter points at the network.
- iRacing's own threads: CpuUsageFG (the foreground/render thread) and
  CpuUsageBG, GpuUsage - each a fraction of available time, 1s average.
- MemPageFaultSec / MemSoftPageFaultSec: hard page faults mean the sim
  had to wait for data to be read back from disk.
- SessionTick: the simulation's own 60 Hz tick counter. If it jumps by
  more than expected between samples, the simulation itself paused - as
  opposed to only the rendering stalling while the sim kept running.

Beyond those, everything else iRacing publishes that could plausibly bear
on performance is recorded too, so it's already there when a future
question needs it: what the sim was doing (session state, garage, replay,
camera, tow), what it was loading or writing (car textures, its own disk
telemetry, video capture), voice chat, how many cars were in the world
and near you, and the weather/time of day the renderer was drawing. Vars
the running iRacing build doesn't publish are simply stored as empty.

The full session info (iRacing's YAML: track, weather settings, every
driver and car, cameras, results) is snapshotted whenever it changes -
see session_info().
"""

import logging
import re
import time
import zlib

import irsdk
import yaml

log = logging.getLogger(__name__)

# Telemetry var -> stored column, for everything recorded as-is.
_RECORDED_VARS = {
    # Stutter diagnostics (see above)
    "SessionTick": "session_tick",
    "ChanQuality": "chan_quality",
    "ChanPartnerQuality": "chan_partner_quality",
    "ChanLatency": "chan_latency",
    "ChanAvgLatency": "chan_avg_latency",
    "ChanClockSkew": "chan_clock_skew",
    "CpuUsageFG": "cpu_usage_fg",
    "CpuUsageBG": "cpu_usage_bg",
    "GpuUsage": "ir_gpu_usage",
    "MemPageFaultSec": "mem_page_fault_sec",
    "MemSoftPageFaultSec": "mem_soft_page_fault_sec",
    # What the sim was doing
    "SessionNum": "session_num",
    "SessionState": "session_state",
    "SessionFlags": "session_flags",
    "SessionUniqueID": "session_unique_id",
    "IsOnTrackCar": "on_track_car",
    "IsInGarage": "in_garage",
    "IsGarageVisible": "garage_visible",
    "IsReplayPlaying": "replay_playing",
    "ReplayFrameNum": "replay_frame_num",
    "ReplayFrameNumEnd": "replay_frame_num_end",
    "CamCarIdx": "cam_car_idx",
    "CamCameraNumber": "cam_camera_number",
    "CamGroupNumber": "cam_group_number",
    "CamCameraState": "cam_camera_state",
    "PlayerTrackSurface": "player_track_surface",
    "OnPitRoad": "on_pit_road",
    "PlayerCarInPitStall": "in_pit_stall",
    "PlayerCarTowTime": "tow_time",
    "PitstopActive": "pitstop_active",
    # What it was loading or writing
    "LoadNumTextures": "load_num_textures",
    "OkToReloadTextures": "ok_to_reload_textures",
    "IsDiskLoggingEnabled": "disk_logging_enabled",
    "IsDiskLoggingActive": "disk_logging_active",
    "VidCapEnabled": "vid_cap_enabled",
    "VidCapActive": "vid_cap_active",
    "RadioTransmitCarIdx": "radio_transmit_car_idx",
    "RadioTransmitRadioIdx": "radio_transmit_radio_idx",
    # Scene: traffic, weather and lighting the renderer was drawing
    "CarDistAhead": "car_dist_ahead",
    "CarDistBehind": "car_dist_behind",
    "SessionTimeOfDay": "time_of_day",
    "SolarAltitude": "solar_altitude",
    "Skies": "skies",
    "Precipitation": "precipitation",
    "TrackWetness": "track_wetness",
    "FogLevel": "fog_level",
    "WeatherDeclaredWet": "declared_wet",
}

# Per-car arrays, reduced to counts (see _traffic()).
_ARRAY_VARS = ["CarIdxTrackSurface", "CarIdxLapDistPct", "PlayerCarIdx"]

_WANTED_VARS = ["FrameRate", "SessionTime", "Speed", "Lap", "IsOnTrack", "LapDistPct",
                *_RECORDED_VARS, *_ARRAY_VARS]

# A car within this distance (either direction along the track) counts as
# "near" - roughly what's on screen in front of and behind you.
_NEAR_CAR_M = 200.0

# Full session info can be tens of KB and changes often in a race (results
# update every lap) - snapshot it at most this often.
_SESSION_INFO_MIN_INTERVAL_S = 10.0

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
        self._track_length_m = None
        self._known_car_idxs: set[int] = set()
        self._last_roster_check = 0.0
        self._session_info_update = None
        self._last_session_info = 0.0
        self._fallback_info: tuple[int, dict] | None = None

    def _refresh_session_context(self) -> None:
        """WeekendInfo/DriverInfo can still be empty for a tick or two
        right as is_connected first flips true (the session info YAML is
        parsed on its own schedule, separate from the shared-memory
        connection state) - so this keeps getting called every tick
        until it actually finds a track, not just once at the connection
        edge, to avoid permanently caching an empty read."""
        try:
            weekend = self._session_section("WeekendInfo")
            driver_info = self._session_section("DriverInfo")
            track_name = weekend.get("TrackDisplayName") or weekend.get("TrackName")
            if not track_name:
                return
            self._track_name = track_name
            self._track_config = weekend.get("TrackConfigName")
            # e.g. "5.51 km" (or "3.41 mi" on some older builds)
            m = re.match(r"([\d.]+)\s*(km|mi)", str(weekend.get("TrackLength") or ""))
            if m:
                self._track_length_m = float(m.group(1)) * (1000.0 if m.group(2) == "km" else 1609.344)
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
            driver_info = self._session_section("DriverInfo")
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

    def _session_section(self, key: str) -> dict:
        """One top-level block of the session info (WeekendInfo,
        DriverInfo, ...).

        iRacing builds each driver's AbbrevName/Initials by cutting the
        first name after one *byte* - for a name starting with an accented
        letter (e.g. "Štěpán") that leaves half a UTF-8 character, which
        pyirsdk can't decode, so it fails on the whole block. In that case
        this parses the raw session info itself with the broken bytes
        replaced (cached until the session info next changes)."""
        try:
            return self.ir[key] or {}
        except Exception:
            pass
        try:
            header = self.ir._header
            update = header.session_info_update
            if self._fallback_info is None or self._fallback_info[0] != update:
                start = header.session_info_offset
                raw = bytes(self.ir._shared_mem[start:start + header.session_info_len]).rstrip(b"\x00")
                text = raw.decode("utf-8", errors="replace")
                # Control characters aren't valid YAML.
                text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
                self._fallback_info = (update, yaml.safe_load(text) or {})
            return self._fallback_info[1].get(key) or {}
        except Exception:
            log.debug("failed reading session info %s", key, exc_info=True)
            return {}

    def _traffic(self, values: dict) -> dict:
        """Cars in the world (loaded and drawn somewhere on track, pit
        road or in their stall) and cars near you - each one is more for
        the sim to simulate and render."""
        surfaces = values.get("CarIdxTrackSurface")
        if not surfaces:
            return {"cars_in_world": None, "cars_near": None}
        me = values.get("PlayerCarIdx")
        # irsdk_TrkLoc: -1 = not in world, 0 = off track, 1 = pit stall,
        # 2 = approaching pits, 3 = on track.
        in_world = [i for i, s in enumerate(surfaces) if s is not None and s >= 0]
        near = None
        pcts = values.get("CarIdxLapDistPct")
        if pcts and me is not None and 0 <= me < len(pcts) and pcts[me] is not None and pcts[me] >= 0:
            # Without a track length, fall back to ~2% of a lap (about 100m
            # at a 5km track).
            window = _NEAR_CAR_M / self._track_length_m if self._track_length_m else 0.02
            near = 0
            for i in in_world:
                if i == me or pcts[i] is None or pcts[i] < 0:
                    continue
                gap = abs(pcts[i] - pcts[me]) % 1.0
                if min(gap, 1.0 - gap) <= window:
                    near += 1
        return {"cars_in_world": len(in_world), "cars_near": near}

    def session_info(self) -> tuple[int, bytes] | None:
        """(update number, zlib-compressed YAML) when iRacing's session
        info has changed since the last snapshot - otherwise None.
        Stored raw rather than parsed, so nothing in it is lost."""
        try:
            if not (self.ir.is_initialized and self.ir.is_connected):
                return None
            header = self.ir._header
            update = header.session_info_update
            if update == self._session_info_update:
                return None
            now = time.monotonic()
            # Always take the first one; after that, rate-limit.
            if self._session_info_update is not None and now - self._last_session_info < _SESSION_INFO_MIN_INTERVAL_S:
                return None
            start = header.session_info_offset
            raw = bytes(self.ir._shared_mem[start:start + header.session_info_len]).rstrip(b"\x00")
            if not raw:
                return None
            self._session_info_update = update
            self._last_session_info = now
            return update, zlib.compress(raw, 6)
        except Exception:
            log.debug("failed reading session info", exc_info=True)
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
                self._track_length_m = None
                self._known_car_idxs = set()
                self._session_info_update = None
                self._fallback_info = None
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
            **{column: values.get(var) for var, column in _RECORDED_VARS.items()},
            **self._traffic(values),
            "track_name": self._track_name,
            "track_config": self._track_config,
            "car_name": self._car_name,
            "roster_events": roster_events,
        }
