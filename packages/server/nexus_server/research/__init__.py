"""Research Workspace backend modules.

See docs/design/RESEARCH_WORKSPACE_DESIGN.md.

Submodules:
  patient_facts      — flatten per-patient SQL/graph state into the
                       structured fact view that rule_dsl + LLM judge
                       both read from.
  rule_dsl           — pure-Python evaluator for the restricted
                       comparison + IN + AND/OR + field-whitelist DSL
                       (auto-rule criteria).
  eligibility        — 3-stage engine: auto-rule → auto-llm →
                       overall recommendation. Writes
                       screening_evaluations + emits SCREENING_EVALUATED.
  schedule           — expand_schedule(): turn enrollment + schedule_json
                       into planned study_assessments rows.
  protocol_parser    — protocol .docx → draft inclusion/exclusion +
                       schedule for the batch-confirm UI (D7).
  reports            — interim report / CONSORT / Table 1 / KM
                       generators (Phase 4).
  observations       — auto-mirror SOAP / NODE_ADDED → study_observations.
"""
