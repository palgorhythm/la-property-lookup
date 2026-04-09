#!/usr/bin/env python3
"""
LA Property Lookup MCP Server

Exposes a single tool: lookup_property(address)
Returns a full markdown report from ZIMAS + LADBS.

Connect to Claude desktop via ~/.claude/claude_desktop_config.json:

  {
    "mcpServers": {
      "la-property-lookup": {
        "command": "python",
        "args": ["/path/to/la-property-lookup/server.py"]
      }
    }
  }

Then in Claude you can say:
  "Look up 1923 Preston Ave"
  "Look up 5 addresses and save each as a new page under the Home Notion doc"
"""

import asyncio
import sys
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Import the lookup functions directly
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from lookup import lookup_zimas, lookup_ladbs, format_markdown

app = Server("la-property-lookup")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="lookup_property",
            description=(
                "Look up an LA property address on ZIMAS and LADBS. "
                "Returns a full markdown report covering zoning, assessor data, "
                "hazards, permits, code enforcement, and certificate of occupancy."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Street address, e.g. '1923 Preston Ave' or '1815 Park Dr'",
                    }
                },
                "required": ["address"],
            },
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "lookup_property":
        raise ValueError(f"Unknown tool: {name}")

    address = arguments["address"]

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        zimas_page = await context.new_page()
        zimas_result = await lookup_zimas(zimas_page, address)
        await zimas_page.close()

        ladbs_page = await context.new_page()
        ladbs_result = await lookup_ladbs(ladbs_page, address)
        await ladbs_page.close()

        await browser.close()

    report = format_markdown(zimas_result, ladbs_result)
    return [TextContent(type="text", text=report)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
