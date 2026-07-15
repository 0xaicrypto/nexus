"""Heuristic intent extractor for scheduled tasks (Phase 1).

Phase 1 is regex-only — no LLM call. Two reasons:

  1. Cheaper. 99%+ of chat turns aren't scheduling intents; the regex
     filter bypasses Gemini entirely for the negative case.
  2. Safer. A heuristic that errs conservative (returns ``None``) is
     easier to reason about than an LLM that might confidently emit a
     SCHEDULED_TASK_PROPOSED for "I scheduled the patient for 9am" —
     a sentence ABOUT the past, not an INSTRUCTION about the future.

When the heuristic does match, the result is a ``ScheduleProposal``
the caller emits as SCHEDULED_TASK_PROPOSED (UI renders the
confirmation card). Phase 2 swaps the heuristic for a structured
Gemini call when the regex matches BUT no concrete payload can be
parsed from the message alone.

Scope of Phase 1 patterns:

  Time tokens     "in N {min|minute|hour|day|week}"
                  "in N {分钟|小时|天|周}"
                  "tomorrow [at HH:MM]" / "明天 [HH 点]"
                  "today" / "今天"
                  literal "HH:MM" / "HH 点" / "HH am/pm"

  Action tokens   "email/邮件" (Phase 1 only kind: send_email)
                  Phase 2 adds: brief/summary, remind/reminder

  Recipient       "to <email>" / "给 <email>"

If the message has BOTH a time token AND a recognised action token,
we emit a proposal. Either alone returns None (e.g. "in 2 hours" with
no action is just patient context; "email Dr Smith" with no time
isn't a future task).

We deliberately do NOT try to extract subject / body / cc here. The
UI's confirmation card lets the medic fill those in before confirming.
That's also where Phase 2's LLM extraction will plug in: when
heuristic returns a partial proposal (time + recipient but no body),
the UI surfaces "Nexus will generate a draft" + LLM call on confirm.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Output shape
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ScheduleProposal:
    """One proposed scheduled task. The caller emits this as a
    SCHEDULED_TASK_PROPOSED event; UI renders a confirmation card with
    these fields pre-filled. Medic edits + confirms → emit
    SCHEDULED_TASK_CREATED with a new task_id."""

    proposal_id:    str           # UUID v4, distinct from task_id
    kind:           str           # 'send_email' (Phase 1 only)
    fire_at:        int           # unix seconds UTC
    user_tz:        str           # IANA zone
    summary:        str           # human-readable one-line: 中文 OK
    payload:        dict          # kind-specific; for send_email: {to?, cc?, subject?, body?}
    recurrence_cron: Optional[str] = None
    session_id:    Optional[str] = None
    patient_hash:  Optional[str] = None
    # Fields needed for the next round-trip but not yet final — UI
    # shows these to the medic in the confirmation card so they
    # can adjust before locking in. Empty list = nothing to flag.
    needs_user_input: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────
# Regex tables — compiled once at module load
# ─────────────────────────────────────────────────────────────────────


# Time-relative ("in N units"). Captures (count, unit).
_RE_RELATIVE_EN = re.compile(
    r"\bin\s+(\d+)\s*(min|mins|minute|minutes|hour|hours|h|day|days|week|weeks|d|w)\b",
    re.IGNORECASE,
)
# Chinese relative-time. Numeric value can be ASCII digits or one of
# the common Chinese number words (一/二/两/三/四/五/六/七/八/九/十).
# In Chinese, "two hours" is almost always written "两小时" — never
# "2小时" or "二小时" outside formal documents — so without supporting
# 两/一/etc. we'd miss the most common natural phrasing.
_RE_RELATIVE_ZH = re.compile(
    r"(\d+|一|二|两|三|四|五|六|七|八|九|十)\s*"
    r"(分钟|小时|个小时|天|周|个星期|星期)(?:之?后)?",
    re.UNICODE,
)

# Chinese digit-word → int. Limited to ≤10 because past that the
# heuristic threshold is "did the medic really mean schedule" vs
# "did they mean a clinical anecdote about 12 hours ago" — kick to
# Phase 2's LLM for those edge cases.
_ZH_DIGIT = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

# "tomorrow / today / 明天 / 今天 / 后天" + optional time-of-day.
_RE_TOMORROW_EN = re.compile(
    r"\b(tomorrow|today)\b"
    r"(?:\s*(?:at)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?",
    re.IGNORECASE,
)
_RE_TOMORROW_ZH = re.compile(
    r"(今天|明天|后天)"
    r"(?:\s*(早上|上午|中午|下午|晚上)?\s*(\d{1,2})\s*[点时]"
    r"(?:\s*(\d{1,2})\s*分)?)?",
    re.UNICODE,
)

# Action: "email <addr>" / "send email to <addr>" / "邮件给 <addr>"
_RE_ACTION_EMAIL = re.compile(
    r"\b(?:email|send\s+(?:an?\s+)?email\s+(?:to)?)\b"
    r"|邮件|发邮件|寄?给.*邮件",
    re.IGNORECASE | re.UNICODE,
)

# Recipient: simple email-like token. Used after the action regex
# fired so we don't pull random addresses from unrelated context.
_RE_ADDR = re.compile(r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


_UNIT_TO_SECONDS_EN = {
    "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 7 * 86400, "week": 7 * 86400, "weeks": 7 * 86400,
}
_UNIT_TO_SECONDS_ZH = {
    "分钟": 60,
    "小时": 3600, "个小时": 3600,
    "天":   86400,
    "周":   7 * 86400,
    "个星期": 7 * 86400, "星期": 7 * 86400,
}


def _now_in_tz(tz: str) -> datetime:
    """Caller's now() in their tz. Pulled out so tests can monkeypatch."""
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("UTC")
    return datetime.now(zone)


def _to_unix(dt: datetime) -> int:
    """Convert a tz-aware datetime to unix seconds (UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _parse_relative(text: str) -> Optional[int]:
    """Return seconds-to-add or None. Tries EN then ZH."""
    m = _RE_RELATIVE_EN.search(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        return n * _UNIT_TO_SECONDS_EN.get(unit, 0)
    m = _RE_RELATIVE_ZH.search(text)
    if m:
        raw_n = m.group(1)
        # Either ASCII digits or one of the Chinese digit chars.
        if raw_n.isdigit():
            n = int(raw_n)
        else:
            n = _ZH_DIGIT.get(raw_n, 0)
        unit = m.group(2)
        return n * _UNIT_TO_SECONDS_ZH.get(unit, 0)
    return None


def _parse_day_at(text: str, now: datetime) -> Optional[datetime]:
    """Return a tz-aware datetime for the day-and-optional-time
    expression. None if nothing matched."""
    m = _RE_TOMORROW_EN.search(text)
    if m:
        word = m.group(1).lower()
        days_ahead = 1 if word == "tomorrow" else 0
        target_date = (now + timedelta(days=days_ahead)).date()
        hh, mm, ampm = m.group(2), m.group(3), m.group(4)
        return _compose(target_date, hh, mm, ampm, now=now)

    m = _RE_TOMORROW_ZH.search(text)
    if m:
        word = m.group(1)
        days_ahead = {"今天": 0, "明天": 1, "后天": 2}[word]
        target_date = (now + timedelta(days=days_ahead)).date()
        period = m.group(2)
        hh, mm = m.group(3), m.group(4)
        # ZH period → am/pm equivalent so the same composer can run.
        ampm = None
        if period in ("早上", "上午"):
            ampm = "am"
        elif period in ("下午", "晚上"):
            ampm = "pm"
        elif period == "中午":
            # 中午 N点 — treat 12-1pm as pm; 12点 stays noon.
            ampm = "pm"
        return _compose(target_date, hh, mm, ampm, now=now)
    return None


def _compose(target_date, hh, mm, ampm, *, now: datetime) -> datetime:
    """Combine a date + optional time-of-day into a tz-aware datetime.
    If no time-of-day given, use the current time on the target date —
    or 09:00 if the resulting datetime would be in the past."""
    tz = now.tzinfo
    if hh is None:
        # No time provided. For "tomorrow" with no time, default to 09:00.
        if target_date > now.date():
            return datetime.combine(
                target_date, datetime.min.time().replace(hour=9),
            ).replace(tzinfo=tz)
        # "today" with no time → use now() + 5 min as a safe-future floor.
        return now + timedelta(minutes=5)
    hour = int(hh) % 24
    minute = int(mm) if mm else 0
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    return datetime.combine(
        target_date,
        datetime.min.time().replace(hour=hour, minute=minute),
    ).replace(tzinfo=tz)


def _extract_email(text: str) -> Optional[str]:
    """First plausible email address in the text, or None."""
    m = _RE_ADDR.search(text)
    return m.group(1) if m else None


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def extract_proposal(
    *,
    user_text: str,
    user_tz: str = "UTC",
    session_id: Optional[str] = None,
    patient_hash: Optional[str] = None,
) -> Optional[ScheduleProposal]:
    """Return a ScheduleProposal if the user text expresses a
    future-action intent; else None.

    Conservative: requires BOTH a time token AND a recognised action
    token. "in 2 hours" alone isn't a task. "email Dr Smith" alone
    isn't a future-tense task either (could be "right now"). Phase 1
    only handles send_email; later kinds layer on top.
    """
    if not user_text or not user_text.strip():
        return None

    # Step 1: action token must be present.
    if not _RE_ACTION_EMAIL.search(user_text):
        return None

    # Step 2: try to extract a fire_at from the time tokens. Two
    # strategies in order: explicit "tomorrow at 9am" form, then
    # relative "in 2 hours".
    now = _now_in_tz(user_tz)

    target_dt = _parse_day_at(user_text, now)
    if target_dt is None:
        seconds = _parse_relative(user_text)
        if seconds is None:
            return None
        target_dt = now + timedelta(seconds=seconds)

    fire_at = _to_unix(target_dt)

    # Sanity: never schedule in the past or absurdly far future
    # (>1 year = something parsed wrong).
    now_unix = _to_unix(now)
    if fire_at < now_unix - 60:
        # The composer above already nudges "today" with no time forward
        # by 5 min, so a past time here means the medic explicitly said
        # "today at 8am" and it's currently 11am. Roll to tomorrow.
        target_dt = target_dt + timedelta(days=1)
        fire_at = _to_unix(target_dt)
    if fire_at > now_unix + 366 * 86400:
        return None

    # Step 3: build the payload. Phase 1 only handles send_email — we
    # extract the recipient if mentioned but leave subject/body for the
    # medic to fill in.
    recipient = _extract_email(user_text)
    payload: dict = {}
    needs: list[str] = []
    if recipient:
        payload["to"] = [recipient]
    else:
        needs.append("to")
    needs.append("subject")
    needs.append("body")

    # Step 4: human summary. The UI shows this on the confirmation card
    # so the medic can see "two hours from now (14:32 local) · send email
    # to dr.smith@hosp.org" before clicking Confirm.
    local_str = target_dt.strftime("%Y-%m-%d %H:%M")
    if recipient:
        summary = f"{local_str} ({user_tz}) · send_email → {recipient}"
    else:
        summary = f"{local_str} ({user_tz}) · send_email (recipient TBD)"

    return ScheduleProposal(
        proposal_id=str(uuid.uuid4()),
        kind="send_email",
        fire_at=fire_at,
        user_tz=user_tz,
        summary=summary,
        payload=payload,
        recurrence_cron=None,
        session_id=session_id,
        patient_hash=patient_hash,
        needs_user_input=needs,
    )
