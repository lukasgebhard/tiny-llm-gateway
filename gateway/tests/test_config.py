import pytest
from pydantic import ValidationError

from app.config import load_routes

ROUTES_YAML = """
backends:
  local: {base_url: "http://local/v1"}
  cloud: {base_url: "http://cloud/v1", api_key_env: CLOUD_KEY, external: true}
models:
  mixed: [{backend: local, model: m1}, {backend: cloud, model: m2}]
  cloud-only: [{backend: cloud, model: m2}]
"""


def test_api_key_is_resolved_from_the_environment(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_text(ROUTES_YAML)
    routes = load_routes(path, env={"CLOUD_KEY": "sk-123"})
    assert routes.backends["cloud"].api_key == "sk-123"
    assert set(routes.models) == {"mixed", "cloud-only"}


def test_backend_without_key_is_disabled(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_text(ROUTES_YAML)
    routes = load_routes(path, env={})
    assert set(routes.backends) == {"local"}
    assert set(routes.models) == {"mixed"}
    assert [d.backend for d in routes.models["mixed"]] == ["local"]


def test_unknown_backend_reference_is_rejected(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_text("backends: {}\nmodels:\n  m: [{backend: nope, model: x}]\n")
    with pytest.raises(ValidationError, match="unknown backend"):
        load_routes(path, env={})
