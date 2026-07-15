"""#169 — ``defer_to_background`` tool.

When the medic asks for something that's going to take a while (multi-
step workflow, deep research, segment-all-slices), the agent calls
this tool. The tool persists a row to the async_tasks table and
returns immediately with a confirmation string the agent can
paraphrase. A background worker (see async_tasks._worker_loop) picks
up the task, drives twin.chat with the action prompt, emails the
result, and writes a completion card into the chat session.

This is the opposite of the ``delegate`` tool:
  - ``delegate(skill, task)``   → inline, blocking, single LLM call
  - ``defer_to_background(...)`` → out-of-band, parallel, multi-turn

When the agent SHOULD use defer:
  - Any task expected to take >60 s (workflow_run with 3+ steps,
    document-heavy research, RT contour propagation across 1000+
    slices).
  - User asks for "draft a report and email it to me" — the
    expectation is async-by-design.
  - User explicitly says "处理完邮件通知我" / "I'll be afk for an hour".

When NOT to use:
  - Single-question Q&A that returns in <5 s.
  - Anything where the medic needs follow-up clarification on the
    next turn (defer assumes one-shot completion).
"""

from __future__ import annotations

import logging

from nexus_core.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class DeferToBackgroundTool(BaseTool):
    """Agent-callable tool to schedule a long-running task for
    background execution + email notification on completion."""

    def __init__(self, user_id: str, default_session_id: str = ""):
        self._user_id = user_id
        self._default_session_id = default_session_id

    @property
    def name(self) -> str:
        return "defer_to_background"

    @property
    def description(self) -> str:
        return (
            "Schedule a long-running task to execute in the background "
            "and email the result to the user when done. Use this when "
            "the user asks for work that will take longer than ~60 "
            "seconds — multi-step workflows, deep research, "
            "segmentation across hundreds of slices, etc.\n"
            "\n"
            "⚠️ CRITICAL — MUST EMIT FUNCTION_CALL, NEVER NARRATE.\n"
            "  When the user asks for work + email notification ("
            "'帮我跑 X workflow，做完邮件通知' / 'work on this and "
            "email me when done' / similar) you MUST emit a real "
            "function_call to this tool in the SAME turn. Writing "
            "the words '我会跑一下，做完邮件通知你' or 'I'll work "
            "on this' WITHOUT also emitting the function_call is a "
            "HALLUCINATION — the task never actually starts and the "
            "user keeps waiting for an email that never arrives. "
            "This is the #1 failure mode for this tool. If you find "
            "yourself ABOUT to write narrative confirmation text, "
            "STOP and call this tool first. The user's chat will "
            "auto-render your confirmation + a task card from your "
            "function_call return value.\n"
            "\n"
            "After the function call returns, you may add a short "
            "natural-language confirmation like '好，预计 ~5min 完成 "
            "后邮件 + 这里通知你'. NOT before. NOT instead of.\n"
            "\n"
            "DO NOT use this tool for:\n"
            "  - Quick questions (<5s) — answer inline.\n"
            "  - Tasks where the next user reply is needed to "
            "clarify — defer assumes one-shot completion.\n"
            "  - Tasks the user explicitly says they want to watch "
            "happen ('show me each step' / 'let me see your "
            "thinking') — run inline so cognition panel renders."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": (
                        "Human-readable label for what the task is, "
                        "in 1 sentence. Used as the email subject and "
                        "the completion card title. Example: 'Run "
                        "research-brief workflow on the GLP-1 / "
                        "cardiovascular outcomes question'."
                    ),
                },
                "action_prompt": {
                    "type": "string",
                    "description": (
                        "The literal prompt the worker will hand BACK "
                        "to the agent on a fresh turn to actually do "
                        "the work. Write it as if YOU (the agent) "
                        "are reading it cold and starting from "
                        "scratch — include any context the agent will "
                        "need (file_ids referenced, workflow name to "
                        "invoke, etc.). Example: 'Run the "
                        "research-brief workflow on this question: "
                        "<question>. Use list_workflows + delegate '"
                        "to traverse the recipe end-to-end.'"
                    ),
                },
                "eta_minutes": {
                    "type": "integer",
                    "description": (
                        "Rough estimate of how long the work will "
                        "take, in minutes. Used only in the user-"
                        "facing confirmation text. 5 is a reasonable "
                        "default for most workflows."
                    ),
                },
                "email_to": {
                    "type": "string",
                    "description": (
                        "Email address to send the result to. Use the "
                        "user's known email; if you don't know it, "
                        "leave empty and the chat-only completion "
                        "card will still fire."
                    ),
                },
            },
            "required": ["description", "action_prompt"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        description = (kwargs.get("description") or "").strip()
        action_prompt = (kwargs.get("action_prompt") or "").strip()
        eta_minutes = int(kwargs.get("eta_minutes") or 5)
        email_to = (kwargs.get("email_to") or "").strip()

        if not description or not action_prompt:
            return ToolResult(
                success=False,
                error=(
                    "defer_to_background requires both "
                    "'description' and 'action_prompt'."
                ),
            )

        # Auto-resolve the user's email when the model didn't supply
        # one. We look at the user_id (which IS the email in our
        # setup) and use it as the default destination.
        if not email_to:
            email_to = self._user_id   # user_id == email in this product

        # Persist the task. Worker loop picks it up within ~3 s.
        try:
            from nexus_server.async_tasks import enqueue_task
            task_id = enqueue_task(
                user_id=self._user_id,
                session_id=self._default_session_id,
                description=description,
                action_prompt=action_prompt,
                eta_seconds=eta_minutes * 60,
                email_to=email_to,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("defer_to_background enqueue failed: %s", e)
            return ToolResult(
                success=False,
                error=f"Failed to schedule background task: {e}",
            )

        # Return a confirmation the agent can paraphrase. We
        # deliberately don't return the task_id — keeps the agent's
        # natural-language reply medic-friendly.
        return ToolResult(
            success=True,
            output=(
                f"OK — scheduled background task "
                f"'{description[:60]}'. ETA ~{eta_minutes} min. "
                f"Will email completion to {email_to or 'chat-only'}."
            ),
        )


def register_async_tools(twin, user_id: str) -> None:
    """Twin-manager registrar — mirrors the signature of the existing
    register_subagent_tools / register_workflow_tools so it slots
    into the _USER_SCOPED_TOOL_REGISTRARS tuple without ceremony."""
    try:
        twin.tools.register(DeferToBackgroundTool(user_id=user_id))
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "register_async_tools: failed to register tool for %s: %s",
            user_id, e,
        )
