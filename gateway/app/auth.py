"""API key authentication."""

import hashlib
import secrets
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import select

from app.errors import unauthorized
from app.models import ApiKey

KEY_PREFIX = "sk-tlg-"


@dataclass(frozen=True)
class Principal:
    """The authenticated caller of a request."""

    key_id: int
    alias: str
    allow_external: bool


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    # Keys are long random strings, so a fast unsalted hash is sufficient.
    return hashlib.sha256(key.encode()).hexdigest()


def bearer_token(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise unauthorized()
    return token.strip()


async def require_api_key(request: Request) -> Principal:
    token = bearer_token(request)
    async with request.app.state.sessionmaker() as session:
        key = await session.scalar(
            select(ApiKey).where(ApiKey.key_hash == hash_key(token), ApiKey.active)
        )
    if key is None:
        raise unauthorized()
    return Principal(key_id=key.id, alias=key.alias, allow_external=key.allow_external)


async def require_master_key(request: Request) -> None:
    token = bearer_token(request)
    master_key = request.app.state.settings.master_key
    if not secrets.compare_digest(token.encode(), master_key.encode()):
        raise unauthorized("Admin endpoints require the master key")
