"""Usage accounting: one database row per request, aggregated per key, model or backend."""

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ApiKey, UsageLog
from app.routing import Hook, RequestOutcome

GroupBy = Literal["key", "model", "backend"]

GROUP_COLUMNS = {
    "key": ApiKey.alias,
    "model": UsageLog.requested_model,
    "backend": UsageLog.backend,
}

# Outcomes counted as errors; client disconnects are not the gateway's fault.
ERROR_STATUSES = ("upstream_error", "stream_error", "no_backend", "rejected")


def usage_recorder(sessionmaker: async_sessionmaker) -> Hook:
    """A request hook that stores each outcome in the usage_log table."""

    async def record_usage(outcome: RequestOutcome) -> None:
        async with sessionmaker() as session:
            session.add(
                UsageLog(
                    api_key_id=outcome.key_id,
                    requested_model=outcome.requested_model,
                    backend=outcome.backend,
                    upstream_model=outcome.upstream_model,
                    stream=outcome.stream,
                    status=outcome.status,
                    http_status=outcome.http_status,
                    attempts=outcome.attempts,
                    fallback=outcome.fallback,
                    prompt_tokens=outcome.prompt_tokens,
                    completion_tokens=outcome.completion_tokens,
                    latency_ms=round(outcome.latency_s * 1000),
                    ttfb_ms=None if outcome.ttfb_s is None else round(outcome.ttfb_s * 1000),
                )
            )
            await session.commit()

    return record_usage


async def summarize(
    session: AsyncSession, group_by: GroupBy, since: datetime | None = None
) -> list[dict]:
    group = GROUP_COLUMNS[group_by]
    count = func.count(UsageLog.id)
    stmt = (
        select(
            group.label("group"),
            count.label("requests"),
            func.sum(case((UsageLog.status.in_(ERROR_STATUSES), 1), else_=0)).label("errors"),
            func.sum(case((UsageLog.fallback, 1), else_=0)).label("fallbacks"),
            func.coalesce(func.sum(UsageLog.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(UsageLog.completion_tokens), 0).label("completion_tokens"),
            func.avg(UsageLog.latency_ms).label("avg_latency_ms"),
        )
        .join(ApiKey, ApiKey.id == UsageLog.api_key_id)
        .group_by(group)
        .order_by(count.desc(), group)
    )
    if since is not None:
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        stmt = stmt.where(UsageLog.created_at >= since.astimezone(UTC))

    rows = []
    for row in (await session.execute(stmt)).mappings():
        rows.append(
            {
                group_by: row["group"],
                "requests": row["requests"],
                "errors": row["errors"],
                "fallbacks": row["fallbacks"],
                "prompt_tokens": row["prompt_tokens"],
                "completion_tokens": row["completion_tokens"],
                "total_tokens": row["prompt_tokens"] + row["completion_tokens"],
                "avg_latency_ms": round(row["avg_latency_ms"] or 0),
            }
        )
    return rows
