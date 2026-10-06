"""Requests to OpenAI-compatible upstream backends."""

import httpx

from app.config import Backend, Deployment


def build_request(
    client: httpx.AsyncClient, backend: Backend, deployment: Deployment, body: dict
) -> httpx.Request:
    payload = {**deployment.default_params, **body, "model": deployment.model}
    headers = {}
    if backend.api_key:
        headers["authorization"] = f"Bearer {backend.api_key}"
    url = backend.base_url.rstrip("/") + "/chat/completions"
    return client.build_request("POST", url, json=payload, headers=headers)


def is_retryable(status_code: int) -> bool:
    """Whether another backend might succeed where this one failed.

    Rate limits and server errors are worth retrying elsewhere; any other 4xx
    means the request itself is wrong and would fail everywhere.
    """
    return status_code == 429 or status_code >= 500
