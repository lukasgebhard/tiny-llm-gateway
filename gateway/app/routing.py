"""Model routing: access policy, ordered fallback and cooldown."""

import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

import httpx
from fastapi import Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.auth import Principal
from app.config import Deployment, RoutesConfig
from app.errors import (
    GatewayError,
    bad_request,
    external_not_allowed,
    model_not_found,
    no_backend_available,
)
from app.upstream import build_request, is_retryable

log = logging.getLogger(__name__)


@dataclass
class RequestOutcome:
    """What happened to one chat completion request; passed to all hooks."""

    key_id: int
    key_alias: str
    requested_model: str
    stream: bool
    status: str = "pending"  # ok | upstream_error | stream_error | no_backend | rejected
    http_status: int = 0
    error_code: str | None = None
    backend: str | None = None
    upstream_model: str | None = None
    attempts: int = 0
    failed_backends: list[str] = field(default_factory=list)
    latency_s: float = 0.0
    ttfb_s: float | None = None  # streaming only: time until the first upstream chunk


Hook = Callable[[RequestOutcome], Awaitable[None]]


class Cooldown:
    """Remembers recently failed deployments so requests skip them for a while."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self.seconds = seconds
        self.clock = clock
        self._until: dict[tuple[str, str], float] = {}

    def trip(self, dep: Deployment) -> None:
        self._until[(dep.backend, dep.model)] = self.clock() + self.seconds

    def active(self, dep: Deployment) -> bool:
        return self._until.get((dep.backend, dep.model), 0.0) > self.clock()


class Router:
    def __init__(
        self,
        routes: RoutesConfig,
        client: httpx.AsyncClient,
        cooldown: Cooldown,
        hooks: list[Hook] | None = None,
    ):
        self.routes = routes
        self.client = client
        self.cooldown = cooldown
        self.hooks = hooks if hooks is not None else []

    def _allowed(self, dep: Deployment, principal: Principal) -> bool:
        return principal.allow_external or not self.routes.backends[dep.backend].external

    def visible_models(self, principal: Principal) -> list[str]:
        return [
            alias
            for alias, deps in self.routes.models.items()
            if any(self._allowed(dep, principal) for dep in deps)
        ]

    def plan(self, model: str, principal: Principal) -> list[Deployment]:
        """The deployments to try, in order.

        External deployments are dropped for keys without external access.
        Deployments in cooldown move to the end instead of being dropped, so
        a request still has a last resort when everything has failed recently.
        """
        deps = self.routes.models.get(model)
        if deps is None:
            raise model_not_found(model)
        allowed = [dep for dep in deps if self._allowed(dep, principal)]
        if not allowed:
            raise external_not_allowed(model)
        ready = [dep for dep in allowed if not self.cooldown.active(dep)]
        cooling = [dep for dep in allowed if self.cooldown.active(dep)]
        return ready + cooling

    async def chat_completions(self, body: dict, principal: Principal) -> Response:
        model = body.get("model")
        if not isinstance(model, str) or not model:
            raise bad_request("'model' is required")
        outcome = RequestOutcome(
            key_id=principal.key_id,
            key_alias=principal.alias,
            requested_model=model,
            stream=bool(body.get("stream")),
        )
        started = time.perf_counter()
        try:
            chain = self.plan(model, principal)
        except GatewayError as exc:
            return self._error(exc, outcome, started, status="rejected")
        if outcome.stream:
            return await self._stream(body, chain, outcome, started)
        return await self._complete(body, chain, outcome, started)

    async def _complete(
        self, body: dict, chain: list[Deployment], outcome: RequestOutcome, started: float
    ) -> Response:
        for dep in chain:
            outcome.attempts += 1
            request = build_request(self.client, self.routes.backends[dep.backend], dep, body)
            try:
                resp = await self.client.send(request)
            except httpx.TransportError as exc:
                self._failed(dep, outcome, exc)
                continue
            if is_retryable(resp.status_code):
                self._failed(dep, outcome, f"HTTP {resp.status_code}")
                continue

            self._served_by(dep, outcome, resp.status_code)
            outcome.latency_s = time.perf_counter() - started
            return Response(
                resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"),
                headers=self._headers(outcome),
                background=BackgroundTask(self._run_hooks, outcome),
            )
        return self._error(
            no_backend_available(outcome.requested_model), outcome, started, status="no_backend"
        )

    async def _stream(
        self, body: dict, chain: list[Deployment], outcome: RequestOutcome, started: float
    ) -> Response:
        """Stream from the first deployment that produces a first chunk.

        Fallback is only possible until the first byte reaches the client;
        after that, the response is committed to one backend.
        """
        for dep in chain:
            outcome.attempts += 1
            request = build_request(self.client, self.routes.backends[dep.backend], dep, body)
            try:
                resp = await self.client.send(request, stream=True)
            except httpx.TransportError as exc:
                self._failed(dep, outcome, exc)
                continue
            if is_retryable(resp.status_code):
                await resp.aclose()
                self._failed(dep, outcome, f"HTTP {resp.status_code}")
                continue
            if resp.status_code >= 400:
                content = await resp.aread()
                await resp.aclose()
                self._served_by(dep, outcome, resp.status_code)
                outcome.latency_s = time.perf_counter() - started
                return Response(
                    content,
                    status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"),
                    headers=self._headers(outcome),
                    background=BackgroundTask(self._run_hooks, outcome),
                )

            # A backend may accept the request and die before sending anything,
            # so wait for the first chunk before committing to it.
            chunks = resp.aiter_bytes()
            try:
                first = await anext(chunks, b"")
            except httpx.TransportError as exc:
                await resp.aclose()
                self._failed(dep, outcome, exc)
                continue

            self._served_by(dep, outcome, resp.status_code)
            outcome.ttfb_s = time.perf_counter() - started
            return StreamingResponse(
                self._relay(resp, first, chunks, outcome, started),
                status_code=resp.status_code,
                media_type="text/event-stream",
                headers=self._headers(outcome),
            )
        return self._error(
            no_backend_available(outcome.requested_model), outcome, started, status="no_backend"
        )

    async def _relay(
        self,
        resp: httpx.Response,
        first: bytes,
        chunks: AsyncIterator[bytes],
        outcome: RequestOutcome,
        started: float,
    ) -> AsyncIterator[bytes]:
        outcome.status = "client_disconnected"  # overwritten unless the client goes away
        try:
            yield first
            async for chunk in chunks:
                yield chunk
            outcome.status = "ok"
        except httpx.TransportError as exc:
            log.warning("stream from %s broke mid-response: %r", outcome.backend, exc)
            outcome.status = "stream_error"
            error = GatewayError(
                502, "Upstream stream ended unexpectedly", "upstream_error", "stream_interrupted"
            )
            yield f"data: {json.dumps(error.to_dict())}\n\ndata: [DONE]\n\n".encode()
        finally:
            await resp.aclose()
            outcome.latency_s = time.perf_counter() - started
            await self._run_hooks(outcome)

    def _failed(self, dep: Deployment, outcome: RequestOutcome, reason: object) -> None:
        log.warning("deployment %s/%s failed: %s", dep.backend, dep.model, reason)
        self.cooldown.trip(dep)
        outcome.failed_backends.append(dep.backend)

    @staticmethod
    def _served_by(dep: Deployment, outcome: RequestOutcome, status_code: int) -> None:
        outcome.backend = dep.backend
        outcome.upstream_model = dep.model
        outcome.http_status = status_code
        outcome.status = "ok" if status_code < 400 else "upstream_error"

    def _error(
        self, exc: GatewayError, outcome: RequestOutcome, started: float, status: str
    ) -> Response:
        outcome.status = status
        outcome.http_status = exc.status_code
        outcome.error_code = exc.code
        outcome.latency_s = time.perf_counter() - started
        response = exc.to_response(headers=self._headers(outcome))
        response.background = BackgroundTask(self._run_hooks, outcome)
        return response

    @staticmethod
    def _headers(outcome: RequestOutcome) -> dict[str, str]:
        headers = {"Gateway-Attempts": str(outcome.attempts)}
        if outcome.backend:
            headers["Gateway-Backend"] = outcome.backend
        return headers

    async def _run_hooks(self, outcome: RequestOutcome) -> None:
        for hook in self.hooks:
            try:
                await hook(outcome)
            except Exception:
                log.exception("request hook %s failed", getattr(hook, "__name__", hook))
