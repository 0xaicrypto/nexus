"""Unit tests for the SDK's ``distiller`` module.

Covers the layer split that moved ``extract_text`` + ``distill`` out of
the server-only ``nexus_server.attachment_distiller`` module so any SDK
consumer can use the same pipeline. The tests intentionally drive the
SDK API directly (``nexus_core.distill`` / ``extract_text``) — if a
future refactor breaks the public surface these will fail loudly.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

import nexus_core
from nexus_core.distiller import (
    DISTILL_INPUT_CHAR_BUDGET,
    DISTILL_OUTPUT_CHAR_BUDGET,
    distill,
    extract_text,
)


# ── Public API surface ────────────────────────────────────────────────


def test_top_level_imports_exposed():
    """``nexus_core.distill`` and friends must be importable directly
    from the package root — same ergonomic surface as the other utilities
    (``EventLog``, etc.)."""
    assert nexus_core.distill is distill
    assert nexus_core.extract_text is extract_text
    assert nexus_core.DISTILL_INPUT_CHAR_BUDGET == DISTILL_INPUT_CHAR_BUDGET
    assert nexus_core.DISTILL_OUTPUT_CHAR_BUDGET == DISTILL_OUTPUT_CHAR_BUDGET


# ── extract_text ──────────────────────────────────────────────────────


def test_extract_text_returns_text_when_content_text_present():
    body = "hello there\nthis is a small text file"
    out, source = extract_text("readme.txt", "text/plain", body, None)
    assert out == body
    assert source == "text"


def test_extract_text_truncates_to_input_budget():
    big = "x" * (DISTILL_INPUT_CHAR_BUDGET + 1000)
    out, source = extract_text("big.txt", "text/plain", big, None)
    assert len(out) == DISTILL_INPUT_CHAR_BUDGET
    assert source == "text"


def test_extract_text_decodes_utf8_base64_when_text_missing():
    raw = "utf-8 fallback works".encode("utf-8")
    b64 = base64.b64encode(raw).decode("ascii")
    out, source = extract_text("note.bin", "application/octet-stream", None, b64)
    assert out == "utf-8 fallback works"
    assert source == "text"


def test_extract_text_returns_binary_stub_for_non_decodable_bytes():
    raw = bytes([0xFF, 0xFE, 0xFD, 0xFC] * 16)  # not utf-8
    b64 = base64.b64encode(raw).decode("ascii")
    out, source = extract_text("noise.bin", "application/octet-stream", None, b64)
    assert source == "binary-stub"
    assert "binary attachment" in out
    assert "noise.bin" in out
    assert str(len(raw)) in out


def test_extract_text_handles_bad_base64_gracefully():
    # Must NOT raise — the whole point of extract_text is "always returns
    # something, never raises".
    out, source = extract_text("broken.bin", "x/y", None, "!!!not-base64!!!")
    assert source == "binary-stub"
    assert "broken.bin" in out


def test_extract_text_empty_attachment():
    out, source = extract_text("empty.txt", "text/plain", None, None)
    assert source == "empty"
    assert "empty.txt" in out


# ── distill ───────────────────────────────────────────────────────────


def test_distill_uses_llm_response_when_available():
    """Successful LLM call → its output becomes the summary, source
    label reflects the input extraction path."""
    captured = {}

    async def fake_llm(messages, system, model, temp, mt, tools):
        captured["messages"] = messages
        captured["system"] = system
        captured["temp"] = temp
        return ("## key facts\n- demo distilled summary", "stub-model", "stop", [])

    summary, source = asyncio.run(
        distill(
            name="report.txt",
            mime="text/plain",
            size_bytes=42,
            content_text="The quarterly numbers are up 15%.",
            content_base64=None,
            llm_fn=fake_llm,
        )
    )
    assert "demo distilled summary" in summary
    assert source == "text"

    # System prompt got threaded through — the llm_fn must see the
    # distillation system prompt, NOT a chat-style conversation.
    assert "summarizer" in captured["system"].lower()
    # Input-text body shows up in the user message
    assert "quarterly numbers" in captured["messages"][0]["content"]


def test_distill_falls_back_to_head_excerpt_on_llm_failure():
    """LLM exception must not propagate — we always return *something*
    so the caller's chat handler is never blocked. Source label gains a
    ``+fallback`` suffix to make the degradation observable."""

    async def boom_llm(messages, system, model, temp, mt, tools):
        raise RuntimeError("LLM down")

    summary, source = asyncio.run(
        distill(
            name="paper.txt",
            mime="text/plain",
            size_bytes=200,
            content_text="meaningful prose " * 50,
            content_base64=None,
            llm_fn=boom_llm,
        )
    )
    assert "Distillation unavailable" in summary
    assert "meaningful prose" in summary  # head excerpt
    assert source == "text+fallback"


def test_distill_truncates_oversized_llm_output():
    """If the LLM goes overboard we trim to DISTILL_OUTPUT_CHAR_BUDGET +
    one ellipsis char so prompts downstream don't blow through the
    context window."""

    huge = "y" * (DISTILL_OUTPUT_CHAR_BUDGET + 5_000)

    async def chatty_llm(messages, system, model, temp, mt, tools):
        return (huge, "stub", "stop", [])

    summary, _source = asyncio.run(
        distill(
            name="x.txt", mime="text/plain", size_bytes=1,
            content_text="anything", content_base64=None,
            llm_fn=chatty_llm,
        )
    )
    assert len(summary) == DISTILL_OUTPUT_CHAR_BUDGET + 1  # trimmed text + "…"
    assert summary.endswith("…")


def test_distill_returns_fallback_when_llm_returns_empty():
    """Empty/whitespace LLM output is treated as failure — we don't
    fold an empty summary into the prompt."""

    async def silent_llm(messages, system, model, temp, mt, tools):
        return ("   \n  ", "stub", "stop", [])

    summary, source = asyncio.run(
        distill(
            name="x.txt", mime="text/plain", size_bytes=1,
            content_text="some text", content_base64=None,
            llm_fn=silent_llm,
        )
    )
    assert "Distillation unavailable" in summary
    assert source == "text+fallback"
