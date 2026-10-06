import json

import pytest

from tests.conftest import STREAM_CHUNKS


@pytest.fixture
def key(gateway):
    return gateway.create_key(allow_external=True)


def parse_sse(text: str) -> list:
    events = []
    for block in text.strip().split("\n\n"):
        data = block.removeprefix("data: ")
        events.append(data if data == "[DONE]" else json.loads(data))
    return events


def contents(events: list) -> list[str]:
    return [
        e["choices"][0]["delta"]["content"]
        for e in events
        if isinstance(e, dict) and e.get("choices")
    ]


def test_stream_is_passed_through(gateway, key):
    resp = gateway.chat(key, stream=True)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["gateway-backend"] == "local"
    events = parse_sse(resp.text)
    assert contents(events) == STREAM_CHUNKS
    assert events[-1] == "[DONE]"
    assert events[0]["model"] == "qwen-local"


@pytest.mark.parametrize("failure", ["refuse", "500", "break-at-start"])
def test_falls_back_before_the_first_chunk(gateway, key, failure):
    gateway.upstreams.mode["local"] = failure
    resp = gateway.chat(key, stream=True)
    assert resp.status_code == 200
    assert resp.headers["gateway-backend"] == "cloud"
    assert resp.headers["gateway-attempts"] == "2"
    assert contents(parse_sse(resp.text)) == STREAM_CHUNKS


def test_mid_stream_failure_ends_the_stream_with_an_error(gateway, key):
    gateway.upstreams.mode["local"] = "break-mid-stream"
    resp = gateway.chat(key, stream=True)
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert contents(events) == STREAM_CHUNKS[:2]
    assert events[-2]["error"]["code"] == "stream_interrupted"
    assert events[-1] == "[DONE]"
    assert gateway.upstreams.hosts_called() == ["local"]  # no silent switch mid-answer
    assert gateway.outcomes[0].status == "stream_error"


def test_streaming_outcome_has_time_to_first_byte(gateway, key):
    gateway.chat(key, stream=True)
    (outcome,) = gateway.outcomes
    assert outcome.stream
    assert outcome.status == "ok"
    assert 0 < outcome.ttfb_s <= outcome.latency_s
