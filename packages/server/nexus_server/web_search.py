"""Web-grounded clinical search (T4 retrieval tier).

Backs the T4 path of ``retrieval_tiers.py`` — when the chat
classifier sees a guideline-like query (NCCN, ESMO, "what does
literature say"), we hit a curated set of clinical sources, fold
the snippets into the Gemini call's prompt alongside PATIENT
CONTEXT, and surface ``[Wxx]`` citations the desktop renders as a
distinct chip kind.

Design (full doc: docs/design/web-search-and-subagents.md):

  1. PHI scrubber — strip MRN-like tokens, initials, DOB-like
     dates BEFORE the query leaves the loopback. Mandatory.
  2. Domain allow-list — Tavily ``include_domains`` filters at the
     provider side so out-of-list results never reach us.
  3. Tavily provider — clean snippet+URL JSON, ~5s p95 latency.
  4. Result normaliser — produce ``[WebResult]`` regardless of
     which provider we used so future swap to Brave / Gemini-native
     grounding is a 1-file change.

Auth: TAVILY_API_KEY lives in ``$RUNE_HOME/.env`` and is exposed in
settings_router's ALLOWED_KEYS so the medic can paste it through
the Settings · LLM tab. Missing key → T4 silently degrades to T3
with a one-line "(web search unavailable — set TAVILY_API_KEY)"
tail. Don't fail loud — clinical answers should still flow.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Default clinical allow-list
# ─────────────────────────────────────────────────────────────────────


DEFAULT_CLINICAL_DOMAINS = [
    # International English guidelines + literature
    "uptodate.com",          "nccn.org",          "acr.org",
    "esmo.org",              "ahrq.gov",          "nih.gov",
    "ncbi.nlm.nih.gov",      "pubmed.gov",        "pubmed.ncbi.nlm.nih.gov",
    "radiopaedia.org",       "radiologyassistant.nl",
    "rsna.org",              "thieme-connect.com",
    "cancer.org",            "ahajournals.org",
    # Chinese clinical sources (target users include Chinese clinicians)
    "csco.org.cn",           "cnki.net",
    "chinacdc.cn",
]


# ─────────────────────────────────────────────────────────────────────
# Result shape
# ─────────────────────────────────────────────────────────────────────


@dataclass
class WebResult:
    """Normalised search hit. ``w_id`` is assigned by the caller so
    the same hit gets the same ``[Wxx]`` chip on both the citation
    payload and the prompt body."""
    w_id:    int                  # 1-based ordinal within this turn
    url:     str
    title:   str
    snippet: str
    domain:  str
    raw_content: Optional[str] = None  # only present when caller requested
    score:   float = 0.0

    def to_dict(self) -> dict:
        out = {
            "w_id":    self.w_id,
            "url":     self.url,
            "title":   self.title,
            "snippet": self.snippet,
            "domain":  self.domain,
            "score":   self.score,
        }
        if self.raw_content is not None:
            out["raw_content"] = self.raw_content
        return out


@dataclass
class SearchResponse:
    """What ``search_clinical`` returns to retrieval_tiers."""
    query_sent:   str             # what we actually sent (post-scrub)
    results:      list[WebResult] = field(default_factory=list)
    provider:     str             = "tavily"
    error:        Optional[str]   = None


# ─────────────────────────────────────────────────────────────────────
# PHI scrubber — runs BEFORE the query leaves the loopback
# ─────────────────────────────────────────────────────────────────────
#
# Conservative — we'd rather over-scrub and lose some semantic nuance
# than leak a medic's literal patient note to a third-party search
# provider. Three families:
#
#   1. MRN-like alphanumeric tokens (8+ digits, "MRN-12345" prefix,
#      hospital-specific patterns)
#   2. DOB-like dates (1900-2099 + month/day variants)
#   3. Initials embedded in clinical phrasing ("J.D. has..." → strip)
#   4. Long bracketed citation tokens from chat ([N42], [W7]) — these
#      have no meaning outside our app and confuse the search index

_MRN_PATTERNS = [
    # MRN-12345 / MRN12345 / MRN: 12345 / MRN  12345 (any combo of
    # separators including multiple spaces). The ``[-:\s]*`` allows
    # zero-or-more separators so all of MRN12345, MRN-12345, MRN:
    # 12345 and MRN: 12345 (double space) match.
    re.compile(r"\bMRN[-:\s]*\d{4,}\b", re.IGNORECASE),
    # 9+ digits run (NPI / SSN / med-record numbers; cap matches false-
    # positives like phone numbers)
    re.compile(r"\b\d{9,}\b"),
    # SSN-style: 123-45-6789
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
]
_DOB_PATTERNS = [
    # 1924-08-15, 2024/08/15, 8/15/1924
    re.compile(r"\b(?:19|20)\d{2}[-/.\s]\d{1,2}[-/.\s]\d{1,2}\b"),
    re.compile(r"\b\d{1,2}[-/.\s]\d{1,2}[-/.\s](?:19|20)\d{2}\b"),
]
# Initials like "J.D." or "Z.S." — high false-positive risk (abbreviations
# everywhere in clinical text), so we narrow to: dot-separated, 2-3 capital
# letters, NOT inside a longer all-caps acronym, followed by a verb-like
# token. Trade-off: drops "I usually" type phrasings, keeps "ACR" / "NCCN".
_INITIALS_PATTERN = re.compile(
    r"\b([A-Z]\.[A-Z]\.|[A-Z]\.[A-Z]\.[A-Z]\.)(?=\s+(?:has|is|was|presented|complains|history))",
)
_NEXUS_CITATION_PATTERN = re.compile(r"\[(?:N|W)\d+\]")


def scrub_phi(query: str) -> str:
    """Strip PHI-like tokens from a query string before it leaves the
    machine. Returns the scrubbed string. Never raises — the worst
    case is "scrubbed too aggressively" which produces a slightly
    less specific search; that's strictly better than leaking PHI.
    """
    if not query:
        return ""
    out = query
    for pat in _MRN_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    for pat in _DOB_PATTERNS:
        out = pat.sub("[REDACTED-DATE]", out)
    out = _INITIALS_PATTERN.sub("[REDACTED-PATIENT]", out)
    out = _NEXUS_CITATION_PATTERN.sub("", out)
    # Collapse double-spaces produced by removal.
    out = re.sub(r"\s+", " ", out).strip()
    return out


# ─────────────────────────────────────────────────────────────────────
# Config + provider probe
# ─────────────────────────────────────────────────────────────────────


def _live_tavily_key() -> Optional[str]:
    """Read TAVILY_API_KEY from env. Returns None if unset / placeholder
    so the caller can degrade to T3 gracefully."""
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key or "REPLACE_WITH_" in key:
        return None
    return key


def _live_allowed_domains() -> list[str]:
    """User-tunable list. ``NEXUS_WEB_ALLOWED_DOMAINS`` env var is a
    comma-list; falls back to DEFAULT_CLINICAL_DOMAINS. Empty list =
    no filtering (DON'T return [] — caller would interpret as 'no
    restriction'; instead we keep the defaults and require the user
    to opt out explicitly with the literal string 'NONE')."""
    raw = os.environ.get("NEXUS_WEB_ALLOWED_DOMAINS", "").strip()
    if not raw:
        return list(DEFAULT_CLINICAL_DOMAINS)
    if raw.upper() == "NONE":
        return []
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def is_configured() -> bool:
    """Whether T4 web search is usable on this server. Settings panel
    + tier classifier call this to decide whether to expose / route to
    web-grounded answers."""
    return _live_tavily_key() is not None


# ─────────────────────────────────────────────────────────────────────
# Tavily provider
# ─────────────────────────────────────────────────────────────────────


_TAVILY_URL = "https://api.tavily.com/search"


def _domain_from_url(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


async def _post_tavily(
    query: str,
    api_key: str,
    *,
    max_results: int = 5,
    include_domains: Optional[list[str]] = None,
    include_raw_content: bool = False,
) -> tuple[list[WebResult], Optional[str]]:
    """One Tavily call. Returns (results, error_message_or_None).

    Error path: network failure / 4xx / 5xx → ``([], "msg")`` so
    the caller can degrade to T3. Empty result list with no error
    is a real "Tavily found nothing", which is still valuable
    feedback to the medic ("the literature is silent on this").
    """
    payload: dict = {
        "api_key":      api_key,
        "query":        query,
        "max_results":  max_results,
        "search_depth": "advanced",
        "include_answer":         False,
        "include_raw_content":    include_raw_content,
    }
    if include_domains:
        payload["include_domains"] = include_domains

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(_TAVILY_URL, json=payload)
    except httpx.ConnectError as e:
        return ([], f"Cannot reach Tavily: {e}")
    except httpx.TimeoutException as e:
        return ([], f"Tavily timed out: {e}")
    except Exception as e:  # noqa: BLE001
        return ([], f"Tavily call failed: {e}")

    if resp.status_code != 200:
        try:
            detail = resp.json().get("error", resp.text[:200])
        except Exception:  # noqa: BLE001
            detail = resp.text[:200]
        return ([], f"Tavily HTTP {resp.status_code}: {detail}")

    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return ([], "Tavily returned non-JSON body")

    raw_results = data.get("results") or []
    out: list[WebResult] = []
    for i, r in enumerate(raw_results[:max_results], start=1):
        url   = r.get("url", "")
        title = r.get("title", url)
        snip  = r.get("content", "") or r.get("snippet", "")
        out.append(WebResult(
            w_id=i,
            url=url,
            title=title,
            snippet=snip[:600],     # cap for prompt budget
            domain=_domain_from_url(url),
            raw_content=(r.get("raw_content") if include_raw_content else None),
            score=float(r.get("score") or 0.0),
        ))
    return (out, None)


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


async def search_clinical(
    query: str,
    *,
    max_results: int = 5,
    extra_domains: Optional[list[str]] = None,
    include_raw_content: bool = False,
) -> SearchResponse:
    """Top-level entry point for retrieval_tiers' T4 path.

    Pipeline:
      1. PHI scrub the query.
      2. Resolve TAVILY_API_KEY + allow-list.
      3. POST Tavily.
      4. Map to WebResult.

    If no API key is configured, returns a SearchResponse with
    error set + empty results. Caller (retrieval_tiers) is expected
    to fall through to T3.
    """
    scrubbed = scrub_phi(query)
    # Tavily caps search queries at 400 characters. A medic pasting a
    # full SOAP note into chat blows past this every time and the API
    # returns HTTP 400 "Query is too long". Truncate aggressively
    # (380 chars + ellipsis) — Tavily indexes by keywords anyway, the
    # first sentences of a SOAP usually contain the diagnosis +
    # demographics that matter for grounding.
    if len(scrubbed) > 380:
        logger.info(
            "web_search: truncating query %d → 380 chars (Tavily limit)",
            len(scrubbed),
        )
        scrubbed = scrubbed[:380].rstrip() + "…"
    api_key = _live_tavily_key()
    if not api_key:
        return SearchResponse(
            query_sent=scrubbed,
            results=[],
            error=(
                "TAVILY_API_KEY not configured — set it in Settings · LLM "
                "to enable web-grounded answers. Falling back to "
                "patient-only answers."
            ),
        )

    domains = list(_live_allowed_domains())
    if extra_domains:
        domains = list({*domains, *(d.strip().lower() for d in extra_domains if d.strip())})

    logger.info(
        "web_search: query_in_len=%d query_out_len=%d domains=%d max_results=%d",
        len(query), len(scrubbed), len(domains), max_results,
    )

    results, err = await _post_tavily(
        scrubbed, api_key,
        max_results=max_results,
        include_domains=(domains or None),
        include_raw_content=include_raw_content,
    )
    return SearchResponse(
        query_sent=scrubbed,
        results=results,
        error=err,
    )


# ─────────────────────────────────────────────────────────────────────
# Intent classifier — does this query benefit from web grounding?
# ─────────────────────────────────────────────────────────────────────


# Trigger T4 when the question is generic / external-knowledge.
_WEB_INTENT_PATTERNS = [
    re.compile(r"\b(NCCN|ACR|ESMO|UpToDate|guideline|standard\s+of\s+care)\b", re.IGNORECASE),
    re.compile(r"\b(literature|paper|study|trial|meta[\s-]analysis|systematic\s+review)\b", re.IGNORECASE),
    re.compile(r"\b(latest|recent|new|emerging|20\d{2})\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+does\s+.{1,40}\s+say\s+about\b", re.IGNORECASE),
    re.compile(r"\b(consensus|recommend|evidence\s+for)\b", re.IGNORECASE),
    re.compile(r"(?:指南|文献|最新|共识|证据|推荐)", re.UNICODE),
]

# Patient-context override. If the question is clearly about THIS
# patient, prefer T3 even if it mentions a guideline term ("does
# OUR patient fit NCCN criteria?" → T3 with NCCN context implicit).
#
# Note on \b and CJK: Python's re \b doesn't fire between two
# ideographs (same gotcha as the practitioner heuristic — see
# heuristic_extractor.py module doc). The Chinese alternatives drop
# \b and rely on the literal char anchoring. False-positive risk is
# essentially nil for our domain phrasings.
_PATIENT_INTENT_PATTERNS = [
    re.compile(r"\bthis\s+patient\b", re.IGNORECASE),
    re.compile(r"(?:这个?病人|本患者|该患者|这位患者)", re.UNICODE),
]


def looks_like_web_question(question: str) -> bool:
    """Heuristic for whether to route to T4. Returns False for clearly
    patient-anchored questions even when web tokens are present, so
    we don't pay the search round-trip on questions T3 can answer
    from the patient graph alone."""
    if not question:
        return False
    if any(p.search(question) for p in _PATIENT_INTENT_PATTERNS):
        return False
    return any(p.search(question) for p in _WEB_INTENT_PATTERNS)
