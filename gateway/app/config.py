"""Gateway settings (environment) and routing configuration (YAML)."""

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_")

    master_key: str
    database_url: str = "sqlite+aiosqlite:///./gateway.db"
    routes_file: Path = Path("routes.yaml")
    connect_timeout: float = 3.0
    read_timeout: float = 300.0
    cooldown_seconds: float = 30.0


class Backend(BaseModel):
    base_url: str
    api_key_env: str | None = None
    external: bool = False
    # Resolved from api_key_env at load time; never part of the YAML.
    api_key: str | None = Field(default=None, exclude=True)


class Deployment(BaseModel):
    backend: str
    model: str
    # Defaults merged under the client's request body, e.g. backend-specific
    # parameters like vLLM's chat_template_kwargs that other providers reject.
    default_params: dict[str, Any] = Field(default_factory=dict)


class RoutesConfig(BaseModel):
    backends: dict[str, Backend]
    models: dict[str, list[Deployment]]

    @model_validator(mode="after")
    def check_backend_references(self) -> RoutesConfig:
        for alias, deployments in self.models.items():
            if not deployments:
                raise ValueError(f"model {alias!r} has no deployments")
            for dep in deployments:
                if dep.backend not in self.backends:
                    raise ValueError(f"model {alias!r} references unknown backend {dep.backend!r}")
        return self


def load_routes(path: Path, env: dict[str, str] | None = None) -> RoutesConfig:
    """Load the routing config and resolve backend API keys from the environment.

    Backends whose API key variable is unset are dropped, together with their
    deployments, so a cluster without cloud credentials still serves local models.
    """
    env = os.environ if env is None else env
    config = RoutesConfig.model_validate(yaml.safe_load(path.read_text()))

    usable: dict[str, Backend] = {}
    for name, backend in config.backends.items():
        if backend.api_key_env is None:
            usable[name] = backend
        elif env.get(backend.api_key_env):
            usable[name] = backend.model_copy(update={"api_key": env[backend.api_key_env]})
        else:
            log.warning("backend %s disabled: %s is not set", name, backend.api_key_env)

    models = {}
    for alias, deployments in config.models.items():
        kept = [dep for dep in deployments if dep.backend in usable]
        if kept:
            models[alias] = kept
        else:
            log.warning("model %s disabled: none of its backends are available", alias)

    return RoutesConfig(backends=usable, models=models)
