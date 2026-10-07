"""Admin endpoints for managing API keys (master key required)."""

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth import generate_key, hash_key, require_master_key
from app.errors import GatewayError
from app.models import ApiKey
from app.usage import GroupBy, summarize

router = APIRouter(prefix="/admin", dependencies=[Depends(require_master_key)])


class KeyCreate(BaseModel):
    alias: str = Field(min_length=1, max_length=100)
    allow_external: bool = False


class KeyUpdate(BaseModel):
    allow_external: bool | None = None
    active: bool | None = None


class KeyInfo(BaseModel):
    id: int
    alias: str
    allow_external: bool
    active: bool
    created_at: datetime


class KeyCreated(KeyInfo):
    key: str = Field(description="The secret key. It is shown only once.")


def key_info(key: ApiKey) -> dict:
    return KeyInfo.model_validate(key, from_attributes=True).model_dump()


@router.post("/keys", status_code=201)
async def create_key(payload: KeyCreate, request: Request) -> KeyCreated:
    secret = generate_key()
    key = ApiKey(
        alias=payload.alias, key_hash=hash_key(secret), allow_external=payload.allow_external
    )
    async with request.app.state.sessionmaker() as session:
        session.add(key)
        try:
            await session.commit()
        except IntegrityError as exc:
            raise GatewayError(
                409,
                f"Alias {payload.alias!r} already exists",
                "invalid_request_error",
                "alias_exists",
            ) from exc
    return KeyCreated(**key_info(key), key=secret)


@router.get("/keys")
async def list_keys(request: Request) -> list[KeyInfo]:
    async with request.app.state.sessionmaker() as session:
        keys = await session.scalars(select(ApiKey).order_by(ApiKey.id))
        return [KeyInfo.model_validate(k, from_attributes=True) for k in keys]


@router.get("/usage")
async def usage(request: Request, group_by: GroupBy = "key", since: datetime | None = None):
    """Requests, errors, fallbacks and tokens, grouped by API key, model alias or backend."""
    async with request.app.state.sessionmaker() as session:
        rows = await summarize(session, group_by, since)
    return {"group_by": group_by, "since": since, "data": rows}


@router.patch("/keys/{key_id}")
async def update_key(key_id: int, payload: KeyUpdate, request: Request) -> KeyInfo:
    async with request.app.state.sessionmaker() as session:
        key = await session.get(ApiKey, key_id)
        if key is None:
            raise GatewayError(
                404, f"API key {key_id} does not exist", "invalid_request_error", "key_not_found"
            )
        for field, value in payload.model_dump(exclude_none=True).items():
            setattr(key, field, value)
        await session.commit()
        return KeyInfo(**key_info(key))
