# tiny-llm-gateway

A small, OpenAI-compatible LLM gateway for organisations that run open-weight models on their own hardware and want *controlled* access to cloud models on top.

- **One API for all models.** Clients use the standard OpenAI API (including streaming) and pick a model by alias. The gateway routes each request to a self-hosted backend (e.g. vLLM) or an external provider (e.g. OpenAI).
- **Automatic fallback.** Each model alias has an ordered list of deployments. If one fails, the gateway tries the next.
- **External access is opt-in per API key.** Prompts sent to a cloud provider leave the organisation, so only keys with `allow_external` may reach external backends, directly or as a fallback.
- **Accounting and monitoring.** Every request is logged with its token counts per API key, and Prometheus metrics show traffic, latency, fallbacks and backend health.

## How it works

```mermaid
flowchart LR
    C[Client<br/>OpenAI SDK, curl, chat UI] -->|Bearer API key| G[tiny-llm-gateway]
    G -->|local, preferred| V[vLLM<br/>Qwen3]
    G -->|external, if the key allows it| O[OpenAI]
    G --- DB[(Database<br/>API keys, usage)]
    P[Prometheus] -.->|scrapes /metrics| G
    P -.-> V
```

A request for a model alias goes through three steps:

1. **Policy.** The gateway resolves the alias to its deployments. For a key without `allow_external`, external deployments are removed. If none are left, the request fails with `403 external_models_not_allowed`.
2. **Ordering.** Deployments that failed recently are in *cooldown* and move to the end of the list, so healthy ones are tried first. They aren't dropped, so there's always a last resort.
3. **Fallback.** Deployments are tried in order. The gateway moves on to the next one on connection errors, timeouts, HTTP 429 and 5xx. Other 4xx errors are returned to the client as-is, since a malformed request would fail everywhere. If every deployment fails, the client gets `503 no_backend_available`.

Every response carries two headers:

| Header | Meaning |
|---|---|
| `Gateway-Backend` | The backend that served the request, e.g. `local` or `openai` |
| `Gateway-Attempts` | How many deployments were tried; `2` means one fallback happened |

The response body stays exactly what the backend returned, so strict OpenAI clients keep working. The `model` field in the body shows the upstream model that actually answered.

### Streaming and fallback

For `stream: true`, the gateway waits for the backend's **first chunk** before it commits to that backend. A backend that refuses the connection, returns an error status, or dies before sending anything is skipped like in the non-streaming case.

Once the first bytes have reached the client, the response belongs to that backend. If it breaks mid-stream, the gateway ends the stream with an OpenAI-style error event (`stream_interrupted`) followed by `data: [DONE]`. Switching backends silently at that point would hand the client two half-answers glued together. 

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
| `GET /admin/usage` | Master key | Usage report; see [Usage accounting](#usage-accounting) |
| `GET /healthz` | none | Liveness |
| `GET /readyz` | none | Readiness (database reachable) |
| `GET /metrics` | none | Prometheus metrics; see [Metrics](#metrics) |

API keys are stored as SHA-256 hashes. Errors use OpenAI's format, `{"error": {"message", "type", "code"}}`, so SDKs surface them properly.

## Usage accounting

The gateway stores one row per chat completion request in the `usage_log` table. Each row records:
- the API key and requested model alias
- the backend and upstream model that answered
- the outcome and HTTP status
- the number of attempts and whether a fallback happened
- prompt and completion tokens
- latency, and time to first byte for streams

Rejected requests (403, 503) are recorded too, so the log doubles as an audit trail.

`GET /admin/usage` aggregates the log. `group_by` is `key` (default), `model` or `backend`; the optional `since` (ISO 8601, UTC if no offset is given) limits the time range:

```bash
curl -s "localhost:8080/admin/usage?group_by=key&since=2026-10-01T00:00:00Z" \
  -H "Authorization: Bearer $MASTER_KEY"
```

```json
{
  "group_by": "key",
  "since": "2026-10-01T00:00:00Z",
  "data": [
    {"key": "team-a", "requests": 4, "errors": 0, "fallbacks": 1, "prompt_tokens": 86,
     "completion_tokens": 115, "total_tokens": 201, "avg_latency_ms": 3008}
  ]
}
```

`errors` counts requests that failed because of the request, the policy or the backends. Clients that disconnect mid-stream are logged but not counted as errors.

**Token counts come from the backends.** For non-streaming requests, the gateway reads the `usage` field of the response.

Streams carry no usage by default, so the gateway asks every backend for a final usage chunk (`stream_options.include_usage`, supported by OpenAI and vLLM). It reads the counts as the stream passes through and removes that chunk again, unless the client asked for it itself, so clients receive exactly the stream they requested. A stream that breaks mid-way has no token counts.

## Metrics

`GET /metrics` exposes these metrics in the Prometheus format:

| Metric | Labels | Meaning |
|---|---|---|
| `gateway_requests_total` | `model`, `backend`, `status` | Requests by model alias, the backend that answered (`none` if none did) and outcome |
| `gateway_request_duration_seconds` | `model`, `backend` | Histogram of the time until the response (or stream) is complete |
| `gateway_time_to_first_byte_seconds` | `model`, `backend` | Streams only: histogram of the time until the first chunk from the backend |
| `gateway_tokens_total` | `model`, `backend`, `type` | Prompt and completion tokens, as reported by the backends |
| `gateway_fallbacks_total` | `model`, `from_backend`, `to_backend` | Failed attempts that made the gateway move on, and where the request ended up |
| `gateway_policy_denials_total` | `model` | Requests rejected because the key may not use external models |
| `gateway_deployment_cooldown` | `backend`, `upstream_model` | `1` while a deployment is in cooldown after a failure, else `0` |

Each gateway replica counts its own requests, so queries sum over all of them. Some useful ones:

```promql
# Share of requests that needed a fallback, over the last 5 minutes
sum(rate(gateway_fallbacks_total{to_backend!="none"}[5m])) / sum(rate(gateway_requests_total[5m]))

# 95th percentile time to first byte per backend
histogram_quantile(0.95, sum by (le, backend) (rate(gateway_time_to_first_byte_seconds_bucket[5m])))

# Completion tokens per second, local vs. external
sum by (backend) (rate(gateway_tokens_total{type="completion"}[5m]))
```

**Metrics have no API key label.** Labels only take values from the routes configuration, so the number of time series stays bounded no matter how many keys or clients there are. Per-key numbers come from the [usage report](#usage-accounting) instead. Requests for unknown model aliases are counted under `model="unknown"`, so clients can't create new series either.

`/metrics` needs no authentication, like most Prometheus endpoints. It shows no secrets or key names, but if the gateway is exposed outside the cluster, block `/metrics` at the ingress.

## Running on Kubernetes

The Helm chart in [`chart/`](chart/) deploys the gateway (2 replicas), vLLM serving Qwen3-0.6B on CPU, PostgreSQL for the API keys and usage log, and Prometheus. These instructions use [minikube](https://minikube.sigs.k8s.io/) and a bash shell; on Windows, use WSL 2. They work on x86-64 (AVX2 or AVX-512) as well as ARM64.

You need minikube, [Helm](https://helm.sh/) and kubectl. With the Docker driver, give Docker at least 14 GB of memory (Docker Desktop: *Settings → Resources*; with WSL 2, set `memory=` in `.wslconfig`).

**1. Start a cluster and build the gateway image into it:**

```bash
minikube start --cpus=6 --memory=12g
minikube image build -t tiny-llm-gateway:0.1.0 gateway/
```

minikube runs its own container runtime, so images built with plain `docker build` aren't visible to it. `minikube image build` builds directly inside the cluster.

**2. Optional: store your OpenAI key** as a Kubernetes Secret. Without it, the gateway only serves local models.

```bash
kubectl create secret generic openai --from-literal=api-key=sk-...
```

**3. Install the chart:**

```bash
helm install tlg chart --set openai.existingSecret=openai   # drop --set without an OpenAI key
kubectl rollout status deploy/tlg-vllm --timeout=20m
```

On its first start, vLLM downloads the model (about 1.4 GB) into a persistent volume and compiles it for the CPU; the vLLM image itself is about 1.6 GB on x86-64. Expect several minutes before the rollout finishes. Restarts reuse the downloaded model and the compiled code.

For a quick setup without vLLM, use the lightweight variant instead. A mock backend replaces vLLM, and the whole release needs well under 1 GB of memory:

```bash
helm install tlg chart -f chart/values-mock.yaml --set openai.existingSecret=openai
```

**4. Talk to the gateway.** Forward its port and read the generated master key:

```bash
kubectl port-forward svc/tlg-gateway 8080:8080   # keep running in a separate terminal
MASTER_KEY=$(kubectl get secret tlg -o jsonpath='{.data.master-key}' | base64 -d)
```

From here on, the requests are the same as in [Local development](#local-development), step 3, using `$MASTER_KEY` instead of `dev`.

To look at the metrics, forward Prometheus too and open http://localhost:9090:

```bash
kubectl port-forward svc/tlg-prometheus 9090:9090
```

**5. Run the built-in test.** It creates a temporary key, sends one request through the gateway and deactivates the key again:

```bash
helm test tlg --logs
```

### Guided demo

[`scripts/demo.sh`](scripts/demo.sh) runs all of the above and then walks through the gateway's features step by step:

1. Starts minikube if needed, builds the image and installs the chart
2. Creates two API keys: `team-a` may use external models, `team-b` may not
3. `team-a` asks the local model, once normally and once streamed, then the external model
4. `team-b` asks for the external model and gets `403`
5. The local model server is scaled to 0: `team-a` falls back to OpenAI, `team-b` gets `503`, because its prompts must not leave the cluster
6. Shows the usage report of the run, per key and per backend
7. Queries Prometheus for the same requests, the fallbacks and policy denials, and which scrape targets are up

```bash
export OPENAI_API_KEY=sk-...   # optional; without it, the external steps are skipped
scripts/demo.sh                # or: scripts/demo.sh --mock  (no vLLM)
```

The script needs `minikube`, `kubectl`, `helm`, `curl` and `jq`. It pauses between steps; set `DEMO_PAUSE=0` to run it straight through, and `DEMO_PORT` or `DEMO_PROM_PORT` if port 8080 or 9090 is taken. Re-running it is safe. Abbreviated output of the `team-a` step:

```
=== 4. team-a: local model, streamed local model, external model ===
HTTP 200   Gateway-Backend: local   Gateway-Attempts: 1
  [Qwen/Qwen3-0.6B] An LLM gateway is a platform that enables users to interact with large language models ...
```

### Chart configuration

The most important values; see [`chart/values.yaml`](chart/values.yaml) for all of them:

| Value | Default | Meaning |
|---|---|---|
| `localBackend` | `vllm` | Model server behind the `local` backend: `vllm` or `mock` (fake backend, see `values-mock.yaml`) |
| `routes` | local backend, then OpenAI | Rendered into the gateway's routes file; see [Routes](#routes). Values may use templates. |
| `openai.existingSecret` | `""` | Secret holding the OpenAI key, under `openai.existingSecretKey` (`api-key`) |
| `gateway.replicas` | `2` | Gateway pods |
| `gateway.masterKey` | generated | Admin secret; generated on install and kept on upgrades |
| `vllm.model` | `Qwen/Qwen3-0.6B` | Any model from Hugging Face that fits into memory |
| `vllm.dtype` | `float32` | `bfloat16` is faster on CPUs with native BF16 support (e.g. recent Xeons), but very slow elsewhere |
| `vllm.cpuThreads` | `4` | Inference threads; keep at or below the CPU request |
| `vllm.resources` | 4 CPU, 5–8 Gi | Requests and limits of the vLLM pod |
| `postgres.enabled` | `true` | Deploy PostgreSQL. Set to `false` and set `externalDatabase.url` to use a managed database. |
| `prometheus.enabled` | `true` | Deploy Prometheus. To use an existing one instead, set to `false` and scrape the headless Service `tlg-gateway-pods` on port 8080. |

Design notes on the chart:

- **Graceful shutdown.** Gateway and vLLM pods wait briefly before stopping, so the Service stops sending them new requests, and running streams can finish. The gateway allows 30 s for that, vLLM 60 s.
- **PostgreSQL** runs as a single-replica StatefulSet on the official image. That's plenty for API keys and the usage log. For production, use a managed database or an operator like CloudNativePG.
- **`helm uninstall` keeps the data.** Kubernetes keeps the database volume, with all API keys and usage data, and the chart keeps the release's Secret with the generated passwords to match. A reinstall picks up where the last install left off. For a clean slate, delete both after uninstalling: `kubectl delete pvc data-tlg-postgres-0 && kubectl delete secret tlg`.
- **Several gateway replicas create the schema concurrently** on first start. A Postgres advisory lock serialises that.
- **Prometheus finds each gateway replica** through a headless Service, whose DNS name resolves to all ready pods. Scraping the normal Service would hit a random replica each time. Prometheus stores its data in an `emptyDir`, so it is lost when its pod is replaced.
- **The vLLM cache volume is kept** when switching to the mock backend or uninstalling, so the model isn't downloaded again. To free the space: `kubectl delete pvc tlg-vllm-cache`.

### Troubleshooting

| Symptom | Fix |
|---|---|
| vLLM pod `Pending` | Not enough memory or CPU in the cluster. Check `kubectl describe pod -l app.kubernetes.io/component=vllm`, then restart minikube with more resources or lower `vllm.resources`. |
| vLLM pod `OOMKilled` | Raise `vllm.resources.limits.memory`, or lower `vllm.kvCacheSpaceGiB` / `vllm.maxModelLen` |
| vLLM restarts during startup | The model download takes longer than `vllm.startupTimeoutSeconds` (default 20 min); raise it |
| vLLM logs `No available shared memory broadcast block found` for many minutes | The CPU lacks fast paths for the chosen dtype (typically `bfloat16`) or is oversubscribed. Use `vllm.dtype=float32` and keep `vllm.cpuThreads` at or below the CPUs available to minikube. |
| Gateway answers `503 no_backend_available` | vLLM isn't ready yet, and the key may not use external models. Check `kubectl get pods`. |
| `helm upgrade` doesn't switch between the full and the mock variant | Helm reuses the previous values when an upgrade gets no `-f`/`--set`. Pass `--reset-values`. |
| Changed `postgres.password` has no effect | The password only applies when the volume is first initialised. To start fresh, uninstall and delete the PVC `data-tlg-postgres-0`. |
| Gateway logs `password authentication failed` | The database volume and the release's Secret don't match, e.g. because only one of them was deleted. Uninstall, then delete both (see the design notes above) and install again. |

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

The tests need no running services: backends are faked with `httpx2.MockTransport` and the database is a temporary SQLite file. They cover the access policy, fallback on each kind of failure, cooldown ordering, streaming (including fallback before the first chunk, mid-stream failures and events split across network reads), usage accounting, metrics, auth and config loading.

## Limitations

What a production deployment would still need:

- **More endpoints.** Only Chat Completions and the model list are implemented. Embeddings, the Responses API and file uploads are not.
- **Federated identity.** Clients authenticate with static API keys. In an organisation, keys would be issued to users or groups from an identity provider (OIDC/SAML), and `allow_external` would follow from group membership.
- **Budgets and rate limits.** The usage log records tokens per key, but nothing enforces a limit yet. Per-key limits across several replicas need shared state, e.g. in Redis.
- **Shared health state.** Each replica keeps its own cooldown list, so every replica has to discover a failed backend by itself. Active health checks or shared state would avoid that.
- **Mid-stream failover.** See [Streaming and fallback](#streaming-and-fallback).
- **Schema migrations.** Tables are created on startup; changing them later needs a migration tool like Alembic. The usage log also grows without bound and needs a retention policy.
- **Routing across sites.** Backends are configured statically. Routing between several clusters, e.g. by load or data-residency rules, and a registry where users add their own models are natural next steps.

## Project layout

```
chart/                 Helm chart (gateway, vLLM, mock backend, PostgreSQL, Prometheus)
  values.yaml          defaults: vLLM on CPU, then OpenAI
  values-mock.yaml     lightweight variant with the mock backend (localBackend: mock)
gateway/
  Dockerfile           one image for the gateway and the mock backend
  app/
    main.py            FastAPI app and OpenAI-compatible endpoints
    routing.py         policy filter, fallback, cooldown, streaming relay
    upstream.py        requests to OpenAI-compatible backends
    config.py          settings (env) and routes (YAML)
    auth.py, admin.py  API keys and admin endpoints
    usage.py           usage logging and the usage report
    metrics.py         Prometheus metrics
    db.py, models.py   database setup and tables
    mock_upstream.py   fake OpenAI-compatible backend for development
  tests/               pytest suite with fake upstreams
  routes.dev.yaml      routes for local development
scripts/
  demo.sh              guided end-to-end demo on minikube
```
