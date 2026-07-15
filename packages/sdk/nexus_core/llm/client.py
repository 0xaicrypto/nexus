"""
LLM abstraction — pluggable language model interface.

Supports Google Gemini (default), OpenAI GPT, Anthropic Claude, and
Moonshot AI Kimi (OpenAI-compatible; rides the OpenAI code path with
base_url https://api.moonshot.ai/v1).

Tool Use:
  When tools are provided, chat() returns text as before — tool calls are
  handled internally via a multi-turn loop. The LLM can call tools multiple
  times before producing a final text response.

  chat_with_tools() exposes the raw tool loop for callers that need to
  intercept tool calls (e.g., for logging or custom routing).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING, Optional

from .providers import (
    KIMI_DEFAULT_MODEL,
    LLMProvider,
    resolve_kimi_api_key,
    resolve_kimi_base_url,
)

if TYPE_CHECKING:
    from nexus_core.tools.base import ToolRegistry

logger = logging.getLogger(__name__)

# Maximum tool call rounds to prevent infinite loops. 5 was previously
# adequate, but with the #91 subagent-recipe model a single user
# request can legitimately need 5+ delegate() calls (5-step packs like
# Content Studio: strategist → researcher → writer → editor → publisher)
# plus a final round for the LLM to write its text reply. Raising to 12
# gives multi-step workflows enough budget while still capping a stuck
# tool loop's worst-case wall time. Per-tool timeout (90s) keeps the
# total bounded.
MAX_TOOL_ROUNDS = 12


# How many times to auto-continue when the provider returns
# MAX_TOKENS-truncated text. Each continuation re-sends the
# conversation + a "continue from where you left off" nudge and
# stitches the continuation onto the previous chunk. Cap at 2 so a
# pathologically long pipeline can't burn unbounded tokens — 2
# continuations on top of the initial reply gives us up to 3× the
# max_tokens budget in stitched output, plenty for any reasonable
# article-length deliverable.
MAX_AUTO_CONTINUATIONS = 2

# Marker appended ONLY when auto-continue exhausted its budget and
# the reply was still truncated. The normal happy path stitches
# silently — the user sees a complete reply with no system noise.
_TRUNCATION_MARKER = (
    "\n\n…[response still truncated after auto-continuation. "
    "Ask 'please continue' to get the rest.]"
)

# System message the SDK injects when asking the model to resume.
# Keep it terse — verbose nudges cause models to preamble ("Sure,
# continuing from where I left off…") which double-prefaces the
# stitched output.
_CONTINUATION_NUDGE = (
    "Continue your previous response from EXACTLY where it cut off. "
    "Do NOT repeat any prior text. Do NOT add a preamble like "
    "'continuing from…' — just resume the next character / word "
    "of the prose. Match the prior style + register seamlessly."
)


def _is_max_tokens_truncation(finish_reason) -> bool:
    """Return True if ``finish_reason`` indicates the provider stopped
    generation because it hit max_tokens (vs natural stop, safety
    block, tool call, etc.). Each provider names this differently;
    normalise here so callers can branch once."""
    if finish_reason is None:
        return False
    s = str(finish_reason).upper()
    # Gemini: FinishReason.MAX_TOKENS (also a bare "2" enum value).
    # OpenAI: "length"
    # Anthropic: "max_tokens"
    return (
        "MAX_TOKENS" in s
        or s == "LENGTH" or s.endswith(".LENGTH")
        or s == "2"  # Gemini enum literal
    )

# Hard ceiling on a single tool execution. Tools that already have
# their own internal timeouts (skill installer, file generator) just
# return early — this is the belt-and-braces upper bound so a tool
# that forgets to set its own timeout can't pin the chat. The LLM
# sees a clean ToolResult(success=False) on timeout and can move on.
PER_TOOL_TIMEOUT_SECONDS = 90.0

# Hard ceiling on a single LLM round-trip. The provider SDK calls run
# inside ``loop.run_in_executor`` so a stuck network on the provider's
# side would otherwise pin the chat surface forever (the executor
# thread is just blocked on a socket — Python doesn't interrupt it).
# 90s is well above the p99 of normal completions (Gemini Pro with
# thinking + tools is ~5-15s for chat-length prompts).
LLM_CALL_TIMEOUT_SECONDS = 90.0


class LLMClient:
    """Unified LLM client interface with function calling support."""

    def __init__(
        self,
        provider: LLMProvider,
        api_key: str,
        model: str,
        base_url: Optional[str] = None,
    ):
        self.provider = provider
        self.api_key = api_key
        # Kimi (Moonshot AI) is OpenAI-compatible — it rides the OpenAI
        # code path with a different base_url + model default. Resolve
        # both here so callers can pass provider=KIMI with empty
        # model / key and get sane env-driven behaviour.
        if provider == LLMProvider.KIMI:
            self.model = model or KIMI_DEFAULT_MODEL
            self.base_url = base_url or resolve_kimi_base_url()
            if not self.api_key:
                self.api_key = resolve_kimi_api_key()
        else:
            self.model = model
            self.base_url = base_url
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return

        if self.provider == LLMProvider.GEMINI:
            try:
                from google import genai
                self._client = genai.Client(api_key=self.api_key)
            except ImportError:
                raise ImportError("pip install google-genai")
        elif self.provider == LLMProvider.ANTHROPIC:
            try:
                import anthropic
                self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
            except ImportError:
                raise ImportError("pip install anthropic")
        else:
            # OpenAI — and any OpenAI-compatible provider (Kimi). A
            # custom ``base_url`` (explicit arg, or KIMI_BASE_URL /
            # Moonshot default for provider=KIMI) points the client at
            # the compatible endpoint; everything else (chat,
            # streaming-free completions, tool calling) reuses the
            # stock OpenAI code path below.
            try:
                import openai
                kwargs = {"api_key": self.api_key}
                if self.base_url:
                    kwargs["base_url"] = self.base_url
                self._client = openai.AsyncOpenAI(**kwargs)
            except ImportError:
                raise ImportError("pip install openai")

    # ── Main chat interface ───────────────────────────────────────

    async def chat(
        self,
        messages: list[dict],
        system: str = "",
        temperature: float = 0.7,
        # 2048 was the old default and quietly truncated any reply
        # longer than ~1500 Chinese characters — a problem when the
        # agent runs a full Content Studio recipe and tries to surface
        # a publishable article as its text reply. 8192 covers the
        # 99% case (long-form articles, multi-step explanations,
        # citation lists). Modern Gemini / Claude / GPT all support
        # this and well beyond; the per-provider call paths pass it
        # through verbatim. Caller can override per-call when they
        # KNOW the reply will be short (saves a bit of latency).
        max_tokens: int = 8192,
        json_mode: bool = False,
        tools: Optional["ToolRegistry"] = None,
        thinking_emitter=None,
        **_extra,
    ) -> str:
        """Chat with the LLM, optionally using tools.

        When tools are provided:
          1. LLM receives tool definitions alongside the conversation
          2. If LLM requests a tool call, it's executed automatically
          3. Tool results are fed back to the LLM
          4. Loop continues until LLM produces a text response (or MAX_TOOL_ROUNDS)

        When no tools are provided, behaves exactly as before.

        ``thinking_emitter`` (optional ``ThinkingEmitter``):
            Receives provider-specific reasoning telemetry — Gemini's
            "thinking" tokens, tool decisions, retries — as live
            ``reasoning`` / ``tool_call`` events. Twin passes its own
            emitter through; tests / CLI callers can ignore the param.

        ``**_extra`` swallows future kwargs so callers from older
        versions (or future ones) don't break the call site if a new
        knob is added at the LLMClient layer.
        """
        self._ensure_client()

        if not tools:
            # No tools — use the simple path (unchanged from original)
            return await self._chat_simple(
                messages, system, temperature, max_tokens, json_mode,
                thinking_emitter=thinking_emitter,
            )

        # Tool-enabled path
        return await self._chat_with_tool_loop(
            messages, system, temperature, max_tokens, tools,
            thinking_emitter=thinking_emitter,
        )

    async def _chat_simple(
        self, messages, system, temperature, max_tokens, json_mode=False,
        thinking_emitter=None,
    ) -> str:
        """Simple chat without tools — original behavior."""
        if self.provider == LLMProvider.GEMINI:
            return await self._chat_gemini(
                messages, system, temperature, max_tokens,
                json_mode=json_mode, thinking_emitter=thinking_emitter,
            )
        elif self.provider == LLMProvider.ANTHROPIC:
            return await self._chat_anthropic(messages, system, temperature, max_tokens)
        else:
            return await self._chat_openai(messages, system, temperature, max_tokens)

    # ── Tool Loop ─────────────────────────────────────────────────

    async def _chat_with_tool_loop(
        self,
        messages: list[dict],
        system: str,
        temperature: float,
        max_tokens: int,
        tools: "ToolRegistry",
        thinking_emitter=None,
    ) -> str:
        """Multi-turn tool loop: LLM calls tools, we execute, feed back results.

        Returns the final text response after all tool calls are resolved.

        ``thinking_emitter`` receives a ``tool_call`` event before each
        execution and a ``tool_result`` event with success / latency
        after, so the desktop's live thinking panel can show the loop
        as it unfolds rather than only the final text.
        """
        import time as _time

        from nexus_core.tools.base import ToolCall

        tool_defs = tools.get_definitions()
        logger.info(
            "Entering tool loop with %d tool(s): %s",
            len(tool_defs), [t["name"] for t in tool_defs],
        )
        # Maintain a working copy of messages for the tool loop
        working_messages = list(messages)
        tool_calls_log: list[dict] = []

        for round_num in range(MAX_TOOL_ROUNDS):
            # Call LLM with tools
            response = await self._call_with_tools(
                working_messages, system, temperature, max_tokens, tool_defs,
                thinking_emitter=thinking_emitter,
            )

            # Check if LLM wants to call tools
            if not response.get("tool_calls"):
                # LLM produced a text response — we're done.
                # #97 defence: if the text is empty BUT we executed
                # tools earlier, surface the last successful tool's
                # output as the reply. Otherwise a chain of tool calls
                # ends with a blank assistant bubble and the user has
                # no idea what happened. Caller (llm_gateway) still
                # has its own empty-reply guard for the no-tool case.
                text = response.get("text", "")

                # #97 round-3: mid-recipe stop nudge. When the LLM
                # called delegate() one or more times AND then went
                # silent (no text, no further call), it's usually
                # "decision fatigue" — Gemini decided 2 of 5 steps was
                # enough. Try a single re-prompt: append a system-style
                # nudge to the messages and call the LLM once more. If
                # it still stops, fall through to the synth-from-tool
                # fallback below.
                delegate_calls = [
                    t for t in tool_calls_log if t.get("tool") == "delegate"
                ]
                if (
                    not text.strip()
                    and delegate_calls
                    and round_num < MAX_TOOL_ROUNDS - 1
                ):
                    logger.warning(
                        "LLM stopped after %d delegate() call(s) with no "
                        "text — injecting continue nudge and retrying.",
                        len(delegate_calls),
                    )
                    working_messages.append({
                        "role": "user",
                        "content": (
                            "(System nudge: you called delegate() "
                            f"{len(delegate_calls)} time(s) and then stopped. "
                            "If the workflow recipe has more steps, call "
                            "delegate() for the NEXT step now. If you "
                            "completed all steps, write the final "
                            "deliverable as your text reply — copying the "
                            "last sub-agent's output verbatim is fine. "
                            "Empty replies mid-recipe are bugs.)"
                        ),
                    })
                    continue  # re-enter the loop for one more round

                if not text.strip() and tool_calls_log:
                    last_ok = next(
                        (t for t in reversed(tool_calls_log) if t.get("success")),
                        None,
                    )
                    if last_ok is not None:
                        logger.warning(
                            "LLM returned empty text after %d tool call(s); "
                            "synthesising reply from last successful tool '%s'",
                            len(tool_calls_log), last_ok["tool"],
                        )
                        return (
                            "(The agent finished its tool loop without "
                            "writing a summary. Last tool result below:)\n\n"
                            + str(last_ok.get("result", ""))
                        )
                return text

            # Execute each tool call
            for tc in response["tool_calls"]:
                tool_call = ToolCall(
                    id=tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                    name=tc["name"],
                    arguments=tc.get("arguments", {}),
                )

                logger.info("Tool call [round %d]: %s(%s)", round_num + 1, tool_call.name, tool_call.arguments)
                if thinking_emitter is not None:
                    try:
                        thinking_emitter.emit(
                            "tool_call", f"Calling {tool_call.name}",
                            content=str(tool_call.arguments)[:200],
                            metadata={
                                "tool": tool_call.name,
                                "round": round_num + 1,
                                "arguments": tool_call.arguments,
                            },
                        )
                    except Exception as e:
                        logger.debug("emitting tool_call event failed: %s", e)
                tool_t0 = _time.time()
                try:
                    result = await asyncio.wait_for(
                        tools.execute(tool_call),
                        timeout=PER_TOOL_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    # Treat a timed-out tool the same as a failed one
                    # so the LLM gets a clean signal and chooses a
                    # different path. Without this the call would hang
                    # the whole chat surface — exactly the symptom that
                    # first surfaced as "Agent is thinking…" forever
                    # when `npx`/marketplace installs got stuck.
                    from nexus_core.tools.base import ToolResult
                    result = ToolResult(
                        success=False,
                        output=(
                            f"Tool {tool_call.name} timed out after "
                            f"{PER_TOOL_TIMEOUT_SECONDS:.0f}s. Try a different "
                            f"approach or ask the user to do it manually."
                        ),
                    )
                if thinking_emitter is not None:
                    try:
                        thinking_emitter.emit(
                            "tool_result",
                            f"{tool_call.name} {'returned' if result.success else 'failed'}",
                            content=result.to_str()[:200],
                            metadata={
                                "tool": tool_call.name,
                                "success": result.success,
                            },
                            duration_ms=int((_time.time() - tool_t0) * 1000),
                        )
                    except Exception as e:
                        logger.debug("emitting tool_result event failed: %s", e)
                tool_calls_log.append({
                    "tool": tool_call.name,
                    "args": tool_call.arguments,
                    "result": result.to_str()[:500],
                    "success": result.success,
                })

                # Append tool call + result to working messages
                # Each provider has its own format — _call_with_tools handles normalization
                working_messages.append({
                    "role": "assistant",
                    "tool_calls": [tc],
                })
                working_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.name,
                    "content": result.to_str(),
                })

            # Continue loop — LLM may want more tool calls or produce final text

        # Hit max rounds — force a text response
        logger.warning("Tool loop hit max rounds (%d)", MAX_TOOL_ROUNDS)
        return response.get("text", "[Tool loop reached maximum rounds]")

    async def _call_with_tools(
        self,
        messages: list[dict],
        system: str,
        temperature: float,
        max_tokens: int,
        tool_defs: list[dict],
        thinking_emitter=None,
    ) -> dict:
        """Call LLM with tool definitions. Returns unified response format.

        Returns:
            {
                "text": "response text" or "",
                "tool_calls": [{"id": "...", "name": "...", "arguments": {...}}, ...] or []
            }
        """
        if self.provider == LLMProvider.GEMINI:
            return await self._call_gemini_tools(
                messages, system, temperature, max_tokens, tool_defs,
                thinking_emitter=thinking_emitter,
            )
        elif self.provider == LLMProvider.ANTHROPIC:
            return await self._call_anthropic_tools(messages, system, temperature, max_tokens, tool_defs)
        else:
            return await self._call_openai_tools(messages, system, temperature, max_tokens, tool_defs)

    # ── Provider-specific tool implementations ────────────────────

    async def _call_gemini_tools(
        self, messages, system, temperature, max_tokens, tool_defs,
        thinking_emitter=None,
    ) -> dict:
        """Gemini function calling.

        Key implementation details:
          - Parameters must be converted from JSON Schema dicts to types.Schema
            objects — Gemini silently ignores raw dicts, causing the model to
            generate text about tools instead of actually calling them.
          - tool_config=AUTO tells Gemini it MAY call functions (default behavior
            can be text-only on some model versions).
          - When ``thinking_emitter`` is set, we enable Gemini's
            ``include_thoughts`` so reasoning tokens come back in
            separate parts; we forward them as ``reasoning`` events.
        """
        import asyncio

        from google.genai import types

        # Convert tool definitions to Gemini format with proper Schema objects
        gemini_tools = []
        for td in tool_defs:
            schema = self._json_schema_to_gemini(td["parameters"])
            gemini_tools.append(types.FunctionDeclaration(
                name=td["name"],
                description=td["description"],
                parameters=schema,
            ))
            logger.debug("Gemini tool registered: %s", td["name"])

        # Convert messages to Gemini format (filter out tool-loop messages)
        contents = self._messages_to_gemini_contents(messages)

        gen_kwargs = dict(
            system_instruction=system or None,
            temperature=temperature,
            max_output_tokens=max_tokens,
            tools=[types.Tool(function_declarations=gemini_tools)],
            # AUTO = model decides whether to call a function or respond with text
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="AUTO",
                ),
            ),
        )
        if thinking_emitter is not None:
            try:
                gen_kwargs["thinking_config"] = types.ThinkingConfig(
                    include_thoughts=True,
                )
            except Exception as exc:
                logger.debug("ThinkingConfig unavailable: %s", exc)
        config = types.GenerateContentConfig(**gen_kwargs)

        def _call():
            try:
                response = self._client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=config,
                )
                return response
            except Exception as e:
                logger.error("Gemini tool call failed: %s", e)
                raise

        loop = asyncio.get_event_loop()
        try:
            response = await asyncio.wait_for(
                loop.run_in_executor(None, _call),
                timeout=LLM_CALL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Gemini tool call timed out after %.0fs",
                LLM_CALL_TIMEOUT_SECONDS,
            )
            return {"text": "", "tool_calls": []}

        # Parse Gemini response for tool calls or text
        if not response.candidates:
            logger.warning("Gemini returned no candidates (tool call path)")
            return {"text": "", "tool_calls": []}

        candidate = response.candidates[0]
        # Defensive: when Gemini blocks a response (safety filter,
        # citation issue, etc.) candidate.content can be None — and
        # iterating its .parts would NPE inside the chat loop, bubble
        # up as 502 Bad Gateway to the desktop. Treat blocked
        # responses as "no tool calls, no text" so the loop returns
        # cleanly and the LLM caller can see the empty result.
        if candidate.content is None or not getattr(candidate.content, "parts", None):
            finish_reason = getattr(candidate, "finish_reason", None)
            logger.warning(
                "Gemini returned no parts (finish_reason=%s) — likely blocked",
                finish_reason,
            )
            return {"text": "", "tool_calls": []}

        tool_calls = []
        text_parts = []
        thought_parts = []

        for part in candidate.content.parts:
            if getattr(part, "thought", False):
                txt = getattr(part, "text", "") or ""
                if txt:
                    thought_parts.append(txt)
            elif hasattr(part, 'function_call') and part.function_call:
                fc = part.function_call
                args = dict(fc.args) if fc.args else {}
                tool_calls.append({
                    "id": f"gemini_{uuid.uuid4().hex[:8]}",
                    "name": fc.name,
                    "arguments": args,
                })
                logger.info("Gemini requested tool: %s(%s)", fc.name, args)
            elif hasattr(part, 'text') and part.text:
                text_parts.append(part.text)

        if thinking_emitter is not None and thought_parts:
            try:
                joined = "\n\n".join(thought_parts)
                thinking_emitter.emit(
                    "reasoning", "Gemini reasoning",
                    content=joined[:1500],
                    metadata={
                        "model": self.model,
                        "thought_chunks": len(thought_parts),
                        "thought_chars": len(joined),
                    },
                )
            except Exception as e:
                logger.debug("thinking emit failed: %s", e)

        if not tool_calls:
            logger.debug("Gemini chose text response (no tool calls)")

        text_out = "\n".join(text_parts) if text_parts else ""
        # #103: detect MAX_TOKENS truncation + auto-continue. We don't
        # try to continue when there are tool_calls (the LLM was using
        # the tail for a function call, not free text).
        finish_reason = getattr(candidate, "finish_reason", None)
        if _is_max_tokens_truncation(finish_reason) and not tool_calls:
            text_out = await self._auto_continue_gemini_tools(
                messages, system, temperature, max_tokens, tool_defs,
                initial_text=text_out,
                thinking_emitter=thinking_emitter,
            )
        return {
            "text": text_out,
            "tool_calls": tool_calls,
        }

    async def _auto_continue_gemini_tools(
        self, messages, system, temperature, max_tokens, tool_defs,
        *, initial_text: str, thinking_emitter,
    ) -> str:
        """Recursively re-ask Gemini to continue when its previous turn
        was cut off by MAX_TOKENS. Returns the stitched text. Stops
        after MAX_AUTO_CONTINUATIONS attempts or when a continuation
        comes back with a non-truncated finish_reason."""
        stitched = initial_text or ""
        # Build a working message list: original messages + Gemini's
        # truncated assistant turn + a user-style nudge asking it to
        # continue. We keep re-using the SAME _call_gemini_tools entry
        # so the conversation shape stays consistent.
        working = list(messages)
        for attempt in range(MAX_AUTO_CONTINUATIONS):
            logger.warning(
                "Gemini truncated at attempt %d (chars so far=%d) — "
                "auto-continuing…",
                attempt, len(stitched),
            )
            # Append the truncated assistant text + the continuation
            # nudge. Note: we DON'T pass tool_defs for the continuation
            # call — at this point we just want raw text completion.
            working = working + [
                {"role": "assistant", "content": stitched},
                {"role": "user", "content": _CONTINUATION_NUDGE},
            ]
            try:
                result = await self._call_gemini_tools(
                    working, system, temperature, max_tokens,
                    tool_defs=[],  # text-only continuation
                    thinking_emitter=thinking_emitter,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "auto-continue: provider call failed (%s) — bailing "
                    "with marker.", e,
                )
                return stitched + _TRUNCATION_MARKER
            chunk = (result.get("text") or "").lstrip()
            # Strip a duplicate truncation marker — the inner call's
            # last-attempt marker would land mid-stitch.
            if chunk.endswith(_TRUNCATION_MARKER):
                chunk = chunk[: -len(_TRUNCATION_MARKER)]
                stitched += chunk
                # Inner call already exhausted its own budget; append
                # outer marker and return.
                return stitched + _TRUNCATION_MARKER
            stitched += chunk
            # If the inner result didn't terminate with a marker AND
            # there's no obvious truncation cue, we're done.
            if not chunk:
                logger.warning(
                    "auto-continue: empty continuation chunk on attempt "
                    "%d — bailing with marker.", attempt,
                )
                return stitched + _TRUNCATION_MARKER
            # Heuristic: if the chunk is shorter than ~80% of max_tokens-
            # worth of chars, the model probably did finish on this
            # round. Return clean.
            # (~4 chars / token is a rough mid-language average.)
            if len(chunk) < int(max_tokens * 4 * 0.8):
                logger.info(
                    "auto-continue: completed after %d attempt(s), "
                    "total %d chars stitched.",
                    attempt + 1, len(stitched),
                )
                return stitched
        # Exhausted budget.
        logger.warning(
            "auto-continue: hit MAX_AUTO_CONTINUATIONS=%d, still "
            "truncated; appending marker.",
            MAX_AUTO_CONTINUATIONS,
        )
        return stitched + _TRUNCATION_MARKER

    @staticmethod
    def _json_schema_to_gemini(schema: dict) -> dict:
        """Convert a JSON Schema dict to Gemini-compatible schema dict.

        Gemini's FunctionDeclaration.parameters accepts a dict but requires
        OpenAPI-style schema with specific conventions:
          - No 'required' at property level (must be at object level)
          - 'type' values must be uppercase strings: STRING, INTEGER, OBJECT, etc.
          - Nested objects must also follow this format

        Returns a cleaned dict that Gemini's API will interpret correctly.
        """
        TYPE_MAP = {
            "string": "STRING",
            "integer": "INTEGER",
            "number": "NUMBER",
            "boolean": "BOOLEAN",
            "array": "ARRAY",
            "object": "OBJECT",
        }

        def _convert(s: dict) -> dict:
            if not isinstance(s, dict):
                return s

            result = {}
            schema_type = s.get("type", "object")
            result["type"] = TYPE_MAP.get(schema_type, schema_type.upper())

            if "description" in s:
                result["description"] = s["description"]

            if "properties" in s:
                result["properties"] = {
                    k: _convert(v) for k, v in s["properties"].items()
                }

            if "required" in s:
                result["required"] = s["required"]

            if "items" in s:
                result["items"] = _convert(s["items"])

            if "enum" in s:
                result["enum"] = s["enum"]

            return result

        return _convert(schema)

    def _messages_to_gemini_contents(self, messages: list[dict]) -> list:
        """Convert unified messages (including tool results) to Gemini Content format."""
        from google.genai import types

        contents = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            role = msg.get("role", "user")

            if role == "tool":
                # Gemini expects tool results as FunctionResponse in a "user" turn
                # (this is how Gemini's multi-turn function calling works)
                func_responses = []
                while i < len(messages) and messages[i].get("role") == "tool":
                    tmsg = messages[i]
                    func_responses.append(types.Part(
                        function_response=types.FunctionResponse(
                            name=tmsg.get("name", "unknown"),
                            response={"result": tmsg.get("content", "")},
                        )
                    ))
                    i += 1
                contents.append(types.Content(role="user", parts=func_responses))
                continue

            elif role == "assistant" and msg.get("tool_calls"):
                # Assistant message with tool calls → model turn with FunctionCall parts
                fc_parts = []
                for tc in msg["tool_calls"]:
                    fc_parts.append(types.Part(
                        function_call=types.FunctionCall(
                            name=tc["name"],
                            args=tc.get("arguments", {}),
                        )
                    ))
                contents.append(types.Content(role="model", parts=fc_parts))
                i += 1
                continue

            else:
                # Regular text message — and, since #123, optional
                # ``images`` list (each entry: {mime: "image/png",
                # data_b64: "..."}). Images become inline_data parts
                # alongside the text so Gemini sees them as multimodal
                # input. We base64-decode here (instead of letting
                # Gemini do it) because types.Blob.data expects raw
                # bytes; passing a base64 string silently fails.
                gemini_role = "user" if role == "user" else "model"
                content_text = msg.get("content", "")
                images = msg.get("images") or []
                parts: list = []
                if content_text:
                    parts.append(types.Part(text=content_text))
                for img in images:
                    try:
                        import base64 as _b64
                        raw = _b64.b64decode(img["data_b64"])
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "Skipping image part (decode failed): %s", e,
                        )
                        continue
                    parts.append(types.Part(
                        inline_data=types.Blob(
                            mime_type=img.get("mime", "image/png"),
                            data=raw,
                        ),
                    ))
                if parts:
                    contents.append(types.Content(
                        role=gemini_role,
                        parts=parts,
                    ))
                i += 1

        return contents

    async def _call_openai_tools(
        self, messages, system, temperature, max_tokens, tool_defs,
    ) -> dict:
        """OpenAI function calling."""
        # Convert tool definitions to OpenAI format
        openai_tools = []
        for td in tool_defs:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": td["name"],
                    "description": td["description"],
                    "parameters": td["parameters"],
                },
            })

        # Build messages with system prompt
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})

        for msg in messages:
            if msg.get("role") == "tool":
                # OpenAI expects tool results as role=tool with tool_call_id
                full_messages.append({
                    "role": "tool",
                    "tool_call_id": msg.get("tool_call_id", ""),
                    "content": msg.get("content", ""),
                })
            elif msg.get("role") == "assistant" and msg.get("tool_calls"):
                # OpenAI assistant message with tool_calls
                openai_tcs = []
                for tc in msg["tool_calls"]:
                    openai_tcs.append({
                        "id": tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc.get("arguments", {})),
                        },
                    })
                full_messages.append({
                    "role": "assistant",
                    "tool_calls": openai_tcs,
                })
            else:
                full_messages.append({"role": msg["role"], "content": msg.get("content", "")})

        tool_kwargs = {
            "model": self.model,
            "messages": full_messages,
            "max_tokens": max_tokens,
            "tools": openai_tools,
        }
        # Kimi models (e.g. kimi-k2.7-code) reject any temperature
        # other than 1 — omit the parameter for Kimi (API default).
        if self.provider != LLMProvider.KIMI:
            tool_kwargs["temperature"] = temperature
        response = await self._client.chat.completions.create(**tool_kwargs)

        choice = response.choices[0]
        message = choice.message

        # Parse tool calls
        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": args,
                })

        return {
            "text": message.content or "",
            "tool_calls": tool_calls,
        }

    async def _call_anthropic_tools(
        self, messages, system, temperature, max_tokens, tool_defs,
    ) -> dict:
        """Anthropic Claude function calling (tool_use)."""
        # Convert tool definitions to Anthropic format
        anthropic_tools = []
        for td in tool_defs:
            anthropic_tools.append({
                "name": td["name"],
                "description": td["description"],
                "input_schema": td["parameters"],
            })

        # Build messages — Anthropic has specific format for tool results
        api_messages = []
        i = 0
        while i < len(messages):
            msg = messages[i]

            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                # Assistant with tool calls → content blocks
                content_blocks = []
                for tc in msg["tool_calls"]:
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
                        "name": tc["name"],
                        "input": tc.get("arguments", {}),
                    })
                api_messages.append({"role": "assistant", "content": content_blocks})
                i += 1

            elif msg.get("role") == "tool":
                # Anthropic: tool results go in a "user" message with tool_result blocks
                tool_results = []
                while i < len(messages) and messages[i].get("role") == "tool":
                    tmsg = messages[i]
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tmsg.get("tool_call_id", ""),
                        "content": tmsg.get("content", ""),
                    })
                    i += 1
                api_messages.append({"role": "user", "content": tool_results})

            else:
                api_messages.append({"role": msg["role"], "content": msg.get("content", "")})
                i += 1

        response = await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system or "You are a helpful assistant.",
            messages=api_messages,
            tools=anthropic_tools,
        )

        # Parse response — Anthropic returns content blocks
        tool_calls = []
        text_parts = []

        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.input,
                })
            elif block.type == "text":
                text_parts.append(block.text)

        return {
            "text": "\n".join(text_parts) if text_parts else "",
            "tool_calls": tool_calls,
        }

    # ── Simple provider implementations (no tools) ────────────────

    async def _chat_gemini(
        self, messages, system, temperature, max_tokens,
        json_mode: bool = False,
        thinking_emitter=None,
    ) -> str:
        import asyncio

        from google.genai import types

        contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(types.Content(
                role=role,
                parts=[types.Part(text=msg["content"])],
            ))

        # Gemini 2.5 thinking mode: ask the model to reason explicitly
        # and return those thoughts in a separate part of the response.
        # We surface them via the thinking_emitter so the desktop's
        # live cognition panel can show the chain-of-thought in real
        # time. ``include_thoughts=True`` is what causes Gemini to
        # actually emit thought parts (default is False).
        gen_kwargs = dict(
            system_instruction=system or None,
            temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type="application/json" if json_mode else None,
        )
        if thinking_emitter is not None:
            try:
                gen_kwargs["thinking_config"] = types.ThinkingConfig(
                    include_thoughts=True,
                )
            except Exception as exc:
                # Older google-genai versions don't have ThinkingConfig
                # — fall through and treat thinking as best-effort.
                logger.debug("ThinkingConfig unavailable: %s", exc)
        config = types.GenerateContentConfig(**gen_kwargs)

        def _call():
            response = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )
            # Stream out any thought parts to the emitter BEFORE we
            # return — the desktop wants to render reasoning before
            # the final reply lands. Best-effort: a missing field on
            # an older Gemini version just falls through.
            if thinking_emitter is not None:
                try:
                    thoughts = []
                    if hasattr(response, "candidates") and response.candidates:
                        cand = response.candidates[0]
                        # Same defensive check as _call_gemini_tools:
                        # blocked responses set content to None, which
                        # would NPE on .parts and bubble up as 502.
                        if (
                            hasattr(cand, "content")
                            and cand.content is not None
                            and getattr(cand.content, "parts", None)
                        ):
                            for part in cand.content.parts:
                                if getattr(part, "thought", False):
                                    txt = getattr(part, "text", "") or ""
                                    if txt:
                                        thoughts.append(txt)
                    if thoughts:
                        joined = "\n\n".join(thoughts)
                        thinking_emitter.emit(
                            "reasoning", "Gemini reasoning",
                            content=joined[:1500],
                            metadata={
                                "model": self.model,
                                "thought_chunks": len(thoughts),
                                "thought_chars": len(joined),
                            },
                        )
                except Exception as e:
                    logger.debug("thinking emit failed: %s", e)

            # Gemini can return None/empty when blocked by safety filters
            # or when the model has nothing to say. Return empty string
            # so callers can handle it gracefully.
            text = response.text
            finish_reason = None
            if hasattr(response, 'candidates') and response.candidates:
                finish_reason = getattr(
                    response.candidates[0], 'finish_reason', None,
                )
            if text is None:
                if finish_reason and str(finish_reason) not in (
                    'STOP', '1', 'FinishReason.STOP',
                ):
                    logger.debug(
                        "Gemini response blocked: finish_reason=%s", finish_reason
                    )
                return ""
            # Tag the return so the async wrapper knows to run
            # auto-continue. We can't `await` from inside this sync
            # executor body, so we tunnel the signal back as a tuple.
            if _is_max_tokens_truncation(finish_reason):
                return ("__NEXUS_TRUNCATED__", text or "")
            return text

        loop = asyncio.get_event_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _call),
                timeout=LLM_CALL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Gemini call timed out after %.0fs", LLM_CALL_TIMEOUT_SECONDS,
            )
            return ""
        # #103: auto-continue if the sync inner call returned the
        # truncation sentinel. Run up to MAX_AUTO_CONTINUATIONS
        # follow-up calls, stitching each chunk onto the prior text.
        if isinstance(result, tuple) and result and result[0] == "__NEXUS_TRUNCATED__":
            stitched = result[1] or ""
            working = list(messages)
            for attempt in range(MAX_AUTO_CONTINUATIONS):
                logger.warning(
                    "Gemini non-tool path truncated; auto-continue "
                    "attempt %d (chars so far=%d)",
                    attempt, len(stitched),
                )
                working = working + [
                    {"role": "assistant", "content": stitched},
                    {"role": "user", "content": _CONTINUATION_NUDGE},
                ]
                try:
                    inner = await self._chat_gemini(
                        working, system, temperature, max_tokens,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("auto-continue failed: %s", e)
                    return stitched + _TRUNCATION_MARKER
                # inner is either a plain str (clean finish) or a
                # tuple sentinel if THAT call also truncated.
                if isinstance(inner, tuple) and inner and inner[0] == "__NEXUS_TRUNCATED__":
                    chunk = (inner[1] or "").lstrip()
                    if not chunk:
                        return stitched + _TRUNCATION_MARKER
                    stitched += chunk
                    continue  # try one more round
                chunk = (inner or "").lstrip()
                stitched += chunk
                logger.info(
                    "auto-continue (non-tool): done after %d round(s), "
                    "total %d chars.", attempt + 1, len(stitched),
                )
                return stitched
            return stitched + _TRUNCATION_MARKER
        return result

    async def _chat_anthropic(self, messages, system, temperature, max_tokens) -> str:
        text = await self._anthropic_call_once(messages, system, temperature, max_tokens)
        if not (isinstance(text, tuple) and text and text[0] == "__NEXUS_TRUNCATED__"):
            return text
        # #103: auto-continue loop for Anthropic.
        stitched = text[1] or ""
        working = list(messages)
        for attempt in range(MAX_AUTO_CONTINUATIONS):
            logger.warning(
                "Anthropic truncated; auto-continue attempt %d "
                "(chars so far=%d)", attempt, len(stitched),
            )
            working = working + [
                {"role": "assistant", "content": stitched},
                {"role": "user", "content": _CONTINUATION_NUDGE},
            ]
            try:
                inner = await self._anthropic_call_once(
                    working, system, temperature, max_tokens,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("Anthropic auto-continue failed: %s", e)
                return stitched + _TRUNCATION_MARKER
            if isinstance(inner, tuple) and inner and inner[0] == "__NEXUS_TRUNCATED__":
                chunk = (inner[1] or "").lstrip()
                if not chunk:
                    return stitched + _TRUNCATION_MARKER
                stitched += chunk
                continue
            stitched += (inner or "").lstrip()
            return stitched
        return stitched + _TRUNCATION_MARKER

    async def _anthropic_call_once(self, messages, system, temperature, max_tokens):
        """One Anthropic call. Returns plain str on clean finish, or
        a tuple sentinel ``("__NEXUS_TRUNCATED__", text)`` when the
        response hit max_tokens. _chat_anthropic wraps this with the
        auto-continue loop."""
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system or "You are a helpful assistant.",
            messages=messages,
        )
        text = response.content[0].text
        stop_reason = getattr(response, "stop_reason", None)
        if _is_max_tokens_truncation(stop_reason):
            return ("__NEXUS_TRUNCATED__", text or "")
        return text

    async def _chat_openai(self, messages, system, temperature, max_tokens) -> str:
        text = await self._openai_call_once(messages, system, temperature, max_tokens)
        if not (isinstance(text, tuple) and text and text[0] == "__NEXUS_TRUNCATED__"):
            return text
        # #103: auto-continue loop for OpenAI.
        stitched = text[1] or ""
        working = list(messages)
        for attempt in range(MAX_AUTO_CONTINUATIONS):
            logger.warning(
                "OpenAI truncated; auto-continue attempt %d "
                "(chars so far=%d)", attempt, len(stitched),
            )
            working = working + [
                {"role": "assistant", "content": stitched},
                {"role": "user", "content": _CONTINUATION_NUDGE},
            ]
            try:
                inner = await self._openai_call_once(
                    working, system, temperature, max_tokens,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("OpenAI auto-continue failed: %s", e)
                return stitched + _TRUNCATION_MARKER
            if isinstance(inner, tuple) and inner and inner[0] == "__NEXUS_TRUNCATED__":
                chunk = (inner[1] or "").lstrip()
                if not chunk:
                    return stitched + _TRUNCATION_MARKER
                stitched += chunk
                continue
            stitched += (inner or "").lstrip()
            return stitched
        return stitched + _TRUNCATION_MARKER

    async def _openai_call_once(self, messages, system, temperature, max_tokens):
        """Single OpenAI call. Plain str on clean finish, tuple
        sentinel on max_tokens truncation. _chat_openai wraps with the
        auto-continue loop."""
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)
        once_kwargs = {
            "model": self.model,
            "messages": full_messages,
            "max_tokens": max_tokens,
        }
        # Kimi models (e.g. kimi-k2.7-code) reject any temperature
        # other than 1 with 400 "invalid temperature" — omit the
        # parameter and let the API use the model default.
        if self.provider != LLMProvider.KIMI:
            once_kwargs["temperature"] = temperature
        response = await self._client.chat.completions.create(**once_kwargs)
        choice = response.choices[0]
        text = choice.message.content
        finish_reason = getattr(choice, "finish_reason", None)
        if _is_max_tokens_truncation(finish_reason):
            return ("__NEXUS_TRUNCATED__", text or "")
        return text

    async def complete(
        self,
        prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        """Single-turn completion for evolution prompts.

        Does NOT use json_mode — Gemini's json_mode (response_mime_type=
        "application/json") causes output truncation at ~200-300 chars on
        some prompts. Since all callers already use _robust_json_parse()
        which handles markdown fences and prose wrapping, plain text mode
        is both more reliable and produces longer, complete responses.

        max_tokens defaults to 4096 (up from 2048) to give Gemini enough
        room for skill detection responses that enumerate multiple skills.
        """
        return await self.chat(
            messages=[{"role": "user", "content": prompt}],
            system="You are a precise JSON extraction engine. Return only valid JSON, no markdown fences.",
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=False,
        )

    async def close(self):
        if self._client and hasattr(self._client, "close"):
            await self._client.close()
        self._client = None
