import pytest
from fastmcp import Client

from mattermost_mcp.server import mcp

pytestmark = pytest.mark.anyio


async def test_ping_is_listed() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()

    assert [tool.name for tool in tools] == ["ping"]


async def test_ping_declares_title_and_hints() -> None:
    async with Client(mcp) as client:
        [tool] = await client.list_tools()

    assert tool.title == "Ping"
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is False


async def test_ping_returns_pong() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("ping")

    assert not result.is_error
    assert result.data == "pong"
