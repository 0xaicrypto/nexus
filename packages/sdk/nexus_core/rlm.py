"""Recursive Language Model (RLM) primitive.

Implements the RLM inference paradigm from Zhang, Kraska & Khattab,
*Recursive Language Models* (arXiv:2512.24601, Dec 2025): instead
of feeding a long prompt directly into the LLM, load it as a
variable inside a Python REPL-style sandbox and let the model
*write code* to peek at, slice, and recursively sub-call itself
over snippets.

The paper proves this approach handles inputs ~2 orders of
magnitude beyond the base LLM's context window while costing the
same or less per query.

Why this lives in the SDK
=========================

RLM is a *general* runtime pattern with multiple Nexus consumers:

* **Chat projection** — turning an unbounded EventLog into a
  task-conditioned context string fed to the chat LLM
  (replaces / augments the single-call ``π(events, task, budget)``
  projection in DPM).
* **Verdict scorer** (Phase O.4) — counting ``task_kind``
  occurrences over an observation window that may exceed the LLM's
  context window.
* **Attachment processing** — letting the LLM seek into a 100MB
  PDF only when the chat actually needs that section.
* **Knowledge / skill search** — querying over a large registry
  without pre-indexing.

All four use the same primitive: *give the LLM REPL access to
something big, let it slice and sub-call*. Building one good
implementation here keeps the rest of Nexus thin.

Determinism caveat
==================

RLM trajectories are *stochastic* — the root LLM may write
different code on different runs even with ``temperature=0``. This
is fine for chat projection (best-effort), unsafe for chain anchor
(must be reproducible). The DPM contract therefore splits:

* **Chain anchor path** uses compile-time chunked manifests
  (BEP-Nexus §3) — deterministic.
* **Chat projection path** uses RLM — stochastic, higher quality.

If a future version needs RLM-with-replay (e.g. for cross-runtime
DPM), record the trajectory (code + sub-LM I/O pairs) into the
EventLog so a different runtime can replay deterministically.
That's tracked in ``docs/design/nexus-architecture.md``.

Sandbox model
=============

``RLMRunner`` runs root-LLM-emitted code in an *async-aware*
``exec()`` sandbox with a controlled globals dict. This is **not**
a security boundary against a hostile model — for production
deployments handling untrusted code, swap to RestrictedPython /
WASM / docker-per-call. For the trusted path (your own twin's
projection), the simple sandbox is fine.

Hard limits in the runner are coarse (max_iterations,
max_sub_calls, timeout) — the goal is bounded cost, not security.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import dataclasses
import io
import re
import textwrap
import time
from typing import Any, Awaitable, Callable, Optional

# ── Public types ──────────────────────────────────────────────────────


@dataclasses.dataclass
class RLMConfig:
    """Knobs for an :class:`RLMRunner`. All fields have defaults
    chosen for the chat-projection use case; tune per call site.
    """

    #: Maximum root-LLM iterations. Each iteration = one round-trip
    #: where the root LLM emits a code block and gets exec feedback.
    max_iterations: int = 10

    #: Maximum total sub-LLM calls across all iterations of a run.
    #: Cap prevents runaway recursion.
    max_sub_calls: int = 20

    #: Wall-clock timeout for the whole run, in seconds.
    timeout_seconds: float = 60.0

    #: Token budget hint passed to the root LLM in the system
    #: prompt. The runner doesn't enforce this — it's a hint to
    #: encourage the model to slice rather than dump.
    target_output_tokens: int = 2000

    #: Recursion depth limit. The paper found depth=1 (sub-LM is
    #: a plain LLM, not another RLM) sufficient for OOLONG-class
    #: tasks; deeper recursion is future work.
    max_recursion_depth: int = 1


@dataclasses.dataclass
class TrajectoryEntry:
    """One iteration of the RLM loop — root LLM said this, we
    executed it, this is what happened."""

    iteration: int
    raw_response: str           # what the root LLM emitted
    code: str                   # extracted Python code (may be empty)
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None  # exception message if exec raised
    sub_calls: int = 0          # sub-LM calls made inside THIS iteration


@dataclasses.dataclass
class RLMResult:
    """Outcome of an RLM run."""

    #: The final output the root LLM committed via ``_set_result(...)``.
    #: Empty string if the run hit a limit before resolving.
    output: str

    #: Ordered list of every iteration's behaviour. Useful for
    #: debugging, auditing, and (one day) deterministic replay.
    trajectory: list[TrajectoryEntry]

    iterations_used: int
    sub_calls_used: int

    #: True iff the run hit a hard limit (max_iterations,
    #: max_sub_calls, or timeout) before the root LLM called
    #: ``_set_result``.
    truncated: bool

    #: True iff the run exited because of an exec exception that
    #: the root LLM didn't recover from.
    crashed: bool = False

    elapsed_seconds: float = 0.0


# ── System prompt ─────────────────────────────────────────────────────


_DEFAULT_SYSTEM_PROMPT = """\
You are running inside a Recursive Language Model (RLM) loop. Each
turn you emit ONE Python code block. The code runs in a persistent
REPL — variables and imports survive across turns.

Available tools (already imported as globals):
  * Standard Python (re, json, math, statistics, collections, …).
  * `await _sub_llm(query: str) -> str` — call a cheaper LLM on a
    snippet. Use this to summarise, classify, or extract from
    pieces of context. Each call costs budget; do NOT loop over
    the whole input naively.
  * `_set_result(value: str) -> None` — emit your final answer
    and terminate the loop. Call this when ready.

Available context variables: see the user message below.

Patterns that work well:
  1. Slice / regex / filter the variable in code FIRST to narrow
     down to relevant pieces.
  2. Sub-LM-call those pieces with focused queries.
  3. Stitch sub-LM outputs into a final result.

Hard limits — your run terminates early if you hit any:
  * max iterations
  * max sub-LM calls
  * wall-clock timeout

Always emit exactly one ```python code block per turn. Do NOT
include explanatory prose outside the block — anything outside is
ignored.
"""


# ── Code-block extraction ─────────────────────────────────────────────


_CODE_BLOCK_RE = re.compile(
    r"```(?:python)?\s*\n(.*?)```",
    re.DOTALL,
)


def extract_code_block(text: str) -> str:
    """Pull the first fenced ```python ... ``` block out of LLM output.

    Returns the empty string if none found — callers SHOULD treat
    that as a no-op iteration (don't crash, but trajectory entry
    captures the empty code so audit shows what happened).
    """
    if not text:
        return ""
    m = _CODE_BLOCK_RE.search(text)
    if m:
        return m.group(1).strip()
    # Fallback: if the whole thing looks like code (no ``` fences),
    # treat it as a single block. This is forgiving for cheaper
    # LLMs that don't reliably fence.
    stripped = text.strip()
    if stripped.startswith(("import ", "from ", "_set_result", "if ", "for ", "result")):
        return stripped
    return ""


# ── Async exec ────────────────────────────────────────────────────────


@dataclasses.dataclass
class _ExecOutcome:
    stdout: str
    stderr: str
    error: Optional[str] = None


def _collect_top_level_assigned_names(tree: "ast.Module") -> set[str]:
    """Walk the AST module body, collect every name that gets bound
    at the top level (assignments, for-targets, with-as, function /
    class defs, imports). We then declare those as ``global`` inside
    the async wrapper so the user's REPL semantics — bind once,
    visible in next iteration — actually work.
    """
    import ast

    assigned: set[str] = set()

    def _collect(target):
        if isinstance(target, ast.Name):
            assigned.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                _collect(elt)
        elif isinstance(target, ast.Starred):
            _collect(target.value)
        # Subscript / Attribute don't introduce new bindings — skip.

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                _collect(t)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            _collect(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _collect(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    _collect(item.optional_vars)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            assigned.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assigned.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue  # can't enumerate
                assigned.add(alias.asname or alias.name)

    return assigned


async def _exec_in_sandbox(
    code: str, globals_dict: dict, timeout_s: float
) -> _ExecOutcome:
    """Execute ``code`` inside ``globals_dict`` with stdout/stderr
    capture, async ``await`` support, and a timeout.

    Implementation note. We can't just ``exec(code)`` at module
    level because the code may contain ``await _sub_llm(...)`` —
    top-level await is allowed by the ``PyCF_ALLOW_TOP_LEVEL_AWAIT``
    flag in CPython, but then we have to ``eval()`` the resulting
    coroutine code object, and getting the right code-object flags
    to detect that case is fragile across Python versions.

    Cleaner: wrap user code in ``async def __rlm_main__(): …`` and
    ``await __rlm_main__()``. That naturally supports ``await``.
    But it changes scope: assignments inside the function are
    *local*, not visible to the next iteration's exec.

    Solution: AST-rewrite. Walk the user's top-level statements,
    find every name they bind (``a = …``, ``for a in …``,
    ``import a``, ``def a(…)``, etc.), and prepend a ``global a, b,
    c, …`` declaration inside the wrapper. Now assignments still
    happen lexically inside the function but bind to
    ``globals_dict`` — i.e. survive across iterations.
    """
    if not code.strip():
        return _ExecOutcome(stdout="", stderr="(no code)")

    import ast

    code = textwrap.dedent(code)

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()

    # Parse + collect bindings BEFORE wrapping, so SyntaxError in
    # user code surfaces here (not buried inside the wrapped form
    # where line numbers would be off-by-one).
    try:
        tree = ast.parse(code, "<rlm-iter>", mode="exec")
    except SyntaxError as e:
        return _ExecOutcome(
            stdout="",
            stderr="",
            error=f"SyntaxError: {e.msg} (line {e.lineno})",
        )

    bound_names = _collect_top_level_assigned_names(tree)

    # Build the wrapper: `async def __rlm_main__():\n  global a, b, ...\n  <user code>`
    if bound_names:
        global_decl = f"    global {', '.join(sorted(bound_names))}\n"
    else:
        global_decl = ""
    indented = textwrap.indent(code, "    ")
    wrapped = f"async def __rlm_main__():\n{global_decl}{indented}\n"

    try:
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            exec(compile(wrapped, "<rlm-iter>", "exec"), globals_dict)
            coro = globals_dict["__rlm_main__"]()
            await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.TimeoutError:
        return _ExecOutcome(
            stdout=stdout_buf.getvalue(),
            stderr=stderr_buf.getvalue(),
            error=f"timeout after {timeout_s}s",
        )
    except Exception as e:  # noqa: BLE001 — surface any error in trajectory
        return _ExecOutcome(
            stdout=stdout_buf.getvalue(),
            stderr=stderr_buf.getvalue(),
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        # Clean up the wrapper from globals so trajectory inspection
        # / replay sees the user's bindings, not our scaffolding.
        globals_dict.pop("__rlm_main__", None)

    return _ExecOutcome(
        stdout=stdout_buf.getvalue(),
        stderr=stderr_buf.getvalue(),
    )


# ── Runner ────────────────────────────────────────────────────────────


#: Type alias: an async LLM callable. Takes a list of chat-style
#: messages + an optional system prompt, returns the assistant's
#: text response.
LLMCallable = Callable[[list[dict], str], Awaitable[str]]

#: Type alias: an async sub-LLM callable. Takes a single string
#: query, returns the response. (Single-string sig keeps the
#: surface the root LLM sees minimal — it's all it needs.)
SubLLMCallable = Callable[[str], Awaitable[str]]


class RLMRunner:
    """Run a Recursive Language Model loop.

    Usage::

        runner = RLMRunner(root_llm=my_root_lm, sub_llm=my_sub_lm)
        result = await runner.run(
            task="Find every mention of 'Tokyo' in the EventLog "
                 "and summarise the user's plans.",
            context_vars={"events": event_log_as_list},
        )
        print(result.output)
    """

    def __init__(
        self,
        root_llm: LLMCallable,
        sub_llm: Optional[SubLLMCallable] = None,
        config: Optional[RLMConfig] = None,
        system_prompt: Optional[str] = None,
    ):
        """
        Args:
            root_llm: Async callable that takes ``(messages, system)``
                and returns the assistant's text. The "smart" model.
            sub_llm: Async callable that takes a query string and
                returns text. The "cheap" model used inside the
                code the root LLM writes. Pass ``None`` to disable
                recursion (root LLM can still slice the context
                programmatically — useful as the "no sub-calls"
                ablation from the paper).
            config: Knobs (defaults are reasonable for chat
                projection).
            system_prompt: Override the default RLM system prompt.
                Provide your own when you want task-specific
                guidance to the root LLM.
        """
        self.root_llm = root_llm
        self.sub_llm = sub_llm
        self.config = config or RLMConfig()
        self.system_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT

    async def run(
        self,
        task: str,
        context_vars: Optional[dict[str, Any]] = None,
    ) -> RLMResult:
        """Execute the RLM loop.

        Args:
            task: What the root LLM should accomplish. Goes into
                the first user message. Should be specific — the
                root LLM uses this to decide what to slice / probe.
            context_vars: Variables loaded into the REPL globals
                before iteration starts. The root LLM can name any
                of these in its code.

        Returns:
            :class:`RLMResult` capturing the output + full trajectory.
        """
        start_time = time.monotonic()
        ctx = dict(context_vars or {})

        sub_call_count = 0
        result_holder: list[Optional[str]] = [None]

        # Wire sub_llm + set_result into the sandbox
        async def _sub_llm_wrapper(query: str) -> str:
            nonlocal sub_call_count
            if self.sub_llm is None:
                raise RuntimeError(
                    "sub_llm is disabled in this RLMRunner — only the root "
                    "LLM may decide; recursion is unavailable."
                )
            if sub_call_count >= self.config.max_sub_calls:
                raise RuntimeError(
                    f"sub_llm budget exhausted ({self.config.max_sub_calls} calls)"
                )
            sub_call_count += 1
            return await self.sub_llm(query)

        def _set_result(value: Any) -> None:
            result_holder[0] = str(value)

        # Build sandbox globals
        sandbox_globals: dict[str, Any] = {
            "__builtins__": __builtins__,
            "_sub_llm": _sub_llm_wrapper,
            "_set_result": _set_result,
            **ctx,
        }

        # Build initial conversation
        var_summary = _summarise_context_vars(ctx)
        first_user_msg = (
            f"Task: {task}\n\n"
            f"Context variables available in the REPL:\n{var_summary}\n\n"
            f"Limits: max_iterations={self.config.max_iterations}, "
            f"max_sub_calls={self.config.max_sub_calls}, "
            f"timeout={self.config.timeout_seconds:.0f}s, "
            f"target_output_tokens={self.config.target_output_tokens}.\n\n"
            "Emit ONE Python code block per turn."
        )
        messages: list[dict] = [{"role": "user", "content": first_user_msg}]

        trajectory: list[TrajectoryEntry] = []
        crashed = False

        for i in range(self.config.max_iterations):
            sub_calls_at_start = sub_call_count

            # Wall-clock check
            elapsed = time.monotonic() - start_time
            if elapsed >= self.config.timeout_seconds:
                break

            response = await self.root_llm(messages, self.system_prompt)
            code = extract_code_block(response)

            entry = TrajectoryEntry(
                iteration=i,
                raw_response=response,
                code=code,
            )

            # Per-iteration timeout = remaining wall-clock budget,
            # capped so a single iteration can't burn the whole budget.
            iter_timeout = max(
                1.0,
                min(
                    self.config.timeout_seconds - elapsed,
                    self.config.timeout_seconds * 0.5,
                ),
            )
            outcome = await _exec_in_sandbox(code, sandbox_globals, iter_timeout)
            entry.stdout = outcome.stdout
            entry.stderr = outcome.stderr
            entry.error = outcome.error
            entry.sub_calls = sub_call_count - sub_calls_at_start
            trajectory.append(entry)

            # Termination via _set_result
            if result_holder[0] is not None:
                return RLMResult(
                    output=result_holder[0],
                    trajectory=trajectory,
                    iterations_used=i + 1,
                    sub_calls_used=sub_call_count,
                    truncated=False,
                    crashed=False,
                    elapsed_seconds=time.monotonic() - start_time,
                )

            # Hit sub-call budget mid-iteration → keep going one
            # more iter so the root LLM can recover gracefully
            # (call _set_result with what it has so far).

            # Append exec feedback to conversation
            messages.append({"role": "assistant", "content": response})
            messages.append({
                "role": "user",
                "content": _format_exec_feedback(entry, sub_call_count, self.config.max_sub_calls),
            })

        # Loop fell out without _set_result being called
        return RLMResult(
            output=result_holder[0] or "",
            trajectory=trajectory,
            iterations_used=len(trajectory),
            sub_calls_used=sub_call_count,
            truncated=True,
            crashed=crashed,
            elapsed_seconds=time.monotonic() - start_time,
        )


# ── Helpers ───────────────────────────────────────────────────────────


def _summarise_context_vars(ctx: dict[str, Any]) -> str:
    """One-line summary of each context variable for the system
    prompt. Avoids dumping huge values in the prompt itself — the
    whole point of RLM is to NOT pre-load the data into the model.
    """
    if not ctx:
        return "  (none)"
    lines = []
    for k, v in ctx.items():
        if isinstance(v, str):
            lines.append(f"  {k}: str of length {len(v)}")
        elif isinstance(v, (list, tuple)):
            lines.append(f"  {k}: {type(v).__name__} of length {len(v)}")
        elif isinstance(v, dict):
            lines.append(f"  {k}: dict with {len(v)} keys")
        else:
            t = type(v).__name__
            lines.append(f"  {k}: {t}")
    return "\n".join(lines)


def _format_exec_feedback(
    entry: TrajectoryEntry,
    sub_call_count: int,
    max_sub_calls: int,
) -> str:
    """Render a TrajectoryEntry as the user-message feedback the
    root LLM sees after each iteration."""
    parts = [f"Iteration {entry.iteration} executed."]
    if entry.error:
        parts.append(f"ERROR: {entry.error}")
    if entry.stdout.strip():
        parts.append(f"stdout:\n{entry.stdout.rstrip()}")
    if entry.stderr.strip():
        parts.append(f"stderr:\n{entry.stderr.rstrip()}")
    parts.append(
        f"Sub-LLM calls used: {sub_call_count}/{max_sub_calls}. "
        f"Call _set_result(...) when done; else emit the next code block."
    )
    return "\n\n".join(parts)


# ── Public API ────────────────────────────────────────────────────────


async def run_rlm(
    *,
    root_llm: LLMCallable,
    sub_llm: Optional[SubLLMCallable] = None,
    task: str,
    context_vars: Optional[dict[str, Any]] = None,
    config: Optional[RLMConfig] = None,
    system_prompt: Optional[str] = None,
) -> RLMResult:
    """Convenience wrapper — build an :class:`RLMRunner` and run it
    once. For repeated invocations with shared config, instantiate
    ``RLMRunner`` directly to reuse the system prompt and limits."""
    runner = RLMRunner(
        root_llm=root_llm,
        sub_llm=sub_llm,
        config=config,
        system_prompt=system_prompt,
    )
    return await runner.run(task=task, context_vars=context_vars)


__all__ = [
    "RLMConfig",
    "RLMResult",
    "TrajectoryEntry",
    "RLMRunner",
    "run_rlm",
    "extract_code_block",
    "LLMCallable",
    "SubLLMCallable",
]
