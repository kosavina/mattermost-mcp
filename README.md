# mattermost-mcp

A self-hosted Python MCP server that will turn Mattermost messages into ChatGPT events. So far
this repository holds only the project skeleton, the tooling and CI; the MCP code comes later.

## Run the checks locally

Install the dependencies once with `uv sync`, then run the four checks:

```sh
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest
```
