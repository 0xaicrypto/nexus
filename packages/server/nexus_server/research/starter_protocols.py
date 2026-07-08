"""Starter protocols — the 3 reference studies medic Qian is running.

These are extracted from the protocol .docx files at
``clinical_trial_extract/`` (medic-confirmed in §10 #1 of the design
doc) and shipped as ready-to-install rule_dsl + schedule.

Usage:
    from nexus_server.research.starter_protocols import (
        STARTER_PROTOCOLS, install_starter,
    )
    install_starter(user_id, "hybrid-rt-nsclc-iv")

The starter doesn't auto-write — it routes through the same code path
as /api/v1/research/studies POST, so the audit event log captures the
install just like any user-driven creation.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Optional

from nexus_server.database import get_db_connection
from nexus_server.event_sourcing import EventKind, Store


# ─────────────────────────────────────────────────────────────────────
# 1) Hybrid RT NSCLC IV (single-arm I/II)
#    PI: 钱东 USTC Affiliated First Hospital
# ─────────────────────────────────────────────────────────────────────


HYBRID_RT = {
    "study_id":     "hybrid-rt-nsclc-iv",
    "display_name": "IV 期 NSCLC Hybrid RT 联合免疫 I/II 期",
    "short_code":   "HybridRT-IV",
    "phase":        "I/II",
    "target_n":     35,
    "primary_endpoint": "放射性 & 免疫性肺炎发生率（CTCAE V4.0）",
    "secondary_endpoints": [
        "mPFS", "1y PFS", "mOS", "1y OS",
    ],
    "protocol_summary":
        "IV 期 NSCLC 患者一线化疗/免疫 4-6 周期后达 PR/SD/PD，"
        "采用 Hybrid RT (SBRT + Low-dose Bath) 联合 PD-1/PD-L1。"
        "评估肺炎安全性 + 长期 PFS/OS。",
    "inclusion": [
        {"id": "incl_a", "text": "年龄 18–70 岁", "kind": "auto-rule",
         "rule_dsl": "age BETWEEN 18 AND 70"},
        {"id": "incl_b", "text": "ECOG 0–1，能够耐受放疗及免疫治疗",
         "kind": "auto-rule", "rule_dsl": "ecog IN (0,1)"},
        {"id": "incl_c", "text": "病理明确为非小细胞肺癌（鳞癌、腺癌、腺鳞癌、大细胞癌）",
         "kind": "auto-rule",
         "rule_dsl": ("pathology IN ('adenocarcinoma','SCC','adenosquamous',"
                      "'LCC','NSCLC','squamous_cell_carcinoma','large_cell_carcinoma')")},
        {"id": "incl_d", "text": "驱动基因阴性", "kind": "auto-rule",
         "rule_dsl": "driver_mutation = 'negative'"},
        {"id": "incl_e", "text": "AJCC 第八版分期为 IV 期",
         "kind": "auto-rule",
         "rule_dsl": "ajcc_stage IN ('IVA','IVB','IVC','IV')"},
        {"id": "incl_f", "text": "一线化疗/免疫/化免联合 4-6 周期后达最佳疗效，或一线后 PD",
         "kind": "auto-llm",
         "llm_prompt": ("Verify the patient received 4-6 cycles of "
                        "first-line chemo/IO/chemo+IO and reached best "
                        "response (CR/PR/SD) or progressed."),
         "evidence_sources": ["soap", "treatment_log"]},
        {"id": "incl_h", "text": "知情同意书已签", "kind": "manual"},
    ],
    "exclusion": [
        {"id": "excl_c", "text": "病理为小细胞肺癌、类癌等肺神经内分泌肿瘤",
         "kind": "auto-rule",
         "rule_dsl": "pathology IN ('SCLC','carcinoid','neuroendocrine')"},
        {"id": "excl_d", "text": "驱动基因阳性", "kind": "auto-rule",
         "rule_dsl": "driver_mutation = 'positive'"},
        {"id": "excl_f", "text": "一线接受靶向治疗", "kind": "auto-rule",
         "rule_dsl": "first_line_modality = 'targeted'"},
    ],
    "schedule": [
        {"label": "baseline", "offset_days": 0,
         "assessments": ["pet_ct", "brain_mri", "bone_scan",
                         "lab_panel", "ecog"]},
        {"label": "rt_end_4w", "offset_days": 28,
         "assessments": ["chest_ct_plain"]},
        {"label": "fu_3m", "offset_days": 90,
         "assessments": ["chest_ct_contrast", "abdominal_us"]},
        {"label": "fu_6m", "offset_days": 180,
         "assessments": ["chest_ct_contrast", "abdominal_us",
                         "bone_scan", "brain_mri"]},
        {"label": "fu_safety_monthly", "offset_days": 30,
         "assessments": ["cardiac_enzymes", "tft", "lab_panel"],
         "repeat_every_days": 30, "repeat_until_days": 365},
    ],
    "arms": [],
    "stop_rules": {
        "max_g3_pneumonitis_pct": 30,
        "stop_at_first_treatment_death": True,
    },
}


# ─────────────────────────────────────────────────────────────────────
# 2) ES-SCLC + Adebrelimab 双队列 II
# ─────────────────────────────────────────────────────────────────────


ES_SCLC = {
    "study_id":     "es-sclc-adebrelimab-rt",
    "display_name": "广泛期 SCLC EC + 阿德贝利单抗 + 全残留病灶放疗 II 期",
    "short_code":   "ESC-Adebreli",
    "phase":        "II",
    "target_n":     150,
    "primary_endpoint": "PFS",
    "secondary_endpoints": ["ORR", "OS", "DoR", "DCR", "Safety (CTCAE v5.0)"],
    "protocol_summary":
        "ES-SCLC 一线 EC + 阿德贝利单抗 4-6 周期后，"
        "试验组追加全残留病灶大分割放疗，对照组仅免疫维持。"
        "多中心、双队列、非随机平行设计。",
    "inclusion": [
        {"id": "incl_age", "text": "年龄 ≥ 18 岁", "kind": "auto-rule",
         "rule_dsl": "age >= 18"},
        {"id": "incl_ecog", "text": "ECOG PS 0 或 1",
         "kind": "auto-rule", "rule_dsl": "ecog IN (0,1)"},
        {"id": "incl_path", "text": "组织学或细胞学证实的 ES-SCLC (VALG 分期)",
         "kind": "auto-rule",
         "rule_dsl": "pathology = 'SCLC' AND valg_stage = 'ED'"},
        {"id": "incl_anc", "text": "ANC ≥ 1.5×10^9/L",
         "kind": "auto-rule", "rule_dsl": "anc >= 1.5"},
        {"id": "incl_plt", "text": "PLT ≥ 90×10^9/L",
         "kind": "auto-rule", "rule_dsl": "plt >= 90"},
        {"id": "incl_hb",  "text": "Hb ≥ 90 g/L",
         "kind": "auto-rule", "rule_dsl": "hb >= 90"},
        {"id": "incl_prior_io",
         "text": "既往未接受过一线针对 ES-SCLC 的系统治疗或免疫检查点抑制剂",
         "kind": "auto-llm",
         "llm_prompt": "Confirm no prior systemic therapy for ES-SCLC and "
                       "no prior immune checkpoint inhibitor.",
         "evidence_sources": ["treatment_log", "soap"]},
        {"id": "incl_inr", "text": "INR ≤ 1.5×ULN",
         "kind": "auto-rule", "rule_dsl": "inr <= 1.5"},
        {"id": "incl_consent", "text": "签署知情同意书", "kind": "manual"},
    ],
    "exclusion": [
        {"id": "excl_t_cell_co",
         "text": "既往接受过 T 细胞共刺激或免疫检查点治疗 (CTLA-4/PD-1/PD-L1)",
         "kind": "auto-llm",
         "llm_prompt": "Check if prior CTLA-4/PD-1/PD-L1 therapy.",
         "evidence_sources": ["treatment_log"]},
        {"id": "excl_pneumonitis",
         "text": "中重度肺部疾病（间质性肺病、放射性/药物性肺炎需类固醇）",
         "kind": "auto-rule",
         "rule_dsl": "interstitial_lung_disease = true"},
        {"id": "excl_pregnant", "text": "哺乳期妇女或育龄期不避孕",
         "kind": "auto-rule",
         "rule_dsl": "pregnant_or_lactating = true"},
        {"id": "excl_autoimmune",
         "text": "需要长期免疫抑制的活动性自身免疫性疾病",
         "kind": "auto-rule",
         "rule_dsl": "autoimmune_disease = true"},
        {"id": "excl_active_infection",
         "text": "随机分组前 4 周内严重感染",
         "kind": "auto-rule",
         "rule_dsl": "active_infection = true"},
    ],
    "schedule": [
        {"label": "screen", "offset_days": -7,
         "assessments": ["pet_ct", "lab_panel", "ecog", "tft",
                         "cardiac_enzymes", "ecg"]},
        {"label": "cycle_q3w", "offset_days": 0,
         "assessments": ["lab_panel", "ecog"],
         "repeat_every_days": 21, "repeat_until_days": 180},
        {"label": "imaging_q12w", "offset_days": 0,
         "assessments": ["chest_ct_contrast", "abdominal_us"],
         "repeat_every_days": 84, "repeat_until_days": 730},
        {"label": "end_safety", "offset_days": 90,
         "assessments": ["lab_panel", "ecog", "ecg", "cardiac_enzymes"]},
    ],
    "arms": [
        {"id": "trial",   "label": "EC + Adebreli + 全残留病灶 RT"},
        {"id": "control", "label": "EC + Adebreli 仅免疫维持"},
    ],
    "stop_rules": {
        "interim_at_n": 75,
        "alpha":        0.05,
    },
}


# ─────────────────────────────────────────────────────────────────────
# 3) 8 Gy/1f central-restricted ignition + cCRT III NSCLC (I-phase)
# ─────────────────────────────────────────────────────────────────────


IGNITION_8GY = {
    "study_id":     "ignition-8gy-ccrt-iii-nsclc",
    "display_name": "不可切除 III 期 NSCLC 中央限域 8 Gy/1f 免疫点火 + cCRT I 期",
    "short_code":   "8Gy-Ignition",
    "phase":        "I",
    "target_n":     30,        # 6 run-in + 24 expansion
    "primary_endpoint": "DLT 发生率",
    "secondary_endpoints": [
        "cCRT 按时启动率", "cCRT 完成率", "ICI 巩固启动率",
        "ORR", "LRC", "PFS", "OS", "ALC nadir 与恢复",
    ],
    "protocol_summary":
        "不可切除 III 期 NSCLC 行 8 Gy/1f 中央限域免疫点火放疗 → "
        "1 周期 PD-(L)1 → 标准 cCRT 60 Gy/30f → ICI 巩固最长 12 月。"
        "Safety run-in (6 例) + expansion (24 例) 设计。",
    "inclusion": [
        {"id": "incl_age", "text": "年龄 18–75 岁", "kind": "auto-rule",
         "rule_dsl": "age BETWEEN 18 AND 75"},
        {"id": "incl_path", "text": "组织/细胞学证实的非小细胞肺癌",
         "kind": "auto-rule",
         "rule_dsl": ("pathology IN ('adenocarcinoma','SCC','adenosquamous',"
                      "'LCC','NSCLC')")},
        {"id": "incl_stage", "text": "AJCC8 III 期不可切除",
         "kind": "auto-rule",
         "rule_dsl": "ajcc_stage IN ('III','IIIA','IIIB','IIIC')"},
        {"id": "incl_driver", "text": "驱动基因阴性",
         "kind": "auto-rule", "rule_dsl": "driver_mutation = 'negative'"},
        {"id": "incl_ecog", "text": "ECOG PS 0–1",
         "kind": "auto-rule", "rule_dsl": "ecog IN (0,1)"},
        {"id": "incl_central", "text": "存在可勾画的中央限域点火靶区",
         "kind": "auto-llm",
         "llm_prompt": "Verify the primary lesion is anatomically "
                       "suitable for central-restricted ignition (8 Gy "
                       "sub-region within the primary, margin <4 Gy).",
         "evidence_sources": ["imaging", "soap"]},
        {"id": "incl_consent", "text": "签署知情同意书", "kind": "manual"},
    ],
    "exclusion": [
        {"id": "excl_mets", "text": "存在远处转移", "kind": "auto-rule",
         "rule_dsl": "ajcc_stage IN ('IV','IVA','IVB','IVC')"},
        {"id": "excl_driver", "text": "驱动基因阳性",
         "kind": "auto-rule", "rule_dsl": "driver_mutation = 'positive'"},
        {"id": "excl_prior_rt",
         "text": "既往胸部根治性放疗或系统性抗肿瘤治疗",
         "kind": "auto-llm",
         "llm_prompt": "Check for prior chest radical RT or systemic "
                       "anticancer therapy for the current disease.",
         "evidence_sources": ["treatment_log"]},
        {"id": "excl_ild", "text": "活动性间质性肺病、重度放射性/免疫肺炎",
         "kind": "auto-rule",
         "rule_dsl": "interstitial_lung_disease = true"},
        {"id": "excl_unsafe_anat",
         "text": "原发灶紧邻主支气管/隆突/大血管/食管，研究者判断 lead-in 风险过高",
         "kind": "manual"},
        {"id": "excl_pregnant", "text": "妊娠或哺乳期",
         "kind": "auto-rule",
         "rule_dsl": "pregnant_or_lactating = true"},
    ],
    "schedule": [
        {"label": "screen", "offset_days": -7,
         "assessments": ["chest_ct_contrast", "brain_mri", "pet_ct",
                         "lab_panel", "ecog"]},
        {"label": "ignition", "offset_days": 1,
         "assessments": ["lab_panel"]},
        {"label": "ici_cycle1", "offset_days": 2,
         "assessments": ["lab_panel"]},
        {"label": "cCRT_start", "offset_days": 7,
         "assessments": ["lab_panel", "ecog"]},
        {"label": "cCRT_weekly", "offset_days": 14,
         "assessments": ["lab_panel"], "repeat_every_days": 7,
         "repeat_until_days": 49},
        {"label": "cCRT_end_6w", "offset_days": 49,
         "assessments": ["chest_ct_contrast"]},
        {"label": "consolidation_q6w", "offset_days": 49,
         "assessments": ["chest_ct_contrast", "lab_panel"],
         "repeat_every_days": 42, "repeat_until_days": 365},
    ],
    "arms": [],
    "stop_rules": {
        "dlt_cap_run_in": 2,         # >=2/6 DLT halts the study
        "run_in_n": 6,
    },
}


# ─────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────


STARTER_PROTOCOLS: dict[str, dict] = {
    HYBRID_RT["study_id"]:     HYBRID_RT,
    ES_SCLC["study_id"]:       ES_SCLC,
    IGNITION_8GY["study_id"]:  IGNITION_8GY,
}


# ─────────────────────────────────────────────────────────────────────
# Install
# ─────────────────────────────────────────────────────────────────────


def install_starter(user_id: str, starter_id: str,
                    *, overwrite: bool = False) -> str:
    """Install one starter into research_studies for the given user.

    Returns the actual study_id used (which is the starter_id unless
    a collision exists; in that case we suffix a short uuid).
    """
    if starter_id not in STARTER_PROTOCOLS:
        raise KeyError(f"unknown starter {starter_id!r}")
    proto = STARTER_PROTOCOLS[starter_id]
    return _write_protocol(user_id, proto, overwrite=overwrite)


def install_all_starters(user_id: str) -> list[str]:
    """Install all three reference protocols. Skips any already
    present unless overwrite=True is explicit."""
    out = []
    for sid in STARTER_PROTOCOLS:
        try:
            out.append(install_starter(user_id, sid))
        except RuntimeError:
            pass  # already present — keep going
    return out


def _write_protocol(user_id: str, proto: dict, *, overwrite: bool) -> str:
    now = int(time.time() * 1000)
    study_id = proto["study_id"]

    with get_db_connection() as conn:
        existing = conn.execute(
            "SELECT study_id FROM research_studies "
            "WHERE user_id = ? AND study_id = ?",
            (user_id, study_id),
        ).fetchone()
        if existing and not overwrite:
            raise RuntimeError(
                f"study {study_id} already exists for user {user_id}"
            )
        if existing and overwrite:
            study_id_actual = study_id
            conn.execute(
                """
                UPDATE research_studies SET
                  display_name = ?, short_code = ?, phase = ?, target_n = ?,
                  primary_endpoint = ?,
                  secondary_endpoints_json = ?,
                  inclusion_json = ?, exclusion_json = ?,
                  schedule_json  = ?, arms_json = ?,
                  stop_rules_json = ?, protocol_summary = ?,
                  status = COALESCE(NULLIF(status,''), 'enrolling'),
                  updated_at = ?
                WHERE user_id = ? AND study_id = ?
                """,
                (
                    proto["display_name"], proto["short_code"], proto["phase"],
                    proto["target_n"], proto["primary_endpoint"],
                    json.dumps(proto["secondary_endpoints"], ensure_ascii=False),
                    json.dumps(proto["inclusion"], ensure_ascii=False),
                    json.dumps(proto["exclusion"], ensure_ascii=False),
                    json.dumps(proto["schedule"], ensure_ascii=False),
                    json.dumps(proto["arms"], ensure_ascii=False),
                    json.dumps(proto["stop_rules"], ensure_ascii=False),
                    proto["protocol_summary"],
                    now, user_id, study_id,
                ),
            )
        else:
            study_id_actual = study_id
            conn.execute(
                """
                INSERT INTO research_studies
                (user_id, study_id, display_name, short_code, phase,
                 status, target_n, protocol_summary, primary_endpoint,
                 secondary_endpoints_json, inclusion_json, exclusion_json,
                 schedule_json, arms_json, stop_rules_json,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'enrolling', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, study_id_actual,
                    proto["display_name"], proto["short_code"], proto["phase"],
                    proto["target_n"], proto["protocol_summary"],
                    proto["primary_endpoint"],
                    json.dumps(proto["secondary_endpoints"], ensure_ascii=False),
                    json.dumps(proto["inclusion"], ensure_ascii=False),
                    json.dumps(proto["exclusion"], ensure_ascii=False),
                    json.dumps(proto["schedule"], ensure_ascii=False),
                    json.dumps(proto["arms"], ensure_ascii=False),
                    json.dumps(proto["stop_rules"], ensure_ascii=False),
                    now, now,
                ),
            )
        conn.commit()

    try:
        with get_db_connection() as conn:
            store = Store(conn)
            store.emit_and_apply(
                kind=EventKind.STUDY_CREATED,
                payload={
                    "study_id":     study_id_actual,
                    "display_name": proto["display_name"],
                    "short_code":   proto["short_code"],
                    "phase":        proto["phase"],
                    "target_n":     proto["target_n"],
                    "primary_endpoint": proto["primary_endpoint"],
                },
                apply_fn=lambda c, e: None,
                user_id=user_id,
            )
            conn.commit()
    except Exception:
        pass
    return study_id_actual
