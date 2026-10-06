import pytest


@pytest.fixture
def key(gateway):
    return gateway.create_key(allow_external=True)


def test_first_deployment_is_preferred(gateway, key):
    resp = gateway.chat(key)
    assert resp.status_code == 200
    assert resp.headers["gateway-backend"] == "local"
    assert resp.headers["gateway-attempts"] == "1"
    assert resp.json()["model"] == "qwen-local"
    assert gateway.upstreams.hosts_called() == ["local"]


def test_unknown_model(gateway, key):
    resp = gateway.chat(key, model="nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


def test_default_params_are_merged_under_the_request(gateway, key):
    gateway.chat(key)
    gateway.chat(key, temperature=0.7)
    (_, first), (_, second) = gateway.upstreams.calls
    assert first["temperature"] == 0
    assert second["temperature"] == 0.7
    assert first["model"] == "qwen-local"


@pytest.mark.parametrize("failure", ["refuse", "timeout", "429", "500"])
def test_falls_back_on_retryable_failures(gateway, key, failure):
    gateway.upstreams.mode["local"] = failure
    resp = gateway.chat(key)
    assert resp.status_code == 200
    assert resp.headers["gateway-backend"] == "cloud"
    assert resp.headers["gateway-attempts"] == "2"
    assert resp.json()["model"] == "gpt-cloud"
    assert gateway.upstreams.hosts_called() == ["local", "cloud"]


def test_client_errors_are_returned_without_fallback(gateway, key):
    gateway.upstreams.mode["local"] = "400"
    resp = gateway.chat(key)
    assert resp.status_code == 400
    assert resp.json()["error"]["message"] == "upstream 400"
    assert resp.headers["gateway-backend"] == "local"
    assert gateway.upstreams.hosts_called() == ["local"]


def test_all_backends_failing(gateway, key):
    gateway.upstreams.mode.update(local="500", cloud="refuse")
    resp = gateway.chat(key)
    assert resp.status_code == 503
    assert resp.headers["gateway-attempts"] == "2"
    assert "gateway-backend" not in resp.headers


def test_failed_deployment_moves_to_the_end_during_cooldown(gateway, key):
    gateway.upstreams.mode["local"] = "refuse"
    gateway.chat(key)
    gateway.upstreams.mode["local"] = "ok"
    gateway.upstreams.calls.clear()

    resp = gateway.chat(key)
    assert resp.headers["gateway-backend"] == "cloud"
    assert gateway.upstreams.hosts_called() == ["cloud"]


def test_cooling_deployment_is_still_the_last_resort(gateway):
    restricted = gateway.create_key("restricted", allow_external=False)
    gateway.upstreams.mode["local"] = "refuse"
    assert gateway.chat(restricted).status_code == 503
    gateway.upstreams.mode["local"] = "ok"
    resp = gateway.chat(restricted)
    assert resp.status_code == 200
    assert resp.headers["gateway-backend"] == "local"


def test_outcome_is_reported_to_hooks(gateway, key):
    gateway.upstreams.mode["local"] = "500"
    gateway.chat(key)
    (outcome,) = gateway.outcomes
    assert outcome.key_alias == "team"
    assert outcome.requested_model == "qwen3"
    assert outcome.status == "ok"
    assert outcome.http_status == 200
    assert outcome.backend == "cloud"
    assert outcome.upstream_model == "gpt-cloud"
    assert outcome.attempts == 2
    assert outcome.failed_backends == ["local"]
    assert outcome.latency_s > 0
