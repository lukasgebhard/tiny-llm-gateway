"""The allow_external flag decides whether a key may reach external backends."""


def model_ids(gateway, key) -> set[str]:
    resp = gateway.client.get("/v1/models", headers={"authorization": f"Bearer {key}"})
    return {m["id"] for m in resp.json()["data"]}


def test_models_are_filtered_by_policy(gateway):
    restricted = gateway.create_key("restricted", allow_external=False)
    trusted = gateway.create_key("trusted", allow_external=True)
    assert model_ids(gateway, restricted) == {"qwen3", "local-only"}
    assert model_ids(gateway, trusted) == {"qwen3", "gpt", "local-only"}


def test_external_only_model_is_forbidden_without_permission(gateway):
    key = gateway.create_key(allow_external=False)
    resp = gateway.chat(key, model="gpt")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "external_models_not_allowed"
    assert gateway.upstreams.calls == []
    assert gateway.outcomes[0].status == "rejected"


def test_external_model_is_served_with_permission(gateway):
    key = gateway.create_key(allow_external=True)
    resp = gateway.chat(key, model="gpt")
    assert resp.status_code == 200
    assert resp.headers["gateway-backend"] == "cloud"


def test_restricted_key_never_falls_back_to_external(gateway):
    key = gateway.create_key(allow_external=False)
    gateway.upstreams.mode["local"] = "refuse"
    resp = gateway.chat(key)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_backend_available"
    assert gateway.upstreams.hosts_called() == ["local"]


def test_permission_change_takes_effect_immediately(gateway):
    key = gateway.create_key(allow_external=False)
    assert gateway.chat(key, model="gpt").status_code == 403
    key_id = gateway.admin("GET", "/admin/keys").json()[0]["id"]
    gateway.admin("PATCH", f"/admin/keys/{key_id}", json={"allow_external": True})
    assert gateway.chat(key, model="gpt").status_code == 200
