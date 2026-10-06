# tiny-llm-gateway

A small, OpenAI-compatible LLM gateway for organisations that run open-weight models on their own hardware and want *controlled* access to cloud models on top.

- **One API for all models.** Clients use the standard OpenAI API (including streaming) and pick a model by alias. The gateway routes each request to a self-hosted backend (e.g. vLLM) or an external provider (e.g. OpenAI).
- **Automatic fallback.** Each model alias has an ordered list of deployments. If one fails, the gateway tries the next.
- **External access is opt-in per API key.** Prompts sent to a cloud provider leave the organisation, so only keys with `allow_external` may reach external backends, directly or as a fallback.

> Status: work in progress. The gateway core works; Kubernetes deployment, usage accounting and metrics are being added.

## How it works

```mermaid
flowchart LR
    C[Client<br/>OpenAI SDK, curl, chat UI] -->|Bearer API key| G[tiny-llm-gateway]
    G -->|local, preferred| V[vLLM<br/>Qwen3]
    G -->|external, if the key allows it| O[OpenAI]
    G --- DB[(Database<br/>API keys)]
```

A request for a model alias goes through three steps:

1. **Policy.** The gateway resolves the alias to its deployments. For a key without `allow_external`, external deployments are removed. If none are left, the request fails with `403 external_models_not_allowed`.
2. **Ordering.** Deployments that failed recently are in *cooldown* and move to the end of the list, so healthy ones are tried first. They aren't dropped, so there's always a last resort.
3. **Fallback.** Deployments are tried in order. The gateway moves on to the next one on connection errors, timeouts, HTTP 429 and 5xx. Other 4xx errors are returned to the client as-is, since a malformed request would fail everywhere. If every deployment fails, the client gets `503 no_backend_available`.

Every response carries two headers:

| Header | Meaning |
|---|---|
| `Gateway-Backend` | The backend that served the request, e.g. `vllm-local` or `openai` |
| `Gateway-Attempts` | How many deployments were tried; `2` means one fallback happened |

The response body stays exactly what the backend returned, so strict OpenAI clients keep working. The `model` field in the body shows the upstream model that actually answered.

### Streaming and fallback

For `stream: true`, the gateway waits for the backend's **first chunk** before it commits to that backend. A backend that refuses the connection, returns an error status, or dies before sending anything is skipped like in the non-streaming case.

Once the first bytes have reached the client, the response belongs to that backend. If it breaks mid-stream, the gateway ends the stream with an OpenAI-style error event (`stream_interrupted`) followed by `data: [DONE]`. Switching backends silently at that point would hand the client two half-answers glued together.

Ways to lift this limitation, each with a cost:

| Approach | Trade-off |
|---|---|
| Hold back the first N tokens before forwarding | Catches early failures, but delays the first visible token |
| Buffer the whole response | No mid-stream failures at all, but defeats the point of streaming |
| Resume on the next backend: send the partial answer as an assistant prefix and let it continue | vLLM supports this (`continue_final_message`), OpenAI's Chat API does not; the style can shift between models |
| Let the client retry | Pushes the complexity onto every client |

In practice, the most common cause of broken streams in Kubernetes is pods being stopped during rollouts or scale-down. Graceful termination, so running streams can finish, avoids most of them.

## Configuration

### Routes

Backends and model aliases are defined in a YAML file. [`gateway/routes.dev.yaml`](gateway/routes.dev.yaml) is a complete example:

```yaml
backends:
  local:
    base_url: http://127.0.0.1:9000/v1     # any OpenAI-compatible server
  openai:
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY            # name of the env var holding the key
    external: true                         # only keys with allow_external may use it

models:
  qwen3:                                   # alias that clients request
    - backend: local                       # tried first
      model: Qwen/Qwen3-0.6B               # model name sent to the backend
    - backend: openai                      # fallback
      model: gpt-4.1-mini
  gpt-4.1-mini:                            # external model, requestable directly
    - backend: openai
      model: gpt-4.1-mini
```

Notes:
- If the variable named in `api_key_env` isn't set, that backend is disabled at startup with a warning. Model aliases left without deployments are disabled too, so a setup without cloud credentials still serves its local models.
- A deployment can set `default_params`, which are merged *under* the client's request body. Use this for backend-specific parameters that other providers would reject. For example, this turns off Qwen3's thinking mode on vLLM without breaking the fallback to OpenAI:

  ```yaml
  - backend: local
    model: Qwen/Qwen3-0.6B
    default_params:
      chat_template_kwargs: {enable_thinking: false}
  ```

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `GATEWAY_MASTER_KEY` | (required) | Secret for the `/admin` endpoints |
| `GATEWAY_DATABASE_URL` | `sqlite+aiosqlite:///./gateway.db` | SQLAlchemy URL, e.g. `postgresql+asyncpg://user:pw@host/db` |
| `GATEWAY_ROUTES_FILE` | `routes.yaml` | Path to the routes YAML |
| `GATEWAY_CONNECT_TIMEOUT` | `3.0` | Seconds to connect to a backend. Keep it short so fallback is fast. |
| `GATEWAY_READ_TIMEOUT` | `300.0` | Seconds to wait for data from a backend |
| `GATEWAY_COOLDOWN_SECONDS` | `30.0` | How long a failed deployment moves to the end of the list |

## API

| Endpoint | Auth | Description |
|---|---|---|
| `POST /v1/chat/completions` | API key | OpenAI Chat Completions, with and without `stream` |
| `GET /v1/models` | API key | The model aliases this key may use |
| `POST /admin/keys` | Master key | Create a key: `{"alias": "team-a", "allow_external": true}`. The secret is returned only once. |
| `GET /admin/keys` | Master key | List keys (without secrets) |
| `PATCH /admin/keys/{id}` | Master key | Change `allow_external` or `active` |
| `GET /healthz` | none | Liveness |
| `GET /readyz` | none | Readiness (database reachable) |

API keys are stored as SHA-256 hashes. Errors use OpenAI's format, `{"error": {"message", "type", "code"}}`, so SDKs surface them properly.

## Local development

You need [uv](https://docs.astral.sh/uv/). It installs the right Python version (3.14) on its own.

```bash
cd gateway
uv sync
```

**1. Start the mock backend.** It's an OpenAI-compatible fake that echoes the prompt. It stands in for vLLM, so no GPU or model download is needed.

```bash
uv run uvicorn app.mock_upstream:app --port 9000
```

You can make it flaky with `MOCK_FAILURE_RATE=0.5`, or slower with `MOCK_TOKEN_DELAY=0.2` (seconds between streamed tokens).

**2. Start the gateway** in a second terminal. It uses a local SQLite file by default.

```bash
export GATEWAY_MASTER_KEY=dev
export GATEWAY_ROUTES_FILE=routes.dev.yaml
export OPENAI_API_KEY=sk-...      # optional; without it, only local models are served
uv run uvicorn app.main:create_app --factory --port 8080 --reload
```

On Windows PowerShell, set the variables with `$env:GATEWAY_MASTER_KEY = "dev"` and so on.

**3. Create an API key and send requests** (the first command uses [jq](https://jqlang.org/)):

```bash
KEY=$(curl -s -X POST localhost:8080/admin/keys \
  -H 'Authorization: Bearer dev' -H 'Content-Type: application/json' \
  -d '{"alias": "me", "allow_external": true}' | jq -r .key)

curl -i localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model": "qwen3", "messages": [{"role": "user", "content": "Hello"}]}'

curl -N localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model": "qwen3", "stream": true, "messages": [{"role": "user", "content": "Hello"}]}'
```

To see the fallback, stop the mock backend and repeat the request. With `OPENAI_API_KEY` set, the answer now comes from OpenAI (`Gateway-Backend: openai`, `Gateway-Attempts: 2`). A key created with `"allow_external": false` gets `503` instead.

Any OpenAI SDK works as a client:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8080/v1", api_key=KEY)
reply = client.chat.completions.create(
    model="qwen3", messages=[{"role": "user", "content": "Hello"}]
)
```

**Tests and linting:**

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The tests need no running services: backends are faked with `httpx2.MockTransport` and the database is a temporary SQLite file. They cover the access policy, fallback on each kind of failure, cooldown ordering, streaming (including fallback before the first chunk and mid-stream failures), auth and config loading.

## Project layout

```
gateway/
  app/
    main.py            FastAPI app and OpenAI-compatible endpoints
    routing.py         policy filter, fallback, cooldown, streaming relay
    upstream.py        requests to OpenAI-compatible backends
    config.py          settings (env) and routes (YAML)
    auth.py, admin.py  API keys and admin endpoints
    db.py, models.py   database setup and tables
    mock_upstream.py   fake OpenAI-compatible backend for development
  tests/               pytest suite with fake upstreams
  routes.dev.yaml      routes for local development
```
