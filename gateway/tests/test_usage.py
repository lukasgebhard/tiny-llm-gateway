import pytest

from tests.conftest import STREAM_CHUNKS, STREAM_USAGE, USAGE
from tests.test_streaming import contents, parse_sse


@pytest.fixture
def key(gateway):
    return gateway.create_key("team-a", allow_external=True)


def usage(gateway, group_by="key", **params):
    resp = gateway.admin("GET", "/admin/usage", params={"group_by": group_by, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_tokens_are_read_from_a_completion(gateway, key):
    gateway.chat(key)
    (outcome,) = gateway.outcomes
    assert (outcome.prompt_tokens, outcome.completion_tokens) == (3, 1)


def test_tokens_are_read_from_a_stream(gateway, key):
    resp = gateway.chat(key, stream=True)
    (outcome,) = gateway.outcomes
    assert (outcome.prompt_tokens, outcome.completion_tokens) == (5, 3)
    # The backend is asked for usage, but the client didn't ask, so it doesn't see it.
    ((_, upstream_body),) = gateway.upstreams.calls
    assert upstream_body["stream_options"] == {"include_usage": True}
    events = parse_sse(resp.text)
    assert contents(events) == STREAM_CHUNKS
    assert not any(isinstance(e, dict) and e.get("usage") for e in events)
    assert events[-1] == "[DONE]"


def test_usage_chunk_is_forwarded_if_the_client_asked(gateway, key):
    resp = gateway.chat(key, stream=True, stream_options={"include_usage": True})
    events = parse_sse(resp.text)
    assert events[-2]["usage"] == STREAM_USAGE
    assert contents(events) == STREAM_CHUNKS


def test_events_split_across_network_reads_are_reassembled(gateway, key):
    gateway.upstreams.mode["local"] = "fragmented"
    resp = gateway.chat(key, stream=True)
    events = parse_sse(resp.text)
    assert contents(events) == STREAM_CHUNKS
    assert events[-1] == "[DONE]"
    assert gateway.outcomes[0].completion_tokens == STREAM_USAGE["completion_tokens"]


def test_interrupted_stream_has_no_token_counts(gateway, key):
    gateway.upstreams.mode["local"] = "break-mid-stream"
    gateway.chat(key, stream=True)
    (outcome,) = gateway.outcomes
    assert outcome.status == "stream_error"
    assert outcome.prompt_tokens is None


def test_usage_per_key(gateway, key):
    other = gateway.create_key("team-b", allow_external=False)
    gateway.chat(key)
    gateway.chat(key, stream=True)
    gateway.chat(other)
    gateway.chat(other, model="gpt")  # rejected: external model

    rows = {row["key"]: row for row in usage(gateway, "key")}
    assert rows["team-a"] == {
        "key": "team-a",
        "requests": 2,
        "errors": 0,
        "fallbacks": 0,
        "prompt_tokens": USAGE["prompt_tokens"] + STREAM_USAGE["prompt_tokens"],
        "completion_tokens": USAGE["completion_tokens"] + STREAM_USAGE["completion_tokens"],
        "total_tokens": USAGE["total_tokens"] + STREAM_USAGE["total_tokens"],
        "avg_latency_ms": rows["team-a"]["avg_latency_ms"],
    }
    assert rows["team-b"]["requests"] == 2
    assert rows["team-b"]["errors"] == 1
    assert rows["team-b"]["prompt_tokens"] == USAGE["prompt_tokens"]


def test_usage_per_backend_counts_fallbacks(gateway, key):
    gateway.chat(key)
    gateway.upstreams.mode["local"] = "500"
    gateway.chat(key)

    rows = {row["backend"]: row for row in usage(gateway, "backend")}
    assert rows["local"]["requests"] == 1
    assert rows["cloud"]["requests"] == 1
    assert rows["cloud"]["fallbacks"] == 1


def test_usage_per_model(gateway, key):
    gateway.chat(key)
    gateway.chat(key, model="gpt")
    gateway.chat(key, model="gpt")
    assert [(row["model"], row["requests"]) for row in usage(gateway, "model")] == [
        ("gpt", 2),
        ("qwen3", 1),
    ]


def test_usage_since(gateway, key):
    gateway.chat(key)
    assert usage(gateway, since="2000-01-01T00:00:00Z")[0]["requests"] == 1
    assert usage(gateway, since="2999-01-01T00:00:00") == []


def test_usage_requires_master_key_and_valid_grouping(gateway, key):
    resp = gateway.client.get("/admin/usage", headers={"authorization": f"Bearer {key}"})
    assert resp.status_code == 401
    assert gateway.admin("GET", "/admin/usage", params={"group_by": "nope"}).status_code == 422
