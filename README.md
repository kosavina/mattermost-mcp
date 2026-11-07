# mattermost-mcp

A self-hosted Python MCP server that will turn Mattermost messages into ChatGPT events. So far
this repository holds a minimal MCP server built with [FastMCP 4](https://gofastmcp.com): one
`ping` tool, served over Streamable HTTP on this machine only, plus the tooling and CI. The
Mattermost code comes later.

## Run the server locally

```sh
uv run mattermost-mcp
```

The server listens on `http://127.0.0.1:8000/mcp` (this machine only). Its Host/Origin guard
rejects requests with a foreign `Host` or `Origin` header, which protects it from DNS
rebinding. Press Ctrl+C to stop it.

## Try it in MCP Inspector

With the server running, start the Inspector (needs Node `^22.23.4 || ^24.22.1 || >=26.12.1`):

```sh
npx -y @modelcontextprotocol/inspector@2.13.0 --web --transport http --server-url http://127.0.0.1:8000/mcp
```

1. Open the `http://127.0.0.1:6274?MCP_INSPECTOR_API_TOKEN=...` link that the Inspector prints.
   The token in it changes on every launch.
2. If the Inspector is not connected yet, choose the Streamable HTTP transport, enter
   `http://127.0.0.1:8000/mcp` and click Connect.
3. Open the Tools tab, select `ping` and run it. The result is `pong`.

Press Ctrl+C to stop the Inspector. To check the server from the command line instead:

```sh
npx -y @modelcontextprotocol/inspector@2.13.0 --cli --transport http \
  --server-url http://127.0.0.1:8000/mcp --method tools/call --tool-name ping
```

## Run the checks locally

Install the dependencies once with `uv sync`, then run the four checks:

```sh
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest
```
