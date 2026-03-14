"""MCP Tool Registry — auto-discovers and manages pluggable tool modules.

Each tool module (*.py in mcp_tools/) exposes:
  - TOOLS: list[dict]   — OpenAI function-calling schemas
  - HANDLERS: dict       — {name: handler_func(user_id, user_name, params) -> dict}

Handlers may be sync or async. execute_tool() is async and handles both.

Usage:
    from app.mcp_tools.registry import get_all_tools, execute_tool

    tools = get_all_tools()                               # for LLM function-calling
    result = await execute_tool(name, uid, uname, params) # dispatch by name
"""

import asyncio
import importlib
import inspect
import logging
import pkgutil
from typing import Any, Callable

import app.mcp_tools as _pkg

logger = logging.getLogger(__name__)

# Global registries (populated on first call)
_tool_definitions: list[dict] = []
_tool_handlers: dict[str, Callable] = {}
_initialized = False


def _discover_tools() -> None:
    """Scan all modules in app.mcp_tools and collect TOOLS + HANDLERS."""
    global _tool_definitions, _tool_handlers, _initialized

    for importer, modname, ispkg in pkgutil.iter_modules(_pkg.__path__):
        if modname.startswith("_") or modname == "registry":
            continue
        try:
            mod = importlib.import_module(f"app.mcp_tools.{modname}")
            tools = getattr(mod, "TOOLS", [])
            handlers = getattr(mod, "HANDLERS", {})
            _tool_definitions.extend(tools)
            _tool_handlers.update(handlers)
            logger.info(
                "MCP tool module '%s' loaded: %d tools",
                modname, len(tools),
            )
        except Exception:
            logger.exception("Failed to load MCP tool module '%s'", modname)

    _initialized = True
    logger.info(
        "MCP registry initialized: %d tools, %d handlers",
        len(_tool_definitions), len(_tool_handlers),
    )


def get_all_tools() -> list[dict]:
    """Get all registered tool definitions for LLM function calling."""
    if not _initialized:
        _discover_tools()
    return _tool_definitions


def get_all_handlers() -> dict[str, Callable]:
    """Get all registered tool handlers."""
    if not _initialized:
        _discover_tools()
    return _tool_handlers


async def execute_tool(
    tool_name: str,
    user_id: int,
    user_name: str,
    params: dict,
) -> dict:
    """Execute a tool by name (supports both sync and async handlers)."""
    if not _initialized:
        _discover_tools()

    handler = _tool_handlers.get(tool_name)
    if handler is None:
        return {"success": False, "message": f"未知的工具: {tool_name}"}
    try:
        result = handler(user_id, user_name, params)
        # If the handler is async, await the coroutine
        if inspect.isawaitable(result):
            result = await result
        return result
    except Exception as e:
        logger.exception("Tool '%s' failed", tool_name)
        return {"success": False, "message": f"工具执行失败: {str(e)}"}


def register_tool(
    name: str,
    handler: Callable,
    schema: dict,
) -> None:
    """Dynamically register a tool at runtime (e.g., for external MCP servers)."""
    if not _initialized:
        _discover_tools()
    _tool_handlers[name] = handler
    _tool_definitions.append(schema)
    logger.info("Dynamically registered tool: %s", name)
