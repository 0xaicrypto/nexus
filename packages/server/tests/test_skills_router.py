"""Skills management API (/api/v1/skills) + v2 chat injection tests.

Covers:
  * list: empty at first, shows installed skills with the prefs overlay
  * install: monkeypatched SkillManager.install (no network), duplicate
    → 409 already_installed, empty identifier → 422 invalid_identifier
  * search: proxied results with installed flag, backend failure → 502
    search_unavailable, bad source → 422
  * toggle: persists enabled/auto_apply to user_skill_prefs
  * v2 chat prompt injection: requested+enabled skill content lands in
    the LLM system prompt; disabled or unrequested skills don't;
    auto_apply skills ride along on every turn
  * uninstall: removes the on-disk dir + prefs, second delete → 404
  * per-user isolation: skills live under {TWIN_BASE_DIR}/{user_id}/skills
    so user B never sees user A's installs
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

PW = "Str0ng-Pass-123"

SKILL_MARKER = "SKILLMARKER-HAIKU-7791"
SKILL_BODY = (
    "---\n"
    "name: haiku-mode\n"
    "description: Answer in haiku form\n"
    "---\n"
    "\n"
    f"When this skill is active, always respond in traditional haiku "
    f"form (5-7-5 syllables). {SKILL_MARKER}\n"
)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _register(client, name):
    r = client.post("/api/v1/auth/register",
                    json={"username": name, "password": PW})
    assert r.status_code == 201, r.text
    return r.json()


def _auth(user):
    return {"Authorization": f"Bearer {user['jwt_token']}"}


def _err_code(r):
    """main.http_exception_handler reshapes HTTPException detail into
    {"error": <detail>, "status_code": ..., "timestamp": ...}."""
    body = r.json()
    detail = body.get("error", body.get("detail"))
    assert isinstance(detail, dict), body
    return detail["code"]


def _patch_install(monkeypatch, body=SKILL_BODY):
    """Replace SkillManager.install_pack (the endpoint's entry point)
    with a network-free fake that materialises a folder-layout skill
    in the manager's skills dir."""
    from nexus_core.skills.manager import SkillManager

    calls: list[str] = []

    async def fake_install_pack(self, source):
        calls.append(source)
        # Same name derivation the real installers use: last segment.
        name = source.split(":")[-1].split("/")[-1]
        d = self._skills_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")
        skill = self._load_skill_folder(d)
        self._skills[skill.name] = skill
        return [skill]

    monkeypatch.setattr(SkillManager, "install_pack", fake_install_pack)
    return calls


def _install(client, user, identifier="anthropic:haiku-mode"):
    r = client.post("/api/v1/skills/install",
                    json={"identifier": identifier}, headers=_auth(user))
    assert r.status_code == 200, r.text
    return r.json()


def _patch_llm(monkeypatch):
    """Capture llm_gateway.call_llm invocations from the v2 chat path."""
    from nexus_server import llm_gateway

    calls: list[dict] = []

    async def fake_call_llm(*, messages, system_prompt, model,
                            temperature, max_tokens, tools):
        calls.append({"messages": messages, "system_prompt": system_prompt})
        return ("ok — stubbed answer", "gemini-2.5-flash", "stop", [])

    monkeypatch.setattr(llm_gateway, "call_llm", fake_call_llm)
    return calls


def _chat(client, user, text="hello there, quick question",
          skills=None, session_id="sess-skills-1"):
    payload = {"text": text, "session_id": session_id}
    if skills is not None:
        payload["skills"] = skills
    r = client.post("/api/v1/agent/chat", json=payload, headers=_auth(user))
    assert r.status_code == 200, r.text
    frames = []
    for block in r.text.split("\n\n"):
        block = block.strip()
        if block.startswith("data: "):
            frames.append(json.loads(block[len("data: "):]))
    return frames


def _skills_dir(user):
    base = os.environ["NEXUS_TWIN_BASE_DIR"]
    return pathlib.Path(base) / user["user_id"] / "skills"


# ─────────────────────────────────────────────────────────────────────
# List / install / toggle / uninstall lifecycle
# ─────────────────────────────────────────────────────────────────────


def test_list_requires_auth(client):
    assert client.get("/api/v1/skills").status_code == 401


def test_list_empty_initially(client):
    user = _register(client, "alice")
    r = client.get("/api/v1/skills", headers=_auth(user))
    assert r.status_code == 200
    assert r.json() == {"skills": []}


def test_install_then_list_shows_skill(client, monkeypatch):
    user = _register(client, "alice")
    calls = _patch_install(monkeypatch)

    body = _install(client, user)
    assert body["ok"] is True
    assert body["skill"]["name"] == "haiku-mode"
    assert "haiku" in body["skill"]["description"].lower()
    assert calls == ["anthropic:haiku-mode"]

    r = client.get("/api/v1/skills", headers=_auth(user))
    skills = r.json()["skills"]
    assert len(skills) == 1
    row = skills[0]
    assert row["name"] == "haiku-mode"
    assert row["description"] == "Answer in haiku form"
    assert row["source"] == "official"
    assert row["enabled"] is True
    assert row["auto_apply"] is False
    assert row["invocable"] is True
    assert row["installed_at"]  # non-empty ISO timestamp


def test_install_duplicate_409(client, monkeypatch):
    user = _register(client, "alice")
    _patch_install(monkeypatch)
    _install(client, user)
    r = client.post("/api/v1/skills/install",
                    json={"identifier": "anthropic:haiku-mode"},
                    headers=_auth(user))
    assert r.status_code == 409
    assert _err_code(r) == "already_installed"


def test_install_pack_returns_count_and_all_names(client, monkeypatch):
    """A repo-root 'skill pack' install returns every installed skill
    (count + skills[]), keeps the backward-compat .skill first entry,
    upserts a pref row for EACH skill, and dedupes by name."""
    from nexus_core.skills.manager import SkillManager

    names = ["paper-alpha", "paper-beta", "paper-gamma"]

    async def fake_pack(self, source):
        out = []
        for n in names:
            d = self._skills_dir / n
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(
                f"---\nname: {n}\ndescription: skill {n}\n---\n\nbody {n}\n",
                encoding="utf-8",
            )
            skill = self._load_skill_folder(d)
            self._skills[skill.name] = skill
            out.append(skill)
        # Duplicate entry — the endpoint must dedupe by name, not 409.
        return out + [out[0]]

    monkeypatch.setattr(SkillManager, "install_pack", fake_pack)
    user = _register(client, "alice")
    r = client.post("/api/v1/skills/install",
                    json={"identifier": "https://github.com/x/pack-repo"},
                    headers=_auth(user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["count"] == 3
    assert [s["name"] for s in body["skills"]] == names
    # Backward-compat single-skill key = first installed.
    assert body["skill"]["name"] == names[0]

    # Prefs upserted for each skill → list shows all three enabled.
    rows = client.get("/api/v1/skills", headers=_auth(user)).json()["skills"]
    by_name = {x["name"]: x for x in rows}
    assert set(by_name) == set(names)
    for n in names:
        assert by_name[n]["enabled"] is True
        assert by_name[n]["source"] == "github"
        assert by_name[n]["installed_at"]


def test_install_empty_identifier_422(client):
    user = _register(client, "alice")
    r = client.post("/api/v1/skills/install",
                    json={"identifier": "   "}, headers=_auth(user))
    assert r.status_code == 422
    assert _err_code(r) == "invalid_identifier"


def test_install_value_error_maps_to_422(client, monkeypatch):
    from nexus_core.skills.manager import SkillManager

    async def bad_install(self, source):
        raise ValueError(f"Cannot parse GitHub URL: {source}")

    monkeypatch.setattr(SkillManager, "install_pack", bad_install)
    user = _register(client, "alice")
    r = client.post("/api/v1/skills/install",
                    json={"identifier": "https://github.com/x"},
                    headers=_auth(user))
    assert r.status_code == 422
    assert _err_code(r) == "invalid_identifier"


def test_toggle_persists_and_lists(client, monkeypatch):
    user = _register(client, "alice")
    _patch_install(monkeypatch)
    _install(client, user)

    r = client.post("/api/v1/skills/haiku-mode/toggle",
                    json={"enabled": False}, headers=_auth(user))
    assert r.status_code == 200
    assert r.json() == {"ok": True, "enabled": False, "auto_apply": False}

    rows = client.get("/api/v1/skills", headers=_auth(user)).json()["skills"]
    assert rows[0]["enabled"] is False

    # Re-enable with auto_apply.
    r = client.post("/api/v1/skills/haiku-mode/toggle",
                    json={"enabled": True, "auto_apply": True},
                    headers=_auth(user))
    assert r.json() == {"ok": True, "enabled": True, "auto_apply": True}
    rows = client.get("/api/v1/skills", headers=_auth(user)).json()["skills"]
    assert rows[0]["enabled"] is True
    assert rows[0]["auto_apply"] is True


def test_toggle_unknown_skill_404(client):
    user = _register(client, "alice")
    r = client.post("/api/v1/skills/nope/toggle",
                    json={"enabled": False}, headers=_auth(user))
    assert r.status_code == 404
    assert _err_code(r) == "not_installed"


def test_uninstall_removes_disk_and_prefs(client, monkeypatch):
    user = _register(client, "alice")
    _patch_install(monkeypatch)
    _install(client, user)
    assert (_skills_dir(user) / "haiku-mode" / "SKILL.md").exists()

    r = client.delete("/api/v1/skills/haiku-mode", headers=_auth(user))
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert not (_skills_dir(user) / "haiku-mode").exists()
    assert client.get("/api/v1/skills",
                      headers=_auth(user)).json()["skills"] == []

    # Second delete → 404 not_installed.
    r = client.delete("/api/v1/skills/haiku-mode", headers=_auth(user))
    assert r.status_code == 404
    assert _err_code(r) == "not_installed"


def test_uninstall_path_traversal_rejected(client):
    user = _register(client, "alice")
    r = client.delete("/api/v1/skills/..%2F..%2Fetc", headers=_auth(user))
    assert r.status_code in (404, 422)  # never a deletion outside skills dir


# ─────────────────────────────────────────────────────────────────────
# Search proxy
# ─────────────────────────────────────────────────────────────────────


def test_search_official_maps_results(client, monkeypatch):
    from nexus_core.skills.manager import SkillManager

    async def fake_search(self, query, limit=10):
        self._anthropic_listing_cache = []  # mark listing as reachable
        return [{
            "identifier": "anthropic:pdf",
            "name": "pdf",
            "description": "Work with PDFs",
            "source": "anthropic",
            "url": "https://github.com/anthropics/skills/tree/main/skills/pdf",
        }]

    monkeypatch.setattr(SkillManager, "search_anthropic_official", fake_search)
    user = _register(client, "alice")
    r = client.get("/api/v1/skills/search?q=pdf&source=official",
                   headers=_auth(user))
    assert r.status_code == 200
    results = r.json()["results"]
    assert results == [{
        "identifier": "anthropic:pdf",
        "name": "pdf",
        "description": "Work with PDFs",
        "source": "official",
        "installed": False,
    }]


def test_search_marks_installed(client, monkeypatch):
    from nexus_core.skills.manager import SkillManager
    _patch_install(monkeypatch)
    user = _register(client, "alice")
    _install(client, user)

    async def fake_search(self, query, limit=10):
        self._anthropic_listing_cache = []
        return [{"identifier": "anthropic:haiku-mode",
                 "name": "haiku-mode", "description": "d"}]

    monkeypatch.setattr(SkillManager, "search_anthropic_official", fake_search)
    r = client.get("/api/v1/skills/search?q=haiku&source=official",
                   headers=_auth(user))
    assert r.json()["results"][0]["installed"] is True


def test_search_network_failure_502(client, monkeypatch):
    from nexus_core.skills.manager import SkillManager

    async def boom(self, query, limit=10):
        raise OSError("connection refused")

    monkeypatch.setattr(SkillManager, "search_github_topic", boom)
    user = _register(client, "alice")
    r = client.get("/api/v1/skills/search?q=x&source=github",
                   headers=_auth(user))
    assert r.status_code == 502
    assert _err_code(r) == "search_unavailable"


def test_search_bad_source_422(client):
    user = _register(client, "alice")
    r = client.get("/api/v1/skills/search?q=x&source=lobehub",
                   headers=_auth(user))
    assert r.status_code == 422
    assert _err_code(r) == "invalid_source"


# ─────────────────────────────────────────────────────────────────────
# Offline fallback — official search serves the built-in catalog when
# GitHub is unreachable (GFW); install failures map to install_network
# ─────────────────────────────────────────────────────────────────────


def _patch_official_search_down(monkeypatch):
    from nexus_core.skills.manager import SkillManager

    async def boom(self, query, limit=10):
        raise OSError("Tunnel connection failed: 403 Forbidden")

    monkeypatch.setattr(SkillManager, "search_anthropic_official", boom)


def test_search_official_unreachable_falls_back_to_cached_catalog(
    client, monkeypatch,
):
    _patch_official_search_down(monkeypatch)
    user = _register(client, "alice")
    r = client.get("/api/v1/skills/search?q=&source=official",
                   headers=_auth(user))
    assert r.status_code == 200
    results = r.json()["results"]
    ids = {x["identifier"] for x in results}
    assert {"anthropic:docx", "anthropic:pdf",
            "anthropic:pptx", "anthropic:xlsx"} <= ids
    for row in results:
        assert row["source"] == "official"
        assert row["cached"] is True
        assert row["installed"] is False
        assert row["name"]
        assert row["description"]


def test_search_official_fallback_filters_by_q(client, monkeypatch):
    _patch_official_search_down(monkeypatch)
    user = _register(client, "alice")

    r = client.get("/api/v1/skills/search?q=pdf&source=official",
                   headers=_auth(user))
    assert r.status_code == 200
    results = r.json()["results"]
    assert [x["identifier"] for x in results] == ["anthropic:pdf"]
    assert results[0]["cached"] is True

    # Description terms match too; nonsense terms match nothing.
    r = client.get("/api/v1/skills/search?q=spreadsheet&source=official",
                   headers=_auth(user))
    assert [x["identifier"] for x in r.json()["results"]] == ["anthropic:xlsx"]
    r = client.get("/api/v1/skills/search?q=zzz-no-match&source=official",
                   headers=_auth(user))
    assert r.json()["results"] == []


def test_search_official_fallback_marks_installed(client, monkeypatch):
    _patch_official_search_down(monkeypatch)
    # Fake installer whose frontmatter name matches the identifier tail
    # ('pdf') so the installed-overlay lookup lines up.
    _patch_install(monkeypatch, body=SKILL_BODY.replace("haiku-mode", "pdf"))
    user = _register(client, "alice")
    _install(client, user, identifier="anthropic:pdf")  # installs 'pdf'

    r = client.get("/api/v1/skills/search?q=&source=official",
                   headers=_auth(user))
    by_id = {x["identifier"]: x for x in r.json()["results"]}
    assert by_id["anthropic:pdf"]["installed"] is True
    assert by_id["anthropic:docx"]["installed"] is False


def test_search_github_unreachable_still_502(client, monkeypatch):
    """Only source=official has an offline catalog — github keeps the
    502 search_unavailable contract."""
    from nexus_core.skills.manager import SkillManager

    async def boom(self, query, limit=10):
        raise OSError("Tunnel connection failed: 403 Forbidden")

    monkeypatch.setattr(SkillManager, "search_github_topic", boom)
    user = _register(client, "alice")
    r = client.get("/api/v1/skills/search?q=x&source=github",
                   headers=_auth(user))
    assert r.status_code == 502
    assert _err_code(r) == "search_unavailable"


def test_install_network_failure_maps_to_install_network(
    client, monkeypatch,
):
    from nexus_core.skills.manager import SkillManager

    async def net_boom(self, source):
        raise OSError("Tunnel connection failed: 403 Forbidden")

    monkeypatch.setattr(SkillManager, "install_pack", net_boom)
    user = _register(client, "alice")
    r = client.post("/api/v1/skills/install",
                    json={"identifier": "anthropic:pdf"},
                    headers=_auth(user))
    assert r.status_code == 502
    assert _err_code(r) == "install_network"
    body = r.json()
    detail = body.get("error", body.get("detail"))
    # The message must point the user at the mirror env var.
    assert "NEXUS_GITHUB_MIRROR" in detail["message"]


def test_install_non_network_failure_stays_install_failed(
    client, monkeypatch,
):
    from nexus_core.skills.manager import SkillManager

    async def boom(self, source):
        raise RuntimeError(
            "x/y is a multi-skill repo with 3 skills: a, b, c. Pick one."
        )

    monkeypatch.setattr(SkillManager, "install_pack", boom)
    user = _register(client, "alice")
    r = client.post("/api/v1/skills/install",
                    json={"identifier": "x/y"}, headers=_auth(user))
    assert r.status_code == 502
    assert _err_code(r) == "install_failed"


# ─────────────────────────────────────────────────────────────────────
# v2 chat prompt injection
# ─────────────────────────────────────────────────────────────────────


def test_chat_injects_requested_enabled_skill(client, monkeypatch):
    user = _register(client, "alice")
    _patch_install(monkeypatch)
    _install(client, user)
    llm_calls = _patch_llm(monkeypatch)

    frames = _chat(client, user, skills=["haiku-mode"])
    kinds = [f["type"] for f in frames]
    assert "skills_applied" in kinds
    applied = next(f for f in frames if f["type"] == "skills_applied")
    assert applied["skills"] == ["haiku-mode"]
    assert "turn_complete" in kinds

    assert len(llm_calls) == 1
    sp = llm_calls[0]["system_prompt"]
    assert "## Skill: haiku-mode" in sp
    assert SKILL_MARKER in sp
    assert "ACTIVE SKILLS" in sp


def test_chat_disabled_skill_not_injected(client, monkeypatch):
    user = _register(client, "alice")
    _patch_install(monkeypatch)
    _install(client, user)
    client.post("/api/v1/skills/haiku-mode/toggle",
                json={"enabled": False}, headers=_auth(user))
    llm_calls = _patch_llm(monkeypatch)

    frames = _chat(client, user, skills=["haiku-mode"])
    assert all(f["type"] != "skills_applied" for f in frames)
    assert len(llm_calls) == 1
    assert SKILL_MARKER not in llm_calls[0]["system_prompt"]
    assert "## Skill: haiku-mode" not in llm_calls[0]["system_prompt"]


def test_chat_unrequested_skill_not_injected(client, monkeypatch):
    user = _register(client, "alice")
    _patch_install(monkeypatch)
    _install(client, user)
    llm_calls = _patch_llm(monkeypatch)

    _chat(client, user)  # no skills param at all
    assert len(llm_calls) == 1
    assert SKILL_MARKER not in llm_calls[0]["system_prompt"]


def test_chat_auto_apply_skill_injected_without_request(client, monkeypatch):
    user = _register(client, "alice")
    _patch_install(monkeypatch)
    _install(client, user)
    client.post("/api/v1/skills/haiku-mode/toggle",
                json={"enabled": True, "auto_apply": True},
                headers=_auth(user))
    llm_calls = _patch_llm(monkeypatch)

    frames = _chat(client, user)  # NO explicit invocation
    applied = next(f for f in frames if f["type"] == "skills_applied")
    assert applied["skills"] == ["haiku-mode"]
    assert len(llm_calls) == 1
    assert SKILL_MARKER in llm_calls[0]["system_prompt"]


def test_chat_unknown_requested_skill_silently_dropped(client, monkeypatch):
    user = _register(client, "alice")
    llm_calls = _patch_llm(monkeypatch)
    frames = _chat(client, user, skills=["does-not-exist"])
    assert all(f["type"] != "skills_applied" for f in frames)
    assert "turn_complete" in [f["type"] for f in frames]
    assert len(llm_calls) == 1
    assert "## Skill:" not in llm_calls[0]["system_prompt"]


# ─────────────────────────────────────────────────────────────────────
# Per-user isolation
# ─────────────────────────────────────────────────────────────────────


def test_user_isolation(client, monkeypatch):
    a = _register(client, "alice")
    b = _register(client, "bob")
    _patch_install(monkeypatch)
    _install(client, a)

    # B's list is empty — skills dirs are keyed by user_id.
    assert client.get("/api/v1/skills",
                      headers=_auth(b)).json()["skills"] == []
    # On-disk separation is real, not just an overlay.
    assert (_skills_dir(a) / "haiku-mode").exists()
    assert not (_skills_dir(b) / "haiku-mode").exists()

    # B can't toggle or uninstall A's skill.
    r = client.post("/api/v1/skills/haiku-mode/toggle",
                    json={"enabled": False}, headers=_auth(b))
    assert r.status_code == 404
    r = client.delete("/api/v1/skills/haiku-mode", headers=_auth(b))
    assert r.status_code == 404
    assert (_skills_dir(a) / "haiku-mode").exists()

    # B's chat never sees A's skill even when requested by name.
    llm_calls = _patch_llm(monkeypatch)
    _chat(client, b, skills=["haiku-mode"])
    assert len(llm_calls) == 1
    assert SKILL_MARKER not in llm_calls[0]["system_prompt"]
