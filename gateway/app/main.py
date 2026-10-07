"""FastAPI application. Run with: uvicorn app.main:create_app --factory"""

import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated

import httpx2
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import admin
from app.auth import Principal, require_api_key
from app.config import RoutesConfig, Settings, load_routes
from app.db import init_schema, make_engine, make_sessionmaker
from app.errors import GatewayError, bad_request
from app.routing import Cooldown, Hook, Router
from app.usage import usage_recorder

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

Caller = Annotated[Principal, Depends(require_api_key)]


def create_app(
    settings: Settings | None = None,
    routes: RoutesConfig | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    hooks: list[Hook] | None = None,
) -> FastAPI:
    """Build the app. The params are useful for injecting mock objects in tests."""
    settings = settings or Settings()
    routes = routes or load_routes(settings.routes_file)
    hooks = list(hooks or [])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url)
        await init_schema(engine)
        timeout = httpx2.Timeout(settings.read_timeout, connect=settings.connect_timeout)
        async with httpx2.AsyncClient(timeout=timeout, transport=transport) as client:
            sessionmaker = make_sessionmaker(engine)
            app.state.settings = settings
            app.state.sessionmaker = sessionmaker
            app.state.router = Router(
                routes,
                client,
                Cooldown(settings.cooldown_seconds),
                [usage_recorder(sessionmaker), *hooks],
            )
            yield
        await engine.dispose()

    app = FastAPI(title="tiny-llm-gateway", lifespan=lifespan)
    app.include_router(admin.router)

    @app.exception_handler(GatewayError)
    async def gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        return exc.to_response()

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, principal: Caller) -> Response:
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise bad_request("Request body is not valid JSON") from exc
        if not isinstance(body, dict):
            raise bad_request("Request body must be a JSON object")
        return await request.app.state.router.chat_completions(body, principal)

    @app.get("/v1/models")
    async def list_models(request: Request, principal: Caller):
        aliases = request.app.state.router.visible_models(principal)
        return {
            "object": "list",
            "data": [
                {"id": alias, "object": "model", "created": 0, "owned_by": "tiny-llm-gateway"}
                for alias in aliases
            ],
        }

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request):
        try:
            async with request.app.state.sessionmaker() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            return JSONResponse({"status": "unavailable", "detail": str(exc)}, status_code=503)
        return {"status": "ok"}

    return app
