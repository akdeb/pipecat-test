"""Local multi-transport Pipecat server for browser WebRTC and ESP32 WSS."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict, Union

import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from loguru import logger
from pipecat_ai_small_webrtc_prebuilt.frontend import SmallWebRTCPrebuiltUI

from bot import create_esp32_auth_message, run_bot_session
from esp32_transport import Esp32FrameSerializer, Esp32WebsocketTransport
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.smallwebrtc.transport import TransportParams as SmallWebRTCParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "7860"))
ESP32_INPUT_SAMPLE_RATE = int(os.getenv("ESP32_INPUT_SAMPLE_RATE", "16000"))
ESP32_OUTPUT_SAMPLE_RATE = int(os.getenv("ESP32_OUTPUT_SAMPLE_RATE", "24000"))


def create_browser_transport(connection: SmallWebRTCConnection) -> SmallWebRTCTransport:
    return SmallWebRTCTransport(
        webrtc_connection=connection,
        params=SmallWebRTCParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    )


def create_esp32_transport(websocket: WebSocket) -> Esp32WebsocketTransport:
    return Esp32WebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=ESP32_INPUT_SAMPLE_RATE,
            audio_out_sample_rate=ESP32_OUTPUT_SAMPLE_RATE,
            serializer=Esp32FrameSerializer(input_sample_rate=ESP32_INPUT_SAMPLE_RATE),
        ),
    )


def create_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    class IceServer(TypedDict, total=False):
        urls: Union[str, List[str]]

    class IceConfig(TypedDict):
        iceServers: List[IceServer]

    class StartBotResult(TypedDict, total=False):
        sessionId: str
        iceConfig: Optional[IceConfig]

    active_sessions: Dict[str, Dict[str, Any]] = {}
    background_session_tasks: set[asyncio.Task] = set()
    app.mount("/client", SmallWebRTCPrebuiltUI)

    @app.get("/", include_in_schema=False)
    async def root_redirect():
        return RedirectResponse(url="/client/")

    @app.get("/healthz")
    async def healthcheck():
        return {"ok": True}

    @app.get("/files/{filename:path}")
    async def download_file(filename: str):
        file_path = Path(filename)
        if not file_path.exists():
            raise HTTPException(404)
        return FileResponse(path=file_path, filename=file_path.name)

    small_webrtc_handler = SmallWebRTCRequestHandler(esp32_mode=False, host=HOST)

    @app.post("/api/offer")
    async def offer(request: SmallWebRTCRequest, background_tasks: BackgroundTasks):
        async def webrtc_connection_callback(connection: SmallWebRTCConnection):
            transport = create_browser_transport(connection)
            task = asyncio.create_task(run_bot_session(transport, "browser", False))
            background_session_tasks.add(task)
            task.add_done_callback(background_session_tasks.discard)

        return await small_webrtc_handler.handle_web_request(
            request=request,
            webrtc_connection_callback=webrtc_connection_callback,
        )

    @app.patch("/api/offer")
    async def ice_candidate(request: SmallWebRTCPatchRequest):
        await small_webrtc_handler.handle_patch_request(request)
        return {"status": "success"}

    @app.post("/start")
    async def rtvi_start(request: Request):
        try:
            request_data = await request.json()
        except Exception:
            request_data = {}

        session_id = str(uuid.uuid4())
        active_sessions[session_id] = request_data.get("body", {})

        result: StartBotResult = {"sessionId": session_id}
        if request_data.get("enableDefaultIceServers"):
            result["iceConfig"] = IceConfig(
                iceServers=[IceServer(urls=["stun:stun.l.google.com:19302"])]
            )
        return result

    @app.api_route(
        "/sessions/{session_id}/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def proxy_request(
        session_id: str, path: str, request: Request, background_tasks: BackgroundTasks
    ):
        active_session = active_sessions.get(session_id)
        if active_session is None:
            return Response(content="Invalid or not-yet-ready session_id", status_code=404)

        if path.endswith("api/offer"):
            try:
                request_data = await request.json()
                if request.method == "POST":
                    webrtc_request = SmallWebRTCRequest(
                        sdp=request_data["sdp"],
                        type=request_data["type"],
                        pc_id=request_data.get("pc_id"),
                        restart_pc=request_data.get("restart_pc"),
                        request_data=request_data.get("request_data")
                        or request_data.get("requestData")
                        or active_session,
                    )
                    return await offer(webrtc_request, background_tasks)
                if request.method == "PATCH":
                    patch_request = SmallWebRTCPatchRequest(
                        pc_id=request_data["pc_id"],
                        candidates=[IceCandidate(**c) for c in request_data.get("candidates", [])],
                    )
                    return await ice_candidate(patch_request)
            except Exception as exc:
                logger.error(f"Failed to parse WebRTC request: {exc}")
                return Response(content="Invalid WebRTC request", status_code=400)

        return Response(status_code=200)

    @app.websocket("/ws/esp32")
    async def esp32_websocket(websocket: WebSocket):
        await websocket.accept()

        logger.info(
            "ESP32 websocket connected: mac={} rssi={} auth={}",
            websocket.headers.get("x-device-mac", "unknown"),
            websocket.headers.get("x-wifi-rssi", "unknown"),
            "yes" if websocket.headers.get("authorization") else "no",
        )

        await websocket.send_text(json.dumps(create_esp32_auth_message()))

        transport = create_esp32_transport(websocket)
        await run_bot_session(transport, "esp32", False)

    @asynccontextmanager
    async def app_lifespan(app: FastAPI):
        yield
        await small_webrtc_handler.close()

    app.router.lifespan_context = app_lifespan
    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run("server:app", host=HOST, port=PORT, reload=False)
