"""Test the MCP server over stdio: initialize, list tools, call one."""
import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PY = str(Path(".venv/Scripts/python.exe").resolve())
SERVER = str(Path("mcp_server.py").resolve())


async def main():
    params = StdioServerParameters(command=PY, args=[SERVER], cwd=str(Path.cwd()))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("TOOLS:")
            for t in tools.tools:
                print(f"  - {t.name}: {(t.description or '')[:70]}")

            print("\nCALL list_courses:")
            res = await session.call_tool("list_courses", {})
            print(res.content[0].text[:400])

            print("\nCALL list_pending_tasks (first 300 chars):")
            res = await session.call_tool("list_pending_tasks", {})
            print(res.content[0].text[:300])


if __name__ == "__main__":
    asyncio.run(main())
