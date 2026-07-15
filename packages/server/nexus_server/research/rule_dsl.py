"""Restricted DSL for ``auto-rule`` eligibility criteria.

Design §3.3.1 ④/⑤ + §5.1 Stage 1. The DSL is intentionally tiny so a
single grammar rejects everything the LLM could possibly hallucinate:

  expr      := or_expr
  or_expr   := and_expr ('OR' and_expr)*
  and_expr  := unary_expr ('AND' unary_expr)*
  unary_expr := 'NOT' unary_expr | atom
  atom      := comparison | '(' expr ')'
  comparison := field op value
              | field 'BETWEEN' value 'AND' value
              | field 'IN' '(' value (',' value)* ')'
              | field 'IS' 'NULL'
              | field 'IS' 'NOT' 'NULL'
              | 'EXISTS' '(' field ')'
              | 'NOT' 'EXISTS' '(' field ')'
  op        := '=' | '!=' | '<' | '<=' | '>' | '>=' | 'CONTAINS'
  value     := number | string | bool
  field     := identifier in PATIENT_FACTS_SCHEMA

Evaluator returns one of ``'pass' | 'fail' | 'unknown'``. ``unknown``
when a referenced field is missing in the supplied patient_facts —
this signals "we don't know yet, ask doctor or wait for more data".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from nexus_server.research.patient_facts import (
    PATIENT_FACTS_SCHEMA,
    PatientFacts,
)


class RuleParseError(ValueError):
    pass


# ─────────────────────────────────────────────────────────────────────
# Token stream
# ─────────────────────────────────────────────────────────────────────


_TOKEN_RE = re.compile(
    r"""
    \s+                                |  # whitespace
    (?P<num>-?\d+(?:\.\d+)?)           |  # number
    (?P<str>'[^']*'|"[^"]*")           |  # quoted string
    (?P<bool>\b(?:true|false|TRUE|FALSE|True|False)\b) |
    (?P<id>[A-Za-z_][A-Za-z0-9_]*)     |  # identifier or keyword
    (?P<op>!=|<=|>=|=|<|>|\(|\)|,)        # operators / punctuation
    """,
    re.VERBOSE,
)


def _tokenize(s: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(s):
        m = _TOKEN_RE.match(s, pos)
        if not m:
            raise RuleParseError(f"unexpected character at {pos}: {s[pos:pos+10]!r}")
        pos = m.end()
        if m.group("num"):
            out.append(("NUM", m.group("num")))
        elif m.group("str"):
            out.append(("STR", m.group("str")[1:-1]))
        elif m.group("bool"):
            out.append(("BOOL", m.group("bool").lower()))
        elif m.group("id"):
            out.append(("ID", m.group("id")))
        elif m.group("op"):
            out.append(("OP", m.group("op")))
        # whitespace: skip
    return out


KEYWORDS = {"AND", "OR", "NOT", "IN", "BETWEEN", "IS", "NULL",
            "CONTAINS", "EXISTS", "TRUE", "FALSE"}


# ─────────────────────────────────────────────────────────────────────
# AST nodes (kept tiny)
# ─────────────────────────────────────────────────────────────────────


@dataclass
class Cmp:
    field: str
    op:    str         # '=' '!=' '<' '<=' '>' '>=' 'CONTAINS' 'IN' 'BETWEEN' 'IS_NULL' 'IS_NOT_NULL' 'EXISTS' 'NOT_EXISTS'
    value: Any = None  # number | string | bool | list | (lo, hi)


@dataclass
class And:
    left:  Any
    right: Any


@dataclass
class Or:
    left:  Any
    right: Any


@dataclass
class Not:
    expr: Any


# ─────────────────────────────────────────────────────────────────────
# Parser — recursive descent
# ─────────────────────────────────────────────────────────────────────


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.tokens = tokens
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def eat(self, expected: Optional[tuple[str, str]] = None) -> tuple[str, str]:
        if self.i >= len(self.tokens):
            raise RuleParseError("unexpected end of expression")
        t = self.tokens[self.i]; self.i += 1
        if expected and (t[0] != expected[0] or t[1].upper() != expected[1]):
            raise RuleParseError(f"expected {expected}, got {t}")
        return t

    def parse(self):
        node = self.or_expr()
        if self.i != len(self.tokens):
            raise RuleParseError(f"trailing tokens: {self.tokens[self.i:]}")
        return node

    def or_expr(self):
        node = self.and_expr()
        while self._kw("OR"):
            self.eat()
            node = Or(node, self.and_expr())
        return node

    def and_expr(self):
        node = self.unary()
        while self._kw("AND"):
            self.eat()
            node = And(node, self.unary())
        return node

    def unary(self):
        if self._kw("NOT"):
            self.eat()
            return Not(self.unary())
        return self.atom()

    def atom(self):
        t = self.peek()
        if not t:
            raise RuleParseError("expected atom")
        if t[0] == "OP" and t[1] == "(":
            self.eat()
            node = self.or_expr()
            if not (self.peek() and self.peek()[0] == "OP" and self.peek()[1] == ")"):
                raise RuleParseError("missing )")
            self.eat()
            return node
        if self._kw("EXISTS"):
            self.eat()
            self.eat(("OP", "("))
            fld = self.eat()
            if fld[0] != "ID":
                raise RuleParseError(f"EXISTS expects field id, got {fld}")
            self.eat(("OP", ")"))
            return Cmp(field=fld[1], op="EXISTS")
        # comparison: field <op> ...
        return self.comparison()

    def comparison(self):
        ftok = self.eat()
        if ftok[0] != "ID" or ftok[1].upper() in KEYWORDS:
            raise RuleParseError(f"expected field name, got {ftok}")
        field = ftok[1]

        nxt = self.peek()
        if not nxt:
            raise RuleParseError(f"expected operator after {field}")

        # IS NULL / IS NOT NULL
        if self._kw("IS"):
            self.eat()
            if self._kw("NOT"):
                self.eat()
                self.eat(("ID", "NULL"))
                return Cmp(field=field, op="IS_NOT_NULL")
            self.eat(("ID", "NULL"))
            return Cmp(field=field, op="IS_NULL")

        # BETWEEN x AND y
        if self._kw("BETWEEN"):
            self.eat()
            lo = self._value()
            self.eat(("ID", "AND"))
            hi = self._value()
            return Cmp(field=field, op="BETWEEN", value=(lo, hi))

        # IN (v1, v2, …)
        if self._kw("IN"):
            self.eat()
            self.eat(("OP", "("))
            vals = [self._value()]
            while self.peek() and self.peek()[0] == "OP" and self.peek()[1] == ",":
                self.eat()
                vals.append(self._value())
            self.eat(("OP", ")"))
            return Cmp(field=field, op="IN", value=vals)

        # CONTAINS 'substr' (list membership-ish)
        if self._kw("CONTAINS"):
            self.eat()
            v = self._value()
            return Cmp(field=field, op="CONTAINS", value=v)

        # Comparison ops
        op = self.eat()
        if op[0] != "OP" or op[1] not in {"=", "!=", "<", "<=", ">", ">="}:
            raise RuleParseError(f"expected comparison operator, got {op}")
        v = self._value()
        return Cmp(field=field, op=op[1], value=v)

    def _value(self):
        t = self.eat()
        if t[0] == "NUM":
            return float(t[1]) if "." in t[1] else int(t[1])
        if t[0] == "STR":
            return t[1]
        if t[0] == "BOOL":
            return t[1] == "true"
        if t[0] == "ID" and t[1].upper() in ("TRUE", "FALSE"):
            return t[1].upper() == "TRUE"
        if t[0] == "ID":
            # bare identifier used as a string literal (e.g. positive / negative)
            return t[1]
        raise RuleParseError(f"expected value, got {t}")

    def _kw(self, kw: str) -> bool:
        t = self.peek()
        return bool(t and t[0] == "ID" and t[1].upper() == kw)


def parse(expr: str):
    return _Parser(_tokenize(expr)).parse()


# ─────────────────────────────────────────────────────────────────────
# Static validator — used by protocol_parser before saving rule_dsl.
# ─────────────────────────────────────────────────────────────────────


def lint(expr: str, allowed_fields: Optional[set[str]] = None) -> list[str]:
    """Return a list of human-readable errors. Empty list = clean."""
    allowed = allowed_fields or set(PATIENT_FACTS_SCHEMA)
    try:
        ast = parse(expr)
    except RuleParseError as e:
        return [f"parse error: {e}"]
    errors: list[str] = []
    _collect_unknown_fields(ast, allowed, errors)
    return errors


def _collect_unknown_fields(node, allowed: set[str], errors: list[str]) -> None:
    if isinstance(node, Cmp):
        if node.field not in allowed:
            errors.append(
                f"unknown field {node.field!r}; allowed: {sorted(allowed)[:6]}…"
            )
    elif isinstance(node, (And, Or)):
        _collect_unknown_fields(node.left, allowed, errors)
        _collect_unknown_fields(node.right, allowed, errors)
    elif isinstance(node, Not):
        _collect_unknown_fields(node.expr, allowed, errors)


# ─────────────────────────────────────────────────────────────────────
# Evaluator
# ─────────────────────────────────────────────────────────────────────


Verdict = str  # 'pass' | 'fail' | 'unknown'


def _and(a: Verdict, b: Verdict) -> Verdict:
    if a == "fail" or b == "fail":
        return "fail"
    if a == "unknown" or b == "unknown":
        return "unknown"
    return "pass"


def _or(a: Verdict, b: Verdict) -> Verdict:
    if a == "pass" or b == "pass":
        return "pass"
    if a == "unknown" or b == "unknown":
        return "unknown"
    return "fail"


def _not(a: Verdict) -> Verdict:
    if a == "unknown":
        return "unknown"
    return "fail" if a == "pass" else "pass"


def _present(facts: PatientFacts, name: str) -> bool:
    v = getattr(facts, name, None)
    return v not in (None, "", [])


def evaluate(expr: str, facts: PatientFacts) -> Verdict:
    """Evaluate one rule expression against patient_facts.
    Returns 'pass' | 'fail' | 'unknown'.
    """
    try:
        ast = parse(expr)
    except RuleParseError:
        # An invalid rule shouldn't crash the eligibility engine;
        # return unknown so the medic sees a "?" + log.
        return "unknown"
    return _eval(ast, facts)


def _eval(node, facts: PatientFacts) -> Verdict:
    if isinstance(node, And):
        return _and(_eval(node.left, facts), _eval(node.right, facts))
    if isinstance(node, Or):
        return _or(_eval(node.left, facts), _eval(node.right, facts))
    if isinstance(node, Not):
        return _not(_eval(node.expr, facts))
    assert isinstance(node, Cmp)
    op = node.op
    v = getattr(facts, node.field, None)

    if op == "IS_NULL":
        return "pass" if v in (None, "", []) else "fail"
    if op == "IS_NOT_NULL":
        return "pass" if v not in (None, "", []) else "fail"
    if op == "EXISTS":
        return "pass" if _present(facts, node.field) else "fail"

    if not _present(facts, node.field):
        # field unset → can't decide
        return "unknown"

    if op in ("=", "!=", "<", "<=", ">", ">="):
        if isinstance(v, (int, float)) and isinstance(node.value, (int, float)):
            cmp = {"=":  v == node.value,
                   "!=": v != node.value,
                   "<":  v <  node.value,
                   "<=": v <= node.value,
                   ">":  v >  node.value,
                   ">=": v >= node.value}[op]
        else:
            sv, sn = str(v).lower(), str(node.value).lower()
            cmp = {"=":  sv == sn,
                   "!=": sv != sn,
                   "<":  sv <  sn,
                   "<=": sv <= sn,
                   ">":  sv >  sn,
                   ">=": sv >= sn}[op]
        return "pass" if cmp else "fail"

    if op == "BETWEEN":
        lo, hi = node.value
        try:
            ok = lo <= v <= hi
        except TypeError:
            return "unknown"
        return "pass" if ok else "fail"

    if op == "IN":
        if isinstance(v, list):
            inset = any(str(x).lower() in (str(c).lower() for c in node.value) for x in v)
        else:
            inset = str(v).lower() in (str(c).lower() for c in node.value)
        return "pass" if inset else "fail"

    if op == "CONTAINS":
        needle = str(node.value).lower()
        if isinstance(v, list):
            hit = any(needle in str(x).lower() for x in v)
        else:
            hit = needle in str(v).lower()
        return "pass" if hit else "fail"

    return "unknown"
