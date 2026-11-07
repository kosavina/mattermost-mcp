import pytest
from fastmcp import Client

from mattermost_mcp.server import mcp

pytestmark = pytest.mark.anyio


async def test_ping_is_listed() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()

    assert [tool.name for tool in tools] == ["ping"]


async def test_ping_returns_pong() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("ping")

    assert not result.is_error
    assert result.data == "pong"
