"""Server-Sent Events stream for live agent thinking.

Background
==========
The legacy /api/v1/agent/thinking endpoint polled twin's EventLog
filtered to a coarse list of event types. That gave the UI historical
breadcrumbs but no live reasoning — the user stared at a spinner,
then the answer appeared, then the past-tense thinking trace flashed.

This module exposes the SDK's ``ThinkingEmitter`` over a streaming
HTTP transport. The desktop opens one long-lived SSE connection per
chat surface; every step the agent runs (memory recall, tool call,
Gemini reasoning, evolution proposal, …) ships as soon as it fires.

Why SSE, not WebSocket
----------------------
Traffic is one-way (server → client). SSE has built-in reconnect, no
ping/pong needed, every browser/desktop HTTP client supports it
natively, and FastAPI's StreamingResponse renders the framing for
free. The only WebSocket affordance we'd want is bidirectional cancel,
which we don't need — disconnects are detected by the writer side.

Per-twin subscription
---------------------
We pull the user's twin via ``twin_manager.get_twin`` (which lazy-
constructs it on first chat if needed), then call ``twin.thinking.subscribe()``
to get a queue. The handler awaits events on that queue and serialises
them as SSE frames. On client disconnect (or server shutdown), the
handler unsubscribes so we don't leak bounded queues.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from nexus_server.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


# How often we send a comment-line keepalive when the agent is idle.
# SSE comment frames (lines starting with ``:``) are silently dropped
# by every conformant client but keep proxies / load balancers from
# closing the idle TCP connection. 15s is well below the typical
# 30-60s idle timeout.
_KEEPALIVE_INTERVAL_SECONDS = 15.0


def _format_sse(event: dict) -> str:
    """Render a single SSE frame.

    SSE framing is dead simple: ``data: <line>\\n`` for each line of
    the payload, then a blank line to flush. We use a single-line JSON
    payload so the client side can ``JSON.parse`` (or System.Text.Json)
    without splitting work.
    """
    payload = json.dumps(event, separators=(",", ":"))
    return f"data: {payload}\n\n"


async def _stream_for_user(user_id: str, request: Request) -> AsyncIterator[str]:
    """Async generator yielding SSE frames for one user's thinking.

    Yields:
      * one ``hello`` frame so the client knows the connection is live
        even before the agent does anything.
      * live event frames (kind = ThinkingEmitter.emit's kind value)
        as they fire.
      * keepalive comment lines every 15s of idleness.
    Stops when the client disconnects (``await request.is_disconnected()``)
    or the server is shutting down.
    """
    # Lazy import: twin_manager pulls in nexus.twin which has heavy
    # optional deps. If the SSE endpoint is hit before twin is ever
    # used, we want that to be the moment the cost is paid (not at
    # module import).
    from nexus_server.twin_manager import get_twin

    try:
        twin = await get_twin(user_id)
    except Exception as e:
        logger.warning(
            "thinking SSE: get_twin failed for %s: %s", user_id, e,
        )
        yield _format_sse({
            "kind": "error",
            "label": "Twin unavailable",
            "content": str(e),
        })
        return

    emitter = getattr(twin, "thinking", None)
    if emitter is None:
        # Older twin without the emitter — emit a polite stub and
        # close so the desktop can fall back to the legacy polled
        # /agent/thinking endpoint.
        yield _format_sse({
            "kind": "error",
            "label": "Live thinking unavailable",
            "content": "Twin doesn't expose ThinkingEmitter on this server",
        })
        return

    sub = emitter.subscribe()
    yield _format_sse({
        "kind": "hello",
        "label": "Thinking stream connected",
        "content": "",
    })

    try:
        while True:
            # Disconnect check is async-friendly: starlette's Request
            # tracks the underlying transport and reports promptly.
            if await request.is_disconnected():
                logger.debug("thinking SSE: client disconnected (user=%s)", user_id)
                return

            ev = await sub.next_event(timeout=_KEEPALIVE_INTERVAL_SECONDS)
            if ev is None:
                # Idle window — emit a comment line so the connection
                # stays warm. Clients ignore comment frames.
                yield ": keepalive\n\n"
                continue
            yield _format_sse(ev.to_dict())
    finally:
        emitter.unsubscribe(sub)


@router.get("/thinking/stream")
async def thinking_stream(
    request: Request,
    current_user: str = Depends(get_current_user),
) -> StreamingResponse:
    """Long-lived SSE feed of the agent's live thinking.

    Each chat turn the user runs in ``/api/v1/llm/chat`` produces a
    sequence of typed steps (memory_recall, reasoning, tool_call,
    insight, evolution_propose, replying, replied, …). The desktop's
    cognition panel renders them in real time, grouped by ``turn_id``.

    The endpoint authenticates with the same Bearer JWT every other
    /api/v1/* endpoint uses; the SSE channel is scoped to that user
    only. We never multiplex multiple users into one stream.
    """
    return StreamingResponse(
        _stream_for_user(current_user, request),
        media_type="text/event-stream",
        # Disable proxy buffering so each frame lands at the client
        # the moment we yield it. Without this nginx / cloudflare
        # buffer ~4KB before flushing — fine for documents, terrible
        # for live updates.
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
