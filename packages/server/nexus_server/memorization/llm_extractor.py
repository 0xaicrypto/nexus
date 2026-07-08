"""
LLM-backed clinical-entity extractor for chat_ingester.

Bridges the abstract ``Extractor`` callable (``str → ExtractionResult``)
to the real LLM gateway. The output schema is the same as the stub
extractor's: a list of ``StructuredEntity`` rows with node_type +
content + verbatim evidence_quote.

Why a separate module: keeping the prompt in one place makes it easy
to version + iterate. M3-memory-architecture §5.0 talks about prompt
versioning as a first-class concern of the memorization layer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from typing import Any

from nexus_server.memorization.chat_ingester import (
    ExtractionResult, Extractor, StructuredEntity,
)

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT_ID = "chat_v1.0"
EXTRACTION_MODEL_TAG = "gemini-2.5-flash"

_SYSTEM = """\
You extract structured clinical entities from a chat encounter
between a physician and a clinical assistant.

★ OUTPUT FORMAT — STRICT ★
==========================
Your ENTIRE response is a single JSON object. The first character is
``{`` and the last character is ``}``. NEVER add prose before/after.
NEVER wrap in ```json fences. NEVER explain, apologise, or refuse.
If nothing extractable: return ``{"entities": []}``. A non-JSON
response is treated as an empty extraction.

For each clear clinical entity the encounter introduces, emit one
JSON object with these fields:

  node_type:        one of "finding", "med", "ddx", "measurement", "semantic_fact"
  content:          {label: "<short canonical name>", ...optional fields}
  evidence_quote:   a VERBATIM substring of the source text that
                    establishes the entity. Must appear character-for-
                    character in the source — do not paraphrase.
  confidence:       0.0 - 1.0 (your honest estimate)

Output shape — a single JSON object with one key:

  {"entities": [ <object>, <object>, ... ]}

Language policy (read carefully)
--------------------------------
Match the source text's dominant language for the user-visible
``label`` field. The medic will read these labels in the patient
profile pane; if they wrote the SOAP in Chinese, they expect
Chinese labels.

  - Source mostly Chinese  → emit labels in Chinese
    ("肺腺癌", "高血压", "氨氯地平", "右肺上叶占位",
     "纵隔淋巴结肿大", "未行化疗史", "ECOG 1")
  - Source mostly English  → emit labels in English
    ("lung adenocarcinoma", "hypertension", "amlodipine", …)
  - Acronyms / units / scoring scales (NSCLC, RECIST 1.1, CTCAE,
    ECOG, mg, AJCC IIIB, EGFR, ALK, ROS1) keep their canonical
    international form regardless of the surrounding body language —
    don't translate them.

If you can also confidently produce an English canonical synonym for
downstream computation, attach it as ``content.canonical_en`` (e.g.
``{"label": "肺腺癌", "canonical_en": "lung adenocarcinoma"}``).
Optional — leave it off if you're not sure.

Quality rules
-------------
- Only extract entities the chat clearly establishes. Skip speculative
  or hypothetical mentions ("could be X").
- Prefer one canonical short label per concept; don't list synonyms.
  "atrial fibrillation" not "afib"; "warfarin" not "coumadin".
- `evidence_quote` MUST be a substring of the input. Do not invent.
- Empty entities list is allowed. Do not pad with low-confidence
  guesses.
"""


def _parse_json_safe(raw: str) -> dict[str, Any]:
    """Parse the LLM output, tolerating common LLM-format drift.

    Three layers of recovery, each cheaper than the next:

      1. Direct parse of stripped input — fast path.
      2. Strip ```json ... ``` fences if present.
      3. Greedy substring search for the first '{...}' block —
         catches "Here is the JSON: { ... } let me know if..." style
         wrappers Gemini sometimes adds despite explicit instruction.

    Returns ``{}`` on total failure (caller treats as no entities).
    """
    s = (raw or "").strip()
    if not s:
        return {}
    # Layer 1
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        logger.debug("direct JSON parse failed: %s", e)
    # Layer 2 — fence strip
    fenced = s
    if fenced.startswith("```"):
        fenced = re.sub(r"^```(?:json)?\s*", "", fenced)
        fenced = re.sub(r"\s*```$", "", fenced)
        try:
            return json.loads(fenced)
        except json.JSONDecodeError as e:
            logger.debug("fenced JSON parse failed: %s", e)
    # Layer 3 — greedy bracket match. Find first '{' and last '}', try
    # to parse what's between. Doesn't handle nested escaped braces in
    # weird strings but the extractor JSON is shallow enough that it
    # almost always works.
    first = s.find("{")
    last = s.rfind("}")
    if first >= 0 and last > first:
        try:
            return json.loads(s[first : last + 1])
        except json.JSONDecodeError as e:
            logger.debug("bracket-match JSON parse failed: %s", e)
    # Layer 4 — partial-array recovery (F-truncated-extract). When the
    # LLM output is truncated mid-entity (max_tokens hit, network blip,
    # whatever), Layers 1-3 all fail because the outer ``{"entities":
    # [...]}`` is malformed. But the FRONT of the array is fine —
    # complete `{...}` entity objects sit there waiting to be salvaged.
    # We scan from the first `{` after `"entities"` (or `"items"` /
    # `"clinical_entities"`), pulling balanced top-level objects one at
    # a time and parsing each individually. Stop on the first object
    # that doesn't parse — that's where the truncation hit.
    recovered = _recover_partial_entities(s)
    if recovered:
        logger.warning(
            "extractor: top-level JSON unparseable but recovered %d "
            "complete entities from truncated stream",
            len(recovered),
        )
        return {"entities": recovered}
    logger.warning(
        "extractor LLM output isn't JSON even after fence-strip + "
        "bracket-recover + partial-array recovery: %r",
        s[:200],
    )
    return {}


def _recover_partial_entities(s: str) -> list[dict]:
    """Scan a truncated-or-malformed extractor response and pull out
    every well-formed entity object that sits before the break.

    Strategy: locate an array opener after one of the known wrapper
    keys (``"entities"`` / ``"items"`` / ``"clinical_entities"``), then
    walk forward tracking brace depth (string-aware so `}` inside a
    string literal doesn't fool us). Each time depth returns to 1
    (i.e. one level inside the array), we've finished a top-level
    object — slice it out and try ``json.loads``. Bail at the first
    failure; everything we collected so far is good.

    Returns an empty list when nothing salvageable. Cheap — O(n) over
    the string, parses at most one object per yield.
    """
    # Find the array opener.
    array_start = -1
    for key in ('"entities"', '"items"', '"clinical_entities"'):
        idx = s.find(key)
        if idx < 0:
            continue
        # Walk past the `:` and any whitespace to the `[`.
        bracket = s.find("[", idx)
        if bracket >= 0:
            array_start = bracket + 1
            break
    if array_start < 0:
        return []

    out: list[dict] = []
    depth = 0
    obj_start = -1
    in_string = False
    escape = False
    for i in range(array_start, len(s)):
        ch = s[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start >= 0:
                chunk = s[obj_start : i + 1]
                try:
                    parsed = json.loads(chunk)
                except json.JSONDecodeError:
                    # The truncation hit inside this object. Everything
                    # we already collected is still good.
                    break
                if isinstance(parsed, dict):
                    out.append(parsed)
                obj_start = -1
        elif ch == "]" and depth == 0:
            # Clean array end — no truncation, just done.
            break
    return out


def llm_chat_extractor(source_text: str) -> ExtractionResult:
    """Synchronous Extractor that wraps the async llm_gateway.call_llm.

    chat_ingester is sync (it runs as a FastAPI BackgroundTasks callback).
    We bridge to async by running the coroutine on the current event
    loop if one exists, or a fresh one if not.
    """
    t0 = time.monotonic()
    raw = ""
    try:
        from nexus_server import llm_gateway

        async def _call() -> str:
            content, _model, _stop, _tools = await llm_gateway.call_llm(
                messages=[{"role": "user", "content": source_text}],
                system_prompt=_SYSTEM,
                model=None,
                temperature=0.2,        # low T for extraction determinism
                # F15: bumped 1500→3500 (SOAP truncation).
                # F-truncated-extract: bumped 3500→6000. Even 3500
                # truncates dense 老王-style cases (long PET-CT
                # history + multi-system review). Gemini 2.5 Flash's
                # output ceiling is 8192; 6000 leaves a margin while
                # still fitting comfortably in the 30s round-trip.
                # Layer-4 partial-array recovery in _parse_json_safe
                # is the belt; this is the suspenders.
                max_tokens=6000,
                tools=None,
            )
            return content

        # Run the coroutine. If we're inside a running loop (very
        # unlikely for a sync BackgroundTask), fall back to creating a
        # new loop in a thread.
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're nested — run on a fresh loop in a worker thread.
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(asyncio.run, _call())
                    raw = fut.result()
            else:
                raw = loop.run_until_complete(_call())
        except RuntimeError:
            raw = asyncio.run(_call())
    except Exception as exc:  # noqa: BLE001
        logger.exception("LLM extractor failed: %s", exc)
        return ExtractionResult(
            raw_llm_output=f"(extractor error: {exc})",
            entities=[],
            latency_ms=int((time.monotonic() - t0) * 1000),
        )

    # F-extractor-drops — NFC-normalise the source text BEFORE we use
    # it for verbatim comparisons. macOS APFS hands us Chinese strings
    # in NFD (decomposed combining marks); the LLM returns NFC. A
    # naive ``evidence in source_text`` then returns False on what is
    # visually identical text, and the entity gets dropped at the
    # ``not_verbatim`` step. Doing the normalisation once here means
    # downstream cite-back still references the *normalised* form,
    # which is what twin_event_log stores anyway (read paths NFC).
    source_text = unicodedata.normalize("NFC", source_text)

    parsed = _parse_json_safe(raw)
    # F20 — be liberal in what we accept:
    #   {"entities": [...]}   ← canonical
    #   [...] bare array      ← LLM forgot the wrapper
    #   {"items": [...]} / {"clinical_entities": [...]} ← key drift
    if isinstance(parsed, list):
        entities_raw = parsed
    elif isinstance(parsed, dict):
        entities_raw = (
            parsed.get("entities")
            or parsed.get("items")
            or parsed.get("clinical_entities")
            or []
        )
        # Single-object form: {"node_type": ..., "content": ...}
        if not entities_raw and "node_type" in parsed and "content" in parsed:
            entities_raw = [parsed]
    else:
        entities_raw = []
    logger.info(
        "llm_chat_extractor: parsed JSON top-level type=%s, raw_entities=%d, "
        "raw_chars=%d",
        type(parsed).__name__, len(entities_raw), len(raw),
    )
    drops = {
        "not_dict":      0,
        "bad_node_type": 0,
        "no_label":      0,
        "no_evidence":   0,
        "not_verbatim":  0,  # fuzzy retry also failed
        "fuzzy_rescued": 0,  # close-enough match recovered
    }

    entities: list[StructuredEntity] = []
    for item in entities_raw:
        if not isinstance(item, dict):
            drops["not_dict"] += 1
            continue
        node_type = item.get("node_type")
        if node_type not in {
            "finding", "med", "ddx", "measurement", "semantic_fact",
        }:
            drops["bad_node_type"] += 1
            continue
        content = item.get("content") or {}
        if not isinstance(content, dict):
            drops["bad_node_type"] += 1
            continue
        if not content.get("label"):
            drops["no_label"] += 1
            continue
        evidence = item.get("evidence_quote") or ""
        if not isinstance(evidence, str) or not evidence:
            drops["no_evidence"] += 1
            continue
        # F-extractor-drops — normalise evidence the same way we
        # normalised source_text above so the verbatim check is
        # comparing apples to apples.
        evidence = unicodedata.normalize("NFC", evidence)
        # Verbatim check, with a softened fallback for whitespace +
        # punctuation drift the LLM commonly introduces in Chinese
        # text. Without this fallback we used to drop ~60-80% of
        # otherwise-valid extractions because the LLM normalised a
        # space or replaced a full-width punctuation mark, and the
        # medic's Memory tab stayed empty even though they pasted a
        # full SOAP.
        if evidence not in source_text:
            rescued = _fuzzy_rescue(evidence, source_text)
            if rescued is None:
                logger.info(
                    "extractor: dropping entity %r (evidence %r not in source — "
                    "no fuzzy match either)",
                    content.get("label"), evidence[:60],
                )
                drops["not_verbatim"] += 1
                continue
            logger.info(
                "extractor: fuzzy-rescued entity %r — original %r → matched %r",
                content.get("label"), evidence[:40], rescued[:40],
            )
            evidence = rescued
            drops["fuzzy_rescued"] += 1
        try:
            conf = float(item.get("confidence", 0.7))
        except (TypeError, ValueError):
            conf = 0.7
        entities.append(StructuredEntity(
            node_type=node_type,
            content=content,
            evidence_quote=evidence,
            confidence=max(0.0, min(1.0, conf)),
        ))

    logger.info(
        "llm_chat_extractor: source=%d chars, LLM returned %d entities, "
        "kept %d (drops: %s), latency_ms=%d",
        len(source_text), len(entities_raw), len(entities), drops,
        int((time.monotonic() - t0) * 1000),
    )

    # F-extractor-drops — propagate ``drops`` (per-reason kill counts)
    # and ``raw_count`` (how many entities the LLM emitted) so the
    # chat_ingester can persist a precise breakdown into
    # INGESTION_COMPLETED.payload. Without these fields the diagnostic
    # banner could only say "本轮未记忆"; with them it can say
    # "LLM 给了 5 条,4 条因 quote 不匹配丢弃,1 条因缺标签丢弃" —
    # actionable for the medic + for prompt iteration on our side.
    return ExtractionResult(
        raw_llm_output=raw,
        entities=entities,
        latency_ms=int((time.monotonic() - t0) * 1000),
        drops=drops,
        raw_count=len(entities_raw),
    )


def _fuzzy_rescue(evidence: str, source: str) -> "str | None":
    """When ``evidence not in source`` literally, try a few normalisations
    before giving up. Each normalisation rule is one common LLM drift
    mode we've observed:

      1. Whitespace collapse — LLM often de-pads ``"  双肺  纹理  增粗  "``
         to ``"双肺纹理增粗"``.
      2. Full-width punctuation — LLM may swap ``，`` ↔ ``,`` or ``：``
         ↔ ``:`` between Chinese and ASCII.
      3. Trailing newline / period — the LLM may drop ``。`` or
         ``\\n`` at the end of the quote.

    We return the *original* source substring that matches the
    normalised evidence (so downstream provenance still cites the
    medic's text, not the LLM's paraphrase). None means no rescue.
    """
    if not evidence:
        return None

    # 1. Whitespace strip.
    e = evidence.strip()
    if e and e in source:
        return e

    # 2. Punctuation normalisation (CN↔EN). Build a map that converts
    # both sides to ASCII for comparison; then search the normalised
    # source for the normalised evidence and recover the original slice.
    _PUNCT_MAP = str.maketrans({
        "，": ",", "。": ".", "：": ":", "；": ";", "！": "!",
        "？": "?", "（": "(", "）": ")", "【": "[", "】": "]",
        " ": " ",  # NBSP / FW space → ASCII
        "“": '"', "”": '"', "‘": "'", "’": "'",
    })
    e_norm = e.translate(_PUNCT_MAP)
    s_norm = source.translate(_PUNCT_MAP)
    idx = s_norm.find(e_norm)
    if idx >= 0:
        return source[idx : idx + len(e)]

    # 3. Trailing-character tolerance: try without trailing punctuation.
    while e and e[-1] in "。.,，；;！!？?\n":
        e = e[:-1]
        if e in source:
            return e

    return None


# Make the type-system happy: this satisfies the Extractor protocol.
extractor: Extractor = llm_chat_extractor
