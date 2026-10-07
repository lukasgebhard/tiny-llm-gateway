import pytest
from prometheus_client.parser import text_string_to_metric_families

from tests.conftest import STREAM_USAGE, USAGE


@pytest.fixture
def key(gateway):
    return gateway.create_key(allow_external=True)


def sample(gateway, name: str, **labels) -> float | None:
    """The value of one sample on /metrics, or None if it doesn't exist."""
    resp = gateway.client.get("/metrics")
    assert resp.status_code == 200
    for family in text_string_to_metric_families(resp.text):
        for s in family.samples:
            if s.name == name and s.labels == labels:
                return s.value
    return None


def test_requests_and_tokens_are_counted(gateway, key):
    gateway.chat(key)
    gateway.chat(key)
    gateway.chat(key, stream=True)
    ok = {"model": "qwen3", "backend": "local"}
    assert sample(gateway, "gateway_requests_total", status="ok", **ok) == 3
    assert sample(gateway, "gateway_request_duration_seconds_count", **ok) == 3
    assert sample(gateway, "gateway_time_to_first_byte_seconds_count", **ok) == 1
    prompt = 2 * USAGE["prompt_tokens"] + STREAM_USAGE["prompt_tokens"]
    completion = 2 * USAGE["completion_tokens"] + STREAM_USAGE["completion_tokens"]
    assert sample(gateway, "gateway_tokens_total", type="prompt", **ok) == prompt
    assert sample(gateway, "gateway_tokens_total", type="completion", **ok) == completion


def test_fallback_and_cooldown(gateway, key):
    labels = {"backend": "local", "upstream_model": "qwen-local"}
    assert sample(gateway, "gateway_deployment_cooldown", **labels) == 0

    gateway.upstreams.mode["local"] = "500"
    gateway.chat(key)
    fallback = {"model": "qwen3", "from_backend": "local", "to_backend": "cloud"}
    assert sample(gateway, "gateway_fallbacks_total", **fallback) == 1
    assert sample(gateway, "gateway_deployment_cooldown", **labels) == 1
    assert sample(gateway, "gateway_requests_total", model="qwen3", backend="cloud", status="ok")


def test_failures_without_any_backend(gateway):
    restricted = gateway.create_key("restricted", allow_external=False)
    gateway.upstreams.mode["local"] = "refuse"
    gateway.chat(restricted)
    labels = {"model": "qwen3", "backend": "none"}
    assert sample(gateway, "gateway_requests_total", status="no_backend", **labels) == 1
    fallback = {"model": "qwen3", "from_backend": "local", "to_backend": "none"}
    assert sample(gateway, "gateway_fallbacks_total", **fallback) == 1


def test_policy_denials(gateway):
    restricted = gateway.create_key("restricted", allow_external=False)
    gateway.chat(restricted, model="gpt")
    assert sample(gateway, "gateway_policy_denials_total", model="gpt") == 1
    labels = {"model": "gpt", "backend": "none", "status": "rejected"}
    assert sample(gateway, "gateway_requests_total", **labels) == 1


def test_unknown_models_share_one_label(gateway, key):
    for name in ("made-up-1", "made-up-2", "made-up-3"):
        gateway.chat(key, model=name)
    labels = {"model": "unknown", "backend": "none", "status": "rejected"}
    assert sample(gateway, "gateway_requests_total", **labels) == 3
    assert "made-up" not in gateway.client.get("/metrics").text


def test_key_aliases_are_not_exposed(gateway):
    key = gateway.create_key("secret-team-name", allow_external=True)
    gateway.chat(key)
    assert "secret-team-name" not in gateway.client.get("/metrics").text
