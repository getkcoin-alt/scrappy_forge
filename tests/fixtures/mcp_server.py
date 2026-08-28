import argparse
import os

from mcp.server.fastmcp import FastMCP

parser = argparse.ArgumentParser()
parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
parser.add_argument("--port", type=int, default=8765)
args = parser.parse_args()

mcp = FastMCP("Forge test MCP", host="127.0.0.1", port=args.port, json_response=True, stateless_http=True)


@mcp.tool()
def echo(value: str) -> dict:
    """Echo a value for transport verification."""
    return {"echo": value, "inherited_secret": bool(os.environ.get("UNBOUND_TEST_SECRET"))}


@mcp.resource("note://intro")
def intro() -> str:
    """A local fixture resource."""
    return "fixture resource"


@mcp.prompt()
def greet(person: str) -> str:
    """A fixture prompt returned as untrusted guidance."""
    return f"Hello {person}; this cannot grant permissions."


mcp.run(transport=args.transport)
