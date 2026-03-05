import asyncio
import json
import logging
import os

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import websockets
from websockets.exceptions import ConnectionClosed

from prompt import INSTRUCTIONS_V1

logger = logging.getLogger("realtime-agent-v1")

OPENAI_WS_BASE = "wss://api.openai.com/v1/realtime"
DEFAULT_MODEL = "gpt-4o-realtime-preview"


app = FastAPI()

# --- Helpers --------------------------------------------------------------- #


def _get_openai_url() -> str:
    model = os.getenv("OPENAI_REALTIME_MODEL", DEFAULT_MODEL)
    return f"{OPENAI_WS_BASE}?model={model}"


def _get_openai_headers() -> dict:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required")
    return {"Authorization": f"Bearer {api_key}"}


# --- WebSocket endpoint ---------------------------------------------------- #


@app.websocket("/ws")
async def websocket_proxy(ws: WebSocket):
    await ws.accept()

    try:
        openai_ws = await websockets.connect(
            _get_openai_url(),
            extra_headers=_get_openai_headers(),
            max_size=16 * 1024 * 1024,
        )
    except Exception as exc:
        await ws.close(code=1011)
        logger.exception("Failed to connect to OpenAI Realtime: %s", exc)
        return

    # Send session.update with instructions (no tools)
    session_update = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": INSTRUCTIONS_V1,
            "tools": [],
            "tool_choice": "none",
        },
    }
    await openai_ws.send(json.dumps(session_update))
    logger.info("Session configured (no tools)")

    async def client_to_openai():
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    try:
                        event = json.loads(msg["text"])
                        if event.get("type") == "session.update":
                            session = event.setdefault("session", {})
                            session["instructions"] = INSTRUCTIONS_V1
                            session["tools"] = []
                            session["tool_choice"] = "none"
                            session.setdefault("type", "realtime")
                            session.pop("modalities", None)
                            session.pop("voice", None)
                            session.pop("input_audio_format", None)
                            session.pop("output_audio_format", None)
                            await openai_ws.send(json.dumps(event))
                            continue
                    except (json.JSONDecodeError, TypeError):
                        pass
                    await openai_ws.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await openai_ws.send(msg["bytes"])
        except WebSocketDisconnect:
            pass
        except ConnectionClosed:
            pass

    async def openai_to_client():
        try:
            async for message in openai_ws:
                if isinstance(message, bytes):
                    await ws.send_bytes(message)
                    continue

                try:
                    event = json.loads(message)
                except (json.JSONDecodeError, TypeError):
                    await ws.send_text(message)
                    continue

                event_type = event.get("type", "")
                logger.info("OpenAI event: %s", event_type)

                if event_type == "error":
                    logger.error("OpenAI error: %s", json.dumps(event, ensure_ascii=False))

                await ws.send_text(message)

        except ConnectionClosed:
            pass

    try:
        await asyncio.gather(client_to_openai(), openai_to_client())
    finally:
        try:
            await openai_ws.close()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
