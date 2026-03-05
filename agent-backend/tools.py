import json
import logging
import os
import random
from datetime import datetime

from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient


@tool
def get_weather(city: str) -> str:
    """Obtiene el clima actual de una ciudad. Úsala cuando el usuario pregunte por el clima o temperatura de algún lugar."""
    # Simulación — reemplazar con una API real (ej: OpenWeatherMap)
    conditions = ["soleado", "nublado", "lluvioso", "parcialmente nublado"]
    temp = random.randint(15, 35)
    condition = random.choice(conditions)
    return json.dumps(
        {"city": city, "temperature_c": temp, "condition": condition},
        ensure_ascii=False,
    )

@tool
def get_current_time() -> str:
    """Devuelve la fecha y hora actual. Úsala cuando el usuario pregunte qué hora es o la fecha de hoy."""
    now = datetime.now()
    return now.strftime("%A %d de %B de %Y, %H:%M:%S")

LOCAL_TOOLS = [get_weather, get_current_time]

def create_retrieve_context_tool(qdrant_store):
    """Factory function that creates the retrieve_context tool with access to qdrant_store."""
    @tool
    def retrieve_context(query: str) -> str:
        """Busca informacion en la base de conocimiento. Usala cuando el usuario pregunte sobre cursos, docentes, graduados, horarios, matriculas, programas, sedes o cualquier informacion relacionada a la academia."""
        retrieved_docs = qdrant_store.similarity_search(query, k=5)
        serialized = "\n\n".join(
            (f"Fuente: {doc.metadata}\nContenido: {doc.page_content}")
            for doc in retrieved_docs
        )
        return serialized if serialized else "No se encontro informacion relevante."
    return retrieve_context

logger = logging.getLogger("tools")


async def load_neon_tools() -> tuple[list, object | None]:
    """Connect to Neon MCP and return (tools, client). Returns ([], None) if NEON_API_KEY is not set."""
    neon_api_key = os.getenv("NEON_API_KEY")
    if not neon_api_key:
        logger.info("NEON_API_KEY not set — skipping Neon MCP integration")
        return [], None

    client = MultiServerMCPClient(
        {
            "neon": {
                "transport": "streamable_http",
                "url": "https://mcp.neon.tech/mcp",
                "headers": {
                    "Authorization": f"Bearer {neon_api_key}"
                },
            }
        }
    )
    tools = await client.get_tools()
    return tools, client


def get_all_tools(mcp_tools: list | None = None, rag_tool=None) -> list:
    """Return local tools merged with any MCP-sourced tools and an optional RAG tool."""
    all_tools = list(LOCAL_TOOLS)
    if rag_tool is not None:
        all_tools.append(rag_tool)
        logger.info("RAG tool '%s' added to registry", rag_tool.name)
    if mcp_tools:
        all_tools.extend(mcp_tools)
        logger.info("Loaded %d Neon MCP tool(s): %s", len(mcp_tools), [t.name for t in mcp_tools])
    return all_tools
