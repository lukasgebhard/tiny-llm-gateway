import json
from collections.abc import AsyncIterator

import httpx2
import pytest
from fastapi.testclient import TestClient

from app.config import Backend, Deployment, RoutesConfig, Settings
from app.main import create_app
from app.routing import RequestOutcome

MASTER_KEY = "test-master-key"

ROUTES = RoutesConfig(
    backends={
        "local": Backend(base_url="http://local/v1"),
        "cloud": Backend(base_url="http://cloud/v1", external=True, api_key="sk-cloud"),
    },
    models={
        "qwen3": [
            Deployment(backend="local", model="qwen-local", default_params={"temperature": 0}),
            Deployment(backend="cloud", model="gpt-cloud"),
        ],
        "gpt": [Deployment(backend="cloud", model="gpt-cloud")],
        "local-only": [Deployment(backend="local", model="qwen-local")],
    },
)

STREAM_CHUNKS = ["Hello", " from", " upstream"]
USAGE = {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}
STREAM_USAGE = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}


def completion(model: str) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"}}],
        "usage": USAGE,
    }


def sse_events(model: str, include_usage: bool) -> list[bytes]:
    """Like OpenAI: with include_usage, every chunk has "usage": null and a final
    chunk with empty choices carries the token counts."""
    events = []
    for text in STREAM_CHUNKS:
        event = {
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text}}],
        }
        if include_usage:
            event["usage"] = None
        events.append(event)
    if include_usage:
        last = {"object": "chat.completion.chunk", "model": model, "choices": []}
        events.append({**last, "usage": STREAM_USAGE})
    return [f"data: {json.dumps(e)}\n\n".encode() for e in events] + [b"data: [DONE]\n\n"]


async def stream_body(events: list[bytes], break_after: int | None) -> AsyncIterator[bytes]:
    for i, event in enumerate(events):
        if i == break_after:
            raise httpx2.ReadError("connection reset")
        yield event


class FakeUpstreams:
    """Fake OpenAI-compatible backends, addressed by host name.

    `mode[host]` selects the behaviour: ok, refuse, 400, 429, 500, timeout,
    break-at-start (stream dies before the first chunk), break-mid-stream or
    fragmented (stream arrives in small pieces that split events).
    """

    def __init__(self):
        self.mode = {"local": "ok", "cloud": "ok"}
        self.calls: list[tuple[str, dict]] = []

    def hosts_called(self) -> list[str]:
        return [host for host, _ in self.calls]

    async def handler(self, request: httpx2.Request) -> httpx2.Response:
        host = request.url.host
        body = json.loads(request.content)
        self.calls.append((host, body))
        mode = self.mode[host]

        if mode == "refuse":
            raise httpx2.ConnectError("connection refused", request=request)
        if mode == "timeout":
            raise httpx2.ReadTimeout("timed out", request=request)
        if mode in ("400", "429", "500"):
            error = {"error": {"message": f"upstream {mode}", "type": "upstream"}}
            return httpx2.Response(int(mode), json=error)

        if not body.get("stream"):
            return httpx2.Response(200, json=completion(body["model"]))
        break_after = {"break-at-start": 0, "break-mid-stream": 2}.get(mode)
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        events = sse_events(body["model"], include_usage)
        if mode == "fragmented":  # events split across network reads, as over real TCP
            data = b"".join(events)
            events = [data[i : i + 7] for i in range(0, len(data), 7)]
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=stream_body(events, break_after),
        )


class Gateway:
    def __init__(self, client: TestClient, upstreams: FakeUpstreams, outcomes: list):
        self.client = client
        self.upstreams = upstreams
        self.outcomes: list[RequestOutcome] = outcomes

    def admin(self, method: str, path: str, **kwargs) -> httpx2.Response:
        headers = {"authorization": f"Bearer {MASTER_KEY}"}
        return self.client.request(method, path, headers=headers, **kwargs)

    def create_key(self, alias: str = "team", allow_external: bool = False) -> str:
        resp = self.admin(
            "POST", "/admin/keys", json={"alias": alias, "allow_external": allow_external}
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["key"]

    def chat(self, key: str, model: str = "qwen3", stream: bool = False, **extra):
        body = {"model": model, "messages": [{"role": "user", "content": "Hi"}], **extra}
        if stream:
            body["stream"] = True
        return self.client.post(
            "/v1/chat/completions", json=body, headers={"authorization": f"Bearer {key}"}
        )


@pytest.fixture
def gateway(tmp_path):
    upstreams = FakeUpstreams()
    outcomes: list[RequestOutcome] = []

    async def record(outcome: RequestOutcome) -> None:
        outcomes.append(outcome)

    settings = Settings(
        master_key=MASTER_KEY,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        cooldown_seconds=30,
    )
    app = create_app(settings, ROUTES, httpx2.MockTransport(upstreams.handler), hooks=[record])
    with TestClient(app) as client:
        yield Gateway(client, upstreams, outcomes)
