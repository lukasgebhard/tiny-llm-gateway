"""A tiny OpenAI-compatible fake backend for demos and clusters without vLLM.

Run with: uvicorn app.mock_upstream:app

Environment:
  MOCK_FAILURE_RATE   probability (0..1) of answering with HTTP 500 (default 0)
  MOCK_TOKEN_DELAY    seconds between streamed tokens (default 0.05)
"""

import asyncio
import json
import os
import random
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="mock-upstream")

FAILURE_RATE = float(os.environ.get("MOCK_FAILURE_RATE", "0"))
TOKEN_DELAY = float(os.environ.get("MOCK_TOKEN_DELAY", "0.05"))


def reply_tokens(messages: list[dict]) -> list[str]:
    last = next((m for m in reversed(messages) if m.get("role") == "user"), {})
    content = last.get("content", "")
    if isinstance(content, list):  # content parts
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    words = f"This is a mock reply to: {content}".split()
    return [w if i == 0 else " " + w for i, w in enumerate(words)]


def usage(messages: list[dict], tokens: list[str]) -> dict:
    prompt = sum(len(str(m.get("content", "")).split()) for m in messages)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": len(tokens),
        "total_tokens": prompt + len(tokens),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "mock", "object": "model", "owned_by": "mock"}]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    if random.random() < FAILURE_RATE:
        return JSONResponse(
            {"error": {"message": "simulated failure", "type": "server_error"}}, status_code=500
        )

    model = body.get("model", "mock")
    messages = body.get("messages", [])
    tokens = reply_tokens(messages)
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    if not body.get("stream"):
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "".join(tokens)},
                }
            ],
            "usage": usage(messages, tokens),
        }

    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

    def chunk(delta: dict, finish_reason: str | None = None) -> str:
        data = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(data)}\n\n"

    async def events():
        yield chunk({"role": "assistant", "content": ""})
        for token in tokens:
            await asyncio.sleep(TOKEN_DELAY)
            yield chunk({"content": token})
        yield chunk({}, finish_reason="stop")
        if include_usage:
            data = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [],
                "usage": usage(messages, tokens),
            }
            yield f"data: {json.dumps(data)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
