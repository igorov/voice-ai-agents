import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import websockets
from websockets.exceptions import ConnectionClosed

from qdrant_client import QdrantClient
from langchain_qdrant import QdrantVectorStore
from langchain_openai import OpenAIEmbeddings

from tools import load_neon_tools, get_all_tools, create_retrieve_context_tool
from prompt import INSTRUCTIONS

logger = logging.getLogger("realtime-agent-v3")

OPENAI_WS_BASE = "wss://api.openai.com/v1/realtime"
DEFAULT_MODEL = "gpt-4o-realtime-preview"

# --- Tool registry (populated at startup) ---------------------------------- #

TOOLS_BY_NAME: dict = {}
TOOL_DEFINITIONS: list = []


def _build_tool_registry(tools: list) -> None:
    """Populate the module-level TOOLS_BY_NAME and TOOL_DEFINITIONS."""
    global TOOLS_BY_NAME, TOOL_DEFINITIONS
    TOOLS_BY_NAME = {t.name: t for t in tools}
    TOOL_DEFINITIONS = [
        {
            "type": "function",
            "name": t.name,
            "description": t.description,
            "parameters": {
                "type": "object",
                "properties": t.args,
            },
        }
        for t in tools
    ]


def _init_qdrant() -> QdrantVectorStore | None:
    """Initialize Qdrant vector store. Returns None if credentials are not set."""
    qdrant_url = os.getenv("QDRANT_URL")
    qdrant_api_key = os.getenv("QDRANT_API_KEY")
    collection_name = os.getenv("QDRANT_COLLECTION_NAME", "documents")
    if not qdrant_url or not qdrant_api_key:
        logger.info("QDRANT_URL or QDRANT_API_KEY not set — skipping RAG tool")
        return None
    client = QdrantClient(url=qdrant_url, api_key=qdrant_api_key)
    embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
    return QdrantVectorStore(client=client, collection_name=collection_name, embedding=embeddings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: connect to Neon MCP (if configured), init Qdrant, and build tool registry."""
    mcp_tools = []
    try:
        mcp_tools, _ = await load_neon_tools()
        if mcp_tools:
            logger.info("Neon MCP connected — %d tool(s) available", len(mcp_tools))
    except Exception as exc:
        logger.warning("Failed to connect to Neon MCP: %s. Continuing without MCP tools.", exc)

    rag_tool = None
    try:
        qdrant_store = _init_qdrant()
        if qdrant_store is not None:
            rag_tool = create_retrieve_context_tool(qdrant_store)
            logger.info("Qdrant RAG tool initialized")
    except Exception as exc:
        logger.warning("Failed to initialize Qdrant RAG tool: %s. Continuing without it.", exc)

    all_tools = get_all_tools(mcp_tools or None, rag_tool=rag_tool)
    _build_tool_registry(all_tools)
    logger.info("Tool registry ready: %s", [t["name"] for t in TOOL_DEFINITIONS])

    yield


app = FastAPI(lifespan=lifespan)

# --- Helpers --------------------------------------------------------------- #


def _get_openai_url() -> str:
    model = os.getenv("OPENAI_REALTIME_MODEL", DEFAULT_MODEL)
    return f"{OPENAI_WS_BASE}?model={model}"


def _get_openai_headers() -> dict:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required")
    return {"Authorization": f"Bearer {api_key}"}


async def _execute_tool(name: str, call_id: str, arguments: str) -> dict:
    """Run a LangChain tool and return a conversation.item.create event."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        output = json.dumps({"error": f"Tool '{name}' not found"})
    else:
        try:
            args = json.loads(arguments)
            result = await tool.ainvoke(args)
            output = result if isinstance(result, str) else json.dumps(result)
        except Exception as exc:
            logger.exception("Tool '%s' failed: %s", name, exc)
            output = json.dumps({"error": str(exc)})

    return {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
        },
    }


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

    # Send session.update with tools and instructions
    session_update = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": INSTRUCTIONS,
            "tools": TOOL_DEFINITIONS,
            "tool_choice": "auto",
        },
    }
    await openai_ws.send(json.dumps(session_update))
    logger.info(
        "Session configured with %d tool(s): %s",
        len(TOOL_DEFINITIONS),
        [t["name"] for t in TOOL_DEFINITIONS],
    )

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
                            session["instructions"] = INSTRUCTIONS
                            session["tools"] = TOOL_DEFINITIONS
                            session["tool_choice"] = "auto"
                            session.setdefault("type", "realtime")
                            session.pop("modalities", None)
                            session.pop("voice", None)
                            session.pop("input_audio_format", None)
                            session.pop("output_audio_format", None)
                            await openai_ws.send(json.dumps(event))
                            logger.info("Merged tools into client session.update: %s", json.dumps(event, ensure_ascii=False))
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

                if event_type == "session.updated":
                    tools_count = len(event.get("session", {}).get("tools", []))
                    logger.info("Session updated by OpenAI — tools registered: %d", tools_count)
                    await ws.send_text(message)
                elif event_type == "error":
                    logger.error("OpenAI error: %s", json.dumps(event, ensure_ascii=False))
                    await ws.send_text(message)
                elif event_type == "response.function_call_arguments.done":
                    tool_name = event.get("name", "")
                    call_id = event.get("call_id", "")
                    arguments = event.get("arguments", "{}")
                    logger.info("Tool call: %s(%s)", tool_name, arguments)

                    await ws.send_text(json.dumps({
                        "type": "tool_call.executing",
                        "name": tool_name,
                        "arguments": arguments,
                    }))

                    result_event = await _execute_tool(tool_name, call_id, arguments)
                    logger.info("Tool result: %s", result_event["item"]["output"])

                    await openai_ws.send(json.dumps(result_event))
                    await openai_ws.send(json.dumps({
                        "type": "response.create",
                        "response": {},
                    }))

                    await ws.send_text(json.dumps({
                        "type": "tool_call.done",
                        "name": tool_name,
                        "output": result_event["item"]["output"],
                    }))
                else:
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
