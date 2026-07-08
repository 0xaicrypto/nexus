"""Structured patient_facts builder for the eligibility engine.

Design §5.1: every rule_dsl expression must only reference fields in
the shared ``patient_facts`` schema. Adding a new study with a new
required field requires extending this schema first.

This builder is a **pure SQL read** — no LLM calls, no network — so
it can run inside event handlers (per ``handlers.py:5-9``).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Schema — the closed set of fields rule_dsl may reference.
# ─────────────────────────────────────────────────────────────────────

PATIENT_FACTS_SCHEMA: dict[str, str] = {
    "age":                       "int",
    "sex":                       "str",
    "ecog":                      "int",
    "smoking_status":            "str",

    "pathology":                 "str",      # 'adenocarcinoma' | 'SCC' | …
    "ihc_markers":               "list[str]",
    "driver_mutations":          "list[str]",   # 'EGFR L858R', 'ALK', …
    "driver_mutation":           "str",      # convenience: 'positive'|'negative'|''
    "ngs_panel":                 "str",

    "ajcc_stage":                "str",      # 'IVA' | 'IVB' | 'III' …
    "valg_stage":                "str",      # 'LD' | 'ED'
    "oligometastatic":           "bool",
    "brain_mets":                "bool",
    "leptomeningeal":            "bool",

    "prior_lines":               "list[str]",  # ['chemo','io','targeted']
    "first_line_modality":       "str",      # 'chemo' | 'io' | 'chemo+io' | 'targeted' | ''
    "first_line_cycles":         "int",
    "best_response":             "str",      # 'CR'|'PR'|'SD'|'PD'

    # Labs
    "anc":                       "float",
    "plt":                       "float",
    "hb":                        "float",
    "alt":                       "float",
    "ast":                       "float",
    "tbil":                      "float",
    "creatinine":                "float",
    "urine_protein":             "str",      # '0' | '1+' | '2+' …
    "inr":                       "float",

    "comorbidities":             "list[str]",
    "active_infection":          "bool",
    "autoimmune_disease":        "bool",
    "interstitial_lung_disease": "bool",
    "ongoing_treatments":        "list[str]",
    "pregnant_or_lactating":     "bool",

    "informed_consent_signed":   "bool",
    "informed_consent_signed_at":"int",
}


@dataclass
class PatientFacts:
    """Structured patient view; missing fields stay None / [] / 0."""

    patient_hash:    str
    user_id:         str
    age:             Optional[int] = None
    sex:             str = ""
    ecog:            Optional[int] = None
    smoking_status:  str = ""

    pathology:       str = ""
    ihc_markers:     list[str] = field(default_factory=list)
    driver_mutations: list[str] = field(default_factory=list)
    driver_mutation: str = ""
    ngs_panel:       str = ""

    ajcc_stage:      str = ""
    valg_stage:      str = ""
    oligometastatic: Optional[bool] = None
    brain_mets:      Optional[bool] = None
    leptomeningeal:  Optional[bool] = None

    prior_lines:        list[str] = field(default_factory=list)
    first_line_modality: str = ""
    first_line_cycles:  Optional[int] = None
    best_response:      str = ""

    anc:    Optional[float] = None
    plt:    Optional[float] = None
    hb:     Optional[float] = None
    alt:    Optional[float] = None
    ast:    Optional[float] = None
    tbil:   Optional[float] = None
    creatinine: Optional[float] = None
    urine_protein: str = ""
    inr:    Optional[float] = None

    comorbidities:             list[str] = field(default_factory=list)
    active_infection:          Optional[bool] = None
    autoimmune_disease:        Optional[bool] = None
    interstitial_lung_disease: Optional[bool] = None
    ongoing_treatments:        list[str] = field(default_factory=list)
    pregnant_or_lactating:     Optional[bool] = None

    informed_consent_signed:    Optional[bool] = None
    informed_consent_signed_at: Optional[int] = None

    # raw evidence refs (graph node_ids, file_ids) — feeds the LLM
    # judge's evidence_refs requirement so it never invents source ids.
    _evidence_pool: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = {}
        for k in PATIENT_FACTS_SCHEMA:
            v = getattr(self, k, None)
            if v is None or v == "" or v == []:
                continue
            out[k] = v
        out["__patient_hash"] = self.patient_hash
        out["__evidence"]     = self._evidence_pool
        return out

    def get(self, k: str, default=None) -> Any:
        return getattr(self, k, default)


# ─────────────────────────────────────────────────────────────────────
# Builder
# ─────────────────────────────────────────────────────────────────────


def get_patient_facts(
    conn: sqlite3.Connection,
    user_id: str,
    patient_hash: str,
) -> PatientFacts:
    """Build a PatientFacts view from the SQL state.

    Source mix:
      - patients table       — age, sex, MRN, chief complaint
      - clinical_graph_nodes — finding / med / dx / measurement / lab nodes
      - study_enrollments    — consent_signed_at across studies

    All SQL, no LLM. Safe to call in event handlers.
    """
    facts = PatientFacts(user_id=user_id, patient_hash=patient_hash)
    facts._evidence_pool = {
        "patient": [], "graph_nodes": [], "enrollments": [],
    }

    # ─ Patients row ─
    try:
        row = conn.execute(
            "SELECT age_value, sex, chief_complaint, notes "
            "FROM patients WHERE user_id = ? AND patient_hash = ?",
            (user_id, patient_hash),
        ).fetchone()
        if row:
            if row[0]:
                facts.age = int(row[0])
            if row[1]:
                facts.sex = str(row[1])
            facts._evidence_pool["patient"].append(patient_hash)
    except sqlite3.Error as exc:
        logger.debug("patients row read failed: %s", exc)

    # ─ Clinical graph nodes ─
    try:
        rows = conn.execute(
            "SELECT node_id, node_type, content_json "
            "FROM clinical_graph_nodes "
            "WHERE user_id = ? AND patient_hash = ?",
            (user_id, patient_hash),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("clinical_graph_nodes read failed: %s", exc)
        rows = []

    for node_id, node_type, content_raw in rows:
        try:
            content = json.loads(content_raw or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        facts._evidence_pool["graph_nodes"].append(node_id)
        _merge_node_into_facts(facts, node_type, content)

    # ─ Derived flags ─
    if facts.driver_mutations:
        facts.driver_mutation = "positive"
    elif facts.ngs_panel and facts.ngs_panel.lower() in (
        "complete-negative", "negative", "all-negative"
    ):
        facts.driver_mutation = "negative"

    # ─ Consent signed (across any study) ─
    try:
        row = conn.execute(
            "SELECT MIN(consent_signed_at) FROM study_enrollments "
            "WHERE user_id = ? AND patient_hash = ? "
            "  AND consent_signed_at IS NOT NULL",
            (user_id, patient_hash),
        ).fetchone()
        if row and row[0]:
            facts.informed_consent_signed_at = int(row[0])
            facts.informed_consent_signed = True
    except sqlite3.Error:
        pass

    return facts


# ─────────────────────────────────────────────────────────────────────
# Node → facts merge
# ─────────────────────────────────────────────────────────────────────


def _norm(v) -> str:
    return str(v or "").strip().lower()


def _merge_node_into_facts(
    facts: PatientFacts, node_type: str, content: dict,
) -> None:
    """Mutate ``facts`` from one clinical_graph_nodes row.

    Node-type schemas vary; we look at labels / kinds inside content_json
    and pull whatever we can. Conservative: missing fields stay None.
    """
    nt = (node_type or "").lower()
    label = _norm(content.get("label"))
    kind  = _norm(content.get("kind"))
    val   = content.get("value")

    if nt in ("ecog", "performance_status"):
        try:
            facts.ecog = int(val) if val is not None else None
        except (TypeError, ValueError):
            pass

    elif nt == "stage" or kind == "stage":
        s = (content.get("value") or content.get("label") or "").upper()
        if s.startswith("IV"):
            facts.ajcc_stage = s.split()[0]  # 'IVA' / 'IVB' / 'IV'
        elif s.startswith("III") or s.startswith("II") or s.startswith("I"):
            facts.ajcc_stage = s.split()[0]
        if s in ("LD", "ED", "LIMITED", "EXTENSIVE"):
            facts.valg_stage = "LD" if s.startswith("L") else "ED"

    elif nt == "pathology" or kind == "pathology":
        facts.pathology = label or _norm(val) or facts.pathology

    elif nt == "biomarker" or kind in ("biomarker", "ihc"):
        if val:
            facts.ihc_markers.append(str(val))

    elif nt == "driver_mutation" or kind == "driver_mutation":
        # Positive driver mutations have a label like "EGFR L858R".
        if label and label not in ("negative", "none", ""):
            facts.driver_mutations.append(label)
        if _norm(content.get("ngs_panel_status")) in ("complete", "full"):
            facts.ngs_panel = "complete-negative" if not facts.driver_mutations \
                              else "positive"

    elif nt == "ngs_panel":
        facts.ngs_panel = _norm(val) or facts.ngs_panel

    elif nt == "treatment" or kind == "treatment":
        treat = label or _norm(val)
        if treat:
            facts.ongoing_treatments.append(treat)
        # First-line cycles
        if content.get("line") == 1 or kind == "first_line":
            n = content.get("cycles") or content.get("cycle_count")
            if n is not None:
                try:
                    facts.first_line_cycles = int(n)
                except (TypeError, ValueError):
                    pass
            mod = _norm(content.get("modality"))
            if mod:
                facts.first_line_modality = mod
            facts.prior_lines.append("first-line:" + (mod or treat))

    elif nt == "lab" or kind == "lab":
        name = _norm(content.get("name") or content.get("test"))
        try:
            num = float(content.get("value")) if content.get("value") is not None else None
        except (TypeError, ValueError):
            num = None
        if num is None:
            return
        if name in ("anc", "neutrophil"):       facts.anc = num
        elif name in ("plt", "platelet"):       facts.plt = num
        elif name in ("hb", "hemoglobin"):      facts.hb = num
        elif name in ("alt",):                  facts.alt = num
        elif name in ("ast",):                  facts.ast = num
        elif name in ("tbil", "total_bilirubin"): facts.tbil = num
        elif name in ("creatinine", "cr"):      facts.creatinine = num
        elif name in ("inr",):                  facts.inr = num

    elif nt == "comorbidity" or kind == "comorbidity":
        if label:
            facts.comorbidities.append(label)
        if "interstitial" in label or "ipf" in label:
            facts.interstitial_lung_disease = True
        if "autoimmune" in label:
            facts.autoimmune_disease = True

    elif nt in ("finding",):
        # If a finding mentions brain mets / leptomeningeal, surface it.
        text = label or _norm(content.get("text"))
        if "brain" in text and ("met" in text or "metast" in text):
            facts.brain_mets = True
        if "leptomeningeal" in text:
            facts.leptomeningeal = True

    elif nt == "response" or kind == "response":
        r = (content.get("value") or content.get("label") or "").upper()
        if r in ("CR", "PR", "SD", "PD"):
            facts.best_response = r


# ─────────────────────────────────────────────────────────────────────
# Optional: list all known patients of a user. Used by the
# eligibility rescan workflow.
# ─────────────────────────────────────────────────────────────────────


def list_known_patient_hashes(conn: sqlite3.Connection, user_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT patient_hash FROM patients WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    out = [r[0] for r in rows]
    # Also pull from clinical_graph_nodes — DICOM-derived patients that
    # never went through manual registration.
    try:
        extra = conn.execute(
            "SELECT DISTINCT patient_hash FROM clinical_graph_nodes "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        for (h,) in extra:
            if h and h not in out:
                out.append(h)
    except sqlite3.Error:
        pass
    return out
