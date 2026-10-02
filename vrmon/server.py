"""FastAPI app: serves the live dashboard and a WebSocket feed of the
latest collector snapshot. The collector_hub.CollectorHub instance is
attached to app.state.hub by main.py before uvicorn starts serving.
"""

import asyncio
import logging

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from vrmon import config, events, report_list, settings_reader, store

log = logging.getLogger(__name__)

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")

_PUSH_INTERVAL = 0.25


@app.get("/")
def index():
    return FileResponse(str(config.WEB_DIR / "index.html"))


@app.get("/api/sessions")
def api_sessions():
    return store.list_sessions()


@app.get("/api/sessions/{session_id}")
def api_session(session_id: int):
    session = store.get_session(session_id)
    if session is None:
        return {"error": "not found"}
    return session


@app.get("/api/sessions/{session_id}/settings")
def api_session_settings(session_id: int):
    settings = store.get_session_settings(session_id)
    if settings is None:
        return {"error": "no settings captured for this session"}
    return settings


@app.get("/api/settings/current")
def api_settings_current():
    """Reads the config files fresh, right now - not tied to any session.
    Answers "what are my graphics settings currently set to"."""
    return settings_reader.read_all_settings()


@app.get("/api/events/recent")
def api_recent_events(hours: float = 48):
    """Windows crash/driver events, iRacing launcher/anti-cheat log errors,
    sim crash reports and session start/stops, newest first."""
    return events.recent_events(hours=hours)


@app.get("/api/events/detail")
def api_event_detail(kind: str, key: str = "", file: str = "", line: int = 0):
    try:
        return events.event_detail(kind, key=key, file=file, line=line)
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/events/open")
def api_event_open(request: Request, kind: str, key: str = "", file: str = ""):
    """Opens an entry's source (Event Viewer / Notepad / Explorer) on THIS PC.
    The dashboard is reachable from the local network, so only requests from
    this PC itself are allowed to open anything."""
    if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="only available on the PC running iRacing Monitor")
    try:
        events.open_event_source(kind, key=key, file=file)
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@app.get("/api/reports")
def api_reports():
    """Generated reports, newest session first."""
    return report_list.list_reports()


@app.get("/reports/{name}")
def report_file(name: str):
    """Serves a generated report so it can be opened from the dashboard
    (also from a phone). Only report files in the reports folder."""
    try:
        path = report_list.report_path(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    media_type = "application/pdf" if path.suffix == ".pdf" else "text/html"
    return FileResponse(str(path), media_type=media_type)


@app.get("/settings")
def settings_page():
    return FileResponse(str(config.WEB_DIR / "settings.html"))


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    await websocket.accept()
    hub = websocket.app.state.hub
    try:
        while True:
            await websocket.send_json(hub.snapshot())
            await asyncio.sleep(_PUSH_INTERVAL)
    except WebSocketDisconnect:
        pass
