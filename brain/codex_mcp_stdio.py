"""Expose an existing Mastermind in-process MCP surface over stdio.

Codex CLI speaks standard MCP over stdio, while the original Claude Agent SDK
embedded these servers directly in the bot process.  This adapter rebuilds the
same book-scoped surface in a child process; it does not add tools or authority.
"""
from __future__ import annotations

import argparse
import asyncio

from mcp.server.stdio import stdio_server


def _servers_for(book: str) -> dict:
    if book == "autonomous":
        from brain import autonomous_mcp as mod
        return mod.build_servers()
    if book == "etf":
        from brain import etf_mcp as mod
        return mod.build_servers()
    if book == "heavyweight":
        from brain import heavyweight_mcp as mod
        return mod.build_servers()
    if book == "china":
        from brain import china_mcp as mod
        return mod.build_servers()
    if book == "hk":
        from brain import hk_mcp as mod
        return mod.build_servers()
    if book in {"flagship", "flagship_judgment"}:
        from brain import flagship_desk_mcp as mod
        return mod.build_servers()
    from brain import bot_mcp
    return {bot_mcp.SERVER_NAME: bot_mcp.build_server()}


def server_instance(book: str, name: str):
    servers = _servers_for(book)
    cfg = servers.get(name)
    if not isinstance(cfg, dict) or cfg.get("type") != "sdk" or not cfg.get("instance"):
        raise SystemExit(f"MCP server {name!r} is not authorized for book {book!r}")
    return cfg["instance"]


async def _run(book: str, name: str) -> None:
    server = server_instance(book, name)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
            raise_exceptions=False,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--book", default="system")
    parser.add_argument("--server", required=True)
    args = parser.parse_args()
    asyncio.run(_run(args.book, args.server))


if __name__ == "__main__":
    main()
