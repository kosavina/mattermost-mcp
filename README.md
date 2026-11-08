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

## Run it in Docker

Build the image and run it with the same restrictions CI's smoke test uses (read-only file
system, no Linux capabilities, published on this machine only):

```sh
sudo docker build --tag mattermost-mcp:dev .
sudo docker run --rm --name mattermost-mcp --read-only --tmpfs /tmp \
  --tmpfs /data:uid=10001,mode=700 --cap-drop ALL \
  --security-opt no-new-privileges:true -p 127.0.0.1:8000:8000 mattermost-mcp:dev
```

Then open <http://127.0.0.1:8000/health>. It answers `{"status":"ok","commit":"dev"}`; images
built by CI report the commit they were built from. The MCP endpoint is
`http://127.0.0.1:8000/mcp`, as above.

## Events

The server implements [MCP Events](https://developers.openai.com/plugins/build/mcp-events) with
webhook delivery, for one event: `test.message`. It carries only text that the owner types on
the server, to try out delivery end to end. It has no filters.

There are no logins until a later milestone, so a stranger can subscribe too. These limits keep
the worst case harmless:

- Webhooks go only to `https` on port 443 at the hosts in `MCP_EVENTS_CALLBACK_HOSTS`
  (comma-separated, default `connectors.api.openai.com`).
- Every address the host resolves to must be public. The server connects to the address it
  checked, follows no redirects and uses no proxy. No setting switches this off.
- At most `MCP_EVENTS_MAX_SUBSCRIPTIONS` subscriptions exist in total (default 5), with at most
  10 verification attempts a minute.
- A subscription lives for 10 to 60 minutes, then has to be refreshed.
- Events are sent only by the command below, which needs SSH and `sudo` on the server. **Never
  type anything private: assume a stranger is subscribed.**

Subscriptions are kept in `$MCP_DATA_DIR/events.sqlite3` (the `app_data` volume at `/data` on the
server, `.data/` locally). The file holds the signing secrets and only the app user can read
it. The app refuses to start if it cannot write there.

On the server:

```sh
sudo docker exec mattermost-mcp-app-1 mattermost-mcp-events list
sudo docker exec mattermost-mcp-app-1 mattermost-mcp-events send-test "Hello from the server"
sudo docker exec mattermost-mcp-app-1 mattermost-mcp-events remove <id>
sudo docker exec mattermost-mcp-app-1 mattermost-mcp-events remove --all
```

`list` shows each subscription's id, callback host, URL fingerprint, age and last delivery status,
never its secret or full URL. `send-test` prints one line per subscription and exits with 1 if
there is no active subscription or a delivery failed.

## Deployment

The production stack lives in `deploy/`: `compose.yaml` runs the app behind Caddy, which
serves HTTPS for the host name in `MCP_PUBLIC_HOST` and is the only container with published
ports. CI builds an image for every commit and, on main, pushes it to
`ghcr.io/kosavina/mattermost-mcp` tagged with the full commit SHA. On the server,
`deploy/mcp-deploy` starts a given commit's image. An admin installs that script by hand to
`/usr/local/bin/mcp-deploy`: it is the only command the CI deploy key may run, so a pull
request cannot change it.

Every merge to main deploys itself. After `checks` and `image`, the run's `deploy` job (in the
`production` environment) connects to the server over SSH, sends the commit SHA to
`mcp-deploy`, and then waits until `https://<public host>/health` reports that commit. The
`deploy` job of a run shows whether, and when, its commit went live.

Caddy terminates HTTPS, so the app learns the real scheme and client address from Caddy's
`X-Forwarded-Proto` and `X-Forwarded-For` headers. It believes those headers only from the
addresses in `MCP_TRUSTED_PROXIES` (comma-separated addresses or CIDR ranges, default
`127.0.0.1`). `compose.yaml` sets it to the fixed subnet of the private `internal` network.
Never set it to `*`: anyone could then fake their address and pass the Origin check.

## Logs

Every event is one plain line on stdout, including exceptions: the line breaks of a traceback
are written as a literal `\n`. On the server:

```sh
sudo docker compose -p mattermost-mcp logs -f app
```

- Logger `mattermost_mcp.http` writes one line per HTTP request, when its response ends: method,
  path, status, duration, client address, scheme, the `Origin`, `User-Agent`,
  `MCP-Protocol-Version` and `Mcp-Method` headers, and whether `Mcp-Session-Id` is present. A
  long-lived GET stream therefore appears when it closes. Bodies, credentials and other headers
  are never logged. The one request left out is the container's own health check: a `GET /health`
  from `127.0.0.1` or `::1` that answers 200. Every other `/health` request is logged, including
  the deploy job's check through Caddy and any failing health check.
- Logger `fastmcp.middleware.logging` writes a start line and a result line for each JSON-RPC
  method, without payloads.
- Loggers `uvicorn` and `uvicorn.error` write the server's startup messages and unhandled
  exceptions. Logger `mcp` writes the MCP SDK's warnings and errors.

- Logger `mattermost_mcp.events` writes one line per subscribe, refresh, unsubscribe,
  verification, delivery attempt and refusal: the action, event, subscription id, callback host,
  a fingerprint of the URL, the requested and granted lifetime, the outcome, the status and the
  attempt. Secrets, challenges, full URLs and bodies are never logged. `send-test` runs in its
  own process, so its delivery lines appear on that command's stderr, not in the app log.

To roll back, open an older run on main in the Actions tab and re-run its `deploy` job: it
sends that run's commit SHA, so the server goes back to that commit and its image.
