def test_missing_or_unknown_key_is_rejected(gateway):
    assert gateway.client.get("/v1/models").status_code == 401
    resp = gateway.client.get("/v1/models", headers={"authorization": "Bearer sk-nope"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"


def test_admin_requires_master_key(gateway):
    key = gateway.create_key()
    resp = gateway.client.post(
        "/admin/keys", json={"alias": "x"}, headers={"authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 401


def test_key_secret_is_only_returned_on_creation(gateway):
    gateway.create_key("team-a")
    keys = gateway.admin("GET", "/admin/keys").json()
    assert [k["alias"] for k in keys] == ["team-a"]
    assert "key" not in keys[0]


def test_duplicate_alias_conflicts(gateway):
    gateway.create_key("team-a")
    resp = gateway.admin("POST", "/admin/keys", json={"alias": "team-a"})
    assert resp.status_code == 409


def test_deactivated_key_is_rejected(gateway):
    key = gateway.create_key("team-a")
    key_id = gateway.admin("GET", "/admin/keys").json()[0]["id"]
    resp = gateway.admin("PATCH", f"/admin/keys/{key_id}", json={"active": False})
    assert resp.json()["active"] is False
    assert gateway.chat(key).status_code == 401


def test_invalid_json_body(gateway):
    key = gateway.create_key()
    resp = gateway.client.post(
        "/v1/chat/completions", content=b"{not json", headers={"authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 400
