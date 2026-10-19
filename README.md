# lm-gateway

> Use your Microsoft 365 Copilot subscription from any OpenAI- or Anthropic-compatible tool, through a local HTTP gateway.

`lm-gateway` runs on your machine and exposes Microsoft 365 Copilot as standard HTTP endpoints (`/v1/chat/completions`, `/v1/responses`, `/v1/messages`). Point your editor, CLI, or script at `http://127.0.0.1:8000/v1` and it talks to Copilot.

- **No Azure app registration and no admin consent.** It uses your normal signed-in Copilot browser session.
- **Local only.** The server binds to `127.0.0.1` by default.
- **Token handling is automatic.** The short-lived access token is captured from a dedicated browser profile and refreshed in the background.
- **Multi-turn memory is optional.** Stateless by default; enable persistent conversations per request.

> **Unofficial.** This project calls the browser-facing Copilot API, not a public SDK. It can break when Microsoft changes that API. Check your organization's policy before using it.

## How it works

```text
your client ──HTTP──▶ lm-gateway (127.0.0.1:8000) ──WebSocket──▶ Copilot (substrate.office.com)
                                  ▲
                                  └── token read from a dedicated Edge/Chrome profile
```

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (`brew install uv` on macOS)
- Microsoft Edge or Google Chrome, installed in `/Applications` (macOS) or Program Files (Windows)
- A Microsoft 365 account with Copilot access

## Quick start

```bash
git clone https://github.com/carmelobattiato/Proxy-m365-copilot-agents.git
cd Proxy-m365-copilot-agents
./launch.sh
```

`launch.sh` installs dependencies, opens a dedicated browser window, and starts the gateway.

1. On the first run, sign in to Microsoft 365 Copilot in the window that opens. You only need to do this once; the profile is kept.
2. If the terminal says it is waiting for a token, click the chat input box in that window and type one character. You do not need to send it.
3. When you see `Uvicorn running on http://127.0.0.1:8000`, the gateway is ready.

Equivalent without the script:

```bash
uv sync
uv run lm-gateway serve
```

## Try it

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "lm-gateway",
    "messages": [{"role": "user", "content": "Say hello in one short sentence."}]
  }'
```

Check the token status at any time:

```bash
curl http://127.0.0.1:8000/healthz
```

## Connect a client

Every OpenAI-compatible client uses the same three settings:

| Setting | Value |
|---|---|
| Base URL | `http://127.0.0.1:8000/v1` |
| API key | `dummy` (any non-empty string) |
| Model | `lm-gateway` |

### Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="dummy")

reply = client.chat.completions.create(
    model="lm-gateway:sonnet5.5",
    messages=[{"role": "user", "content": "Hello"}],
)
print(reply.choices[0].message.content)
```

### Continue (VS Code / JetBrains)

Add to `~/.continue/config.json`:

```json
{
  "models": [
    {
      "title": "M365 Copilot",
      "provider": "openai",
      "model": "lm-gateway:persist",
      "apiBase": "http://127.0.0.1:8000/v1",
      "apiKey": "dummy"
    }
  ]
}
```

### OpenCode

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8000
export OPENAI_API_KEY=dummy
opencode
```

Select **OpenAI API** as the provider and use the model `lm-gateway`.

### Claude Code

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_API_KEY=dummy
claude
```

Claude Code relies heavily on native tool use. Copilot answers through emulated tool calls (see below), so agentic features such as file editing and shell commands are unreliable through this gateway. Use it for chat-style questions.

## Model names

The model name selects the Copilot model and options. Options are colon-separated and can be combined.

| Model name | Copilot model | Notes |
|---|---|---|
| `lm-gateway` | Claude Sonnet 5.5 (default) | Stateless |
| `lm-gateway:sonnet5.5` | Claude Sonnet 5.5 | |
| `lm-gateway:sonnet5` | Claude Sonnet 5 | |
| `lm-gateway:auto` | Copilot automatic picker | |
| `lm-gateway:persist` | Default model | Multi-turn memory |
| `lm-gateway:sonnet5.5:persist` | Sonnet 5.5 | Multi-turn memory |
| `lm-gateway:fresh` | Default model | New conversation, bypasses incognito mode |

`GET /v1/models` returns the full list.

## Conversation modes

By default, each request is an *incognito* conversation: the gateway does not reuse upstream history and Copilot's chat history is not filled with test requests.

You can change that per request, in this order of precedence:

1. **Header `X-LMG-Session-Id: <name>`**: named persistent session. Use one name per workspace or agent, so sessions do not mix.
2. **Model suffix `:persist`**: persistent session. On `/v1/chat/completions` it is keyed by the request's `user` field; without it, or on `/v1/responses` and `/v1/messages`, all such requests share one default session.
3. **Model suffix `:fresh`**: new conversation, even when incognito is on.
4. **Default**: incognito (`LMG_INCOGNITO=true`).

Example with a named session:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-LMG-Session-Id: refactor-auth" \
  -d '{"model": "lm-gateway", "messages": [{"role": "user", "content": "Remember the code word: sakura. Reply only OK."}]}'
```

## Tool calls

OpenAI `tools` are supported through **emulation**. Copilot has no native tool API, so the gateway adds instructions to the prompt, asks the model to answer with `<tool_call>` blocks, and converts those blocks into OpenAI `tool_calls`.

- Reliability depends on the model's output. Simple single-tool calls work best.
- When `tools` are present, the full answer is buffered before it streams.
- Disable emulation with `LMG_TOOL_EMULATION=false`. `tools` are then ignored and the request becomes plain chat.

## API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | Service health and token status |
| `GET` | `/v1/token/status` | Token validity, expiry time, seconds remaining |
| `GET` | `/v1/models` | Model names advertised to clients |
| `POST` | `/v1/chat/completions` | OpenAI Chat Completions (streaming supported) |
| `POST` | `/v1/responses` | OpenAI Responses API (streaming supported) |
| `POST` | `/v1/messages` | Anthropic Messages API (streaming supported) |

Interactive docs are served at `http://127.0.0.1:8000/docs`.

## Token management

The Copilot access token expires after roughly one hour. The gateway handles this:

- **Startup:** if `.env` has no valid token, it reads one from the dedicated browser profile.
- **Background refresh:** a loop refreshes the token about 5 minutes before it expires. It needs the browser window to stay open.
- **Manual fallback:** if automatic capture fails, copy the Substrate WebSocket URL from DevTools (Network tab, filter `substrate`, Headers, Request URL) and run:

  ```bash
  uv run lm-gateway set-token
  ```

  Paste the URL when prompted. The token is extracted and written to `.env`.

## Command-line reference

```text
uv run lm-gateway serve [options]     Start the gateway (default)
uv run lm-gateway set-token           Paste a token URL manually
uv run lm-gateway capture-token       Capture a token from the running debug browser
uv run lm-gateway launch-browser      Open the dedicated browser profile
```

`serve` options:

| Option | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8000` | HTTP port |
| `--cdp-port` | `9222` | Remote-debugging port of the browser |
| `--no-launch-browser` | off | Do not open the browser on start |
| `--no-capture-on-start` | off | Do not capture a token on start |
| `--no-auto-refresh` | off | Disable background token refresh |
| `--capture-timeout` | `180` | Seconds to wait for a token on start |
| `--refresh-before` | `300` | Refresh this many seconds before expiry |
| `--refresh-retry` | `60` | Seconds between refresh retries |

`launch.sh` reads `LMG_CDP_PORT` (default `9222`) and `LMG_SERVER_PORT` (default `8000`) from the environment.

## Configuration

Settings come from environment variables or from `.env` in the project directory. Prefix: `LMG_`.

| Variable | Default | Description |
|---|---|---|
| `LMG_ACCESS_TOKEN` | *(empty)* | Copilot access token. Written automatically by the capture flow. |
| `LMG_TONE` | `Claude_Sonnet` | Copilot model: `Claude_Sonnet` (Sonnet 5.5), `Claude_Sonnet_5`, or `Magic` (automatic). |
| `LMG_TIME_ZONE` | `Europe/Rome` | Time zone sent to Copilot. |
| `LMG_MODEL_ALIAS` | `lm-gateway` | Base model name returned by `/v1/models`. |
| `LMG_INCOGNITO` | `true` | Use incognito conversations for stateless requests. |
| `LMG_TOOL_EMULATION` | `true` | Emulate OpenAI tool calls through the prompt. |
| `LMG_WS_TIMEOUT_SECONDS` | `30` | Timeout for each upstream turn. |

## Security

- The server listens on `127.0.0.1`. Do not expose it to a network without a reverse proxy and authentication. Anyone who can reach the port can use your Copilot session.
- `LMG_ACCESS_TOKEN` in `.env` is a bearer token for your account. Treat `.env` as a secret. It is git-ignored.
- The browser profile lives in `~/.lm-gateway/browser-profile`. It holds your Microsoft login. Delete it to sign out.
- Requests go only to Microsoft 365 Copilot (`substrate.office.com`). Nothing is sent elsewhere.

## Development

```bash
uv sync
uv run pytest
```

Layout:

```text
src/lmgateway/
  server.py       FastAPI app and endpoints
  adapter.py      OpenAI/Anthropic request translation
  upstream.py     Copilot WebSocket client
  sessions.py     Incognito and persistent session handling
  model_opts.py   Model-name option parsing
  tools.py        Tool-call emulation
  auth.py         Token decoding and validation
  cli.py          lm-gateway command
scripts/capture.py  Standalone token capture helper
tests/              Unit and server tests
```

## Limitations

- Unofficial. The upstream API is not documented and may change without notice.
- Copilot has no native tool API; tool calls are emulated and less reliable than native function calling.
- Token capture needs a signed-in browser profile and an open Copilot tab.
- Token usage fields in responses are placeholders.
- Conversation history is translated to plain text context for stateless requests.

## License

Apache License 2.0. See [LICENSE](LICENSE).
