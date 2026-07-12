# SPDX-License-Identifier: Apache-2.0
"""GitHub skill-install layout tests — no network.

Covers the repo-root resolution rework in nexus_core.skills.manager:

  * default-branch resolution via the repos API (non-'main' defaults —
    the old code hardcoded 'main' and 404'd on 'master' repos);
  * single-skill repo with SKILL.md at the repo ROOT;
  * community "skill pack" repos: MULTIPLE top-level dirs each carrying
    their own SKILL.md (the layout that used to fail with "multi-skill
    repo but I can't list its skills/ folder (HTTP Error 404)");
  * the classic ``skills/<name>/SKILL.md`` convention still works;
  * install() with multiple candidates raises an error listing choices;
  * non-skill dirs (docs / tests / assets / dotdirs) are skipped;
  * install_pack() keeps going past individual failures, raising only
    when nothing installed;
  * top-level skill-dir files (scripts/assets) are downloaded, with the
    2 MB size cap enforced.

All GitHub traffic is faked by monkeypatching the manager's HTTP layer
(_http_get_text / _http_get_json / _download_file).
"""
from __future__ import annotations

import re
import urllib.error

import pytest

from nexus_core.skills.manager import SkillManager


def _skill_md(name: str, desc: str = "test skill") -> str:
    return (
        f"---\nname: {name}\ndescription: {desc}\n---\n\n"
        f"Instructions for {name}.\n"
    )


class FakeRepo:
    """In-memory model of one GitHub repo on its default branch.

    ``files`` maps repo-relative paths to text content. Raw fetches and
    contents-API listings are synthesised from it; any request against
    a different org/repo/branch raises HTTP 404 — which is exactly how
    a hardcoded-'main' bug would surface against a 'master' repo.
    """

    def __init__(self, org: str, repo: str, default_branch: str,
                 files: dict[str, str],
                 size_overrides: dict[str, int] | None = None):
        self.org, self.repo, self.branch = org, repo, default_branch
        self.files = dict(files)
        self.size_overrides = dict(size_overrides or {})
        self.api_calls: list[str] = []
        self.downloads: list[str] = []

    # -- helpers ------------------------------------------------------

    def _404(self, url: str):
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    def raw(self, url: str) -> str:
        prefix = (
            f"https://raw.githubusercontent.com/"
            f"{self.org}/{self.repo}/{self.branch}/"
        )
        if not url.startswith(prefix):
            self._404(url)
        rel = url[len(prefix):]
        if rel not in self.files:
            self._404(url)
        return self.files[rel]

    def api(self, url: str):
        self.api_calls.append(url)
        if url == f"https://api.github.com/repos/{self.org}/{self.repo}":
            return {"default_branch": self.branch}
        m = re.match(
            rf"https://api\.github\.com/repos/{re.escape(self.org)}/"
            rf"{re.escape(self.repo)}/contents(?:/([^?]*))?\?ref=(.+)$",
            url,
        )
        if not m or m.group(2) != self.branch:
            self._404(url)
        path = (m.group(1) or "").strip("/")
        entries: dict[str, dict] = {}
        for rel, content in self.files.items():
            if path:
                if not rel.startswith(path + "/"):
                    continue
                rest = rel[len(path) + 1:]
            else:
                rest = rel
            head = rest.split("/")[0]
            if "/" in rest:
                entries.setdefault(head, {"type": "dir", "name": head})
            else:
                entries[head] = {
                    "type": "file",
                    "name": head,
                    "size": self.size_overrides.get(
                        rel, len(content.encode())),
                    "download_url": (
                        f"https://raw.githubusercontent.com/"
                        f"{self.org}/{self.repo}/{self.branch}/{rel}"
                    ),
                }
        if not entries and path:
            self._404(url)
        return list(entries.values())

    # -- wiring -------------------------------------------------------

    def wire(self, monkeypatch, fail_download_substr: str | None = None):
        repo = self

        monkeypatch.setattr(
            SkillManager, "_http_get_text",
            staticmethod(lambda url: repo.raw(url)),
        )
        monkeypatch.setattr(
            SkillManager, "_http_get_json",
            lambda self, url: repo.api(url),
        )

        async def fake_download(mgr_self, url, dest, timeout=15.0):
            if fail_download_substr and fail_download_substr in url:
                raise RuntimeError(f"simulated download failure: {url}")
            repo.downloads.append(url)
            dest.write_bytes(repo.raw(url).encode())

        monkeypatch.setattr(SkillManager, "_download_file", fake_download)


# ─────────────────────────────────────────────────────────────────────
# Default-branch resolution
# ─────────────────────────────────────────────────────────────────────


async def test_default_branch_resolved_for_non_main_repo(
    tmp_path, monkeypatch,
):
    """Repo-root URL, default branch 'develop' — every fetch against
    'main' 404s, so success proves the branch was resolved, not
    hardcoded."""
    repo = FakeRepo("acme", "one-skill", "develop",
                    {"paper-review/SKILL.md": _skill_md("paper-review")})
    repo.wire(monkeypatch)
    mgr = SkillManager(base_dir=tmp_path)

    skill = await mgr.install("https://github.com/acme/one-skill")
    assert skill.name == "paper-review"
    assert (tmp_path / "skills" / "paper-review" / "SKILL.md").exists()
    assert mgr._default_branch_cache == {"acme/one-skill": "develop"}


async def test_explicit_tree_branch_skips_repos_api(tmp_path, monkeypatch):
    """A /tree/<branch>/ URL carries its branch — no repos-API lookup."""
    repo = FakeRepo("acme", "one-skill", "v2",
                    {"pkg/SKILL.md": _skill_md("pkg")})
    repo.wire(monkeypatch)
    mgr = SkillManager(base_dir=tmp_path)

    skill = await mgr.install("https://github.com/acme/one-skill/tree/v2/pkg")
    assert skill.name == "pkg"
    assert f"https://api.github.com/repos/acme/one-skill" not in repo.api_calls
    assert mgr._default_branch_cache == {}


async def test_default_branch_falls_back_to_main_on_api_failure(
    tmp_path, monkeypatch,
):
    repo = FakeRepo("acme", "flaky", "main",
                    {"SKILL.md": _skill_md("root-skill")})

    orig_api = repo.api

    def api_repos_down(url):
        if url == "https://api.github.com/repos/acme/flaky":
            raise OSError("connection refused")
        return orig_api(url)

    repo.wire(monkeypatch)
    monkeypatch.setattr(SkillManager, "_http_get_json",
                        lambda self, url: api_repos_down(url))
    mgr = SkillManager(base_dir=tmp_path)

    skill = await mgr.install("https://github.com/acme/flaky")
    assert skill.name == "root-skill"
    assert mgr._default_branch_cache == {"acme/flaky": "main"}


# ─────────────────────────────────────────────────────────────────────
# Layout discovery
# ─────────────────────────────────────────────────────────────────────


async def test_root_single_skill_repo(tmp_path, monkeypatch):
    """SKILL.md at the repo ROOT — skill name falls back to the repo."""
    repo = FakeRepo("acme", "solo", "main", {
        "SKILL.md": _skill_md("solo-skill"),
        "helper.py": "print('hi')\n",
    })
    repo.wire(monkeypatch)
    mgr = SkillManager(base_dir=tmp_path)

    skill = await mgr.install("https://github.com/acme/solo")
    assert skill.name == "solo-skill"
    dest = tmp_path / "skills" / "solo"
    assert (dest / "SKILL.md").exists()
    # Top-level files ride along (item 5 — scripts/assets).
    assert (dest / "helper.py").read_text() == "print('hi')\n"


async def test_skills_subfolder_layout_still_works(tmp_path, monkeypatch):
    """Classic skills/<name>/SKILL.md convention, single skill."""
    repo = FakeRepo("acme", "classic", "main", {
        "README.md": "readme",
        "skills/pdf/SKILL.md": _skill_md("pdf"),
        "skills/pdf/references/api.md": "# api ref\n",
    })
    repo.wire(monkeypatch)
    mgr = SkillManager(base_dir=tmp_path)

    skill = await mgr.install("https://github.com/acme/classic")
    assert skill.name == "pdf"
    dest = tmp_path / "skills" / "pdf"
    assert (dest / "SKILL.md").exists()
    assert (dest / "references" / "api.md").read_text() == "# api ref\n"
    assert skill.references == {"api.md": "# api ref\n"}


async def test_install_multiple_candidates_raises_listing_choices(
    tmp_path, monkeypatch,
):
    """install() must NOT silently pick one of several skills — it
    lists the choices and points at install_pack."""
    repo = FakeRepo("Lambenthan", "paper-discipline-skills", "master", {
        "peer-review/SKILL.md": _skill_md("peer-review"),
        "lit-search/SKILL.md": _skill_md("lit-search"),
        "citations/SKILL.md": _skill_md("citations"),
    })
    repo.wire(monkeypatch)
    mgr = SkillManager(base_dir=tmp_path)

    with pytest.raises(RuntimeError) as exc:
        await mgr.install(
            "https://github.com/Lambenthan/paper-discipline-skills")
    msg = str(exc.value)
    assert "3 skills" in msg
    assert "peer-review" in msg and "lit-search" in msg
    assert "install_pack" in msg
    assert mgr.installed == []


async def test_non_skill_dirs_skipped_in_discovery(tmp_path, monkeypatch):
    """docs/tests/assets/dotdirs never count as skills even when they
    happen to contain a SKILL.md."""
    repo = FakeRepo("acme", "pack", "main", {
        "alpha/SKILL.md": _skill_md("alpha"),
        "docs/SKILL.md": _skill_md("docs-not-a-skill"),
        "tests/SKILL.md": _skill_md("tests-not-a-skill"),
        "assets/logo.txt": "x",
        ".github/workflows/ci.yml": "x",
    })
    repo.wire(monkeypatch)
    mgr = SkillManager(base_dir=tmp_path)

    # Only 'alpha' is discovered → single-candidate root install works.
    skill = await mgr.install("https://github.com/acme/pack")
    assert skill.name == "alpha"
    assert set(mgr.names) == {"alpha"}


# ─────────────────────────────────────────────────────────────────────
# install_pack
# ─────────────────────────────────────────────────────────────────────


async def test_install_pack_installs_three_root_skill_dirs(
    tmp_path, monkeypatch,
):
    """The confirmed-bug layout: a github-topic 'skill pack' repo with
    several top-level skill dirs and a non-'main' default branch."""
    repo = FakeRepo("Lambenthan", "paper-discipline-skills", "master", {
        "peer-review/SKILL.md": _skill_md("peer-review"),
        "peer-review/rubric.md": "# rubric\n",
        "lit-search/SKILL.md": _skill_md("lit-search"),
        "citations/SKILL.md": _skill_md("citations"),
        "docs/README.md": "not a skill",
    })
    repo.wire(monkeypatch)
    mgr = SkillManager(base_dir=tmp_path)

    skills = await mgr.install_pack(
        "https://github.com/Lambenthan/paper-discipline-skills")
    assert sorted(s.name for s in skills) == [
        "citations", "lit-search", "peer-review"]
    for name in ("peer-review", "lit-search", "citations"):
        assert (tmp_path / "skills" / name / "SKILL.md").exists()
        assert name in mgr.names
    # Sibling top-level files came along too.
    assert (tmp_path / "skills" / "peer-review" / "rubric.md").exists()


async def test_install_pack_specific_path_behaves_like_install(
    tmp_path, monkeypatch,
):
    repo = FakeRepo("acme", "classic", "main", {
        "skills/pdf/SKILL.md": _skill_md("pdf"),
        "skills/docx/SKILL.md": _skill_md("docx"),
    })
    repo.wire(monkeypatch)
    mgr = SkillManager(base_dir=tmp_path)

    skills = await mgr.install_pack(
        "https://github.com/acme/classic/tree/main/skills/pdf")
    assert [s.name for s in skills] == ["pdf"]
    assert set(mgr.names) == {"pdf"}


async def test_install_pack_continues_past_individual_failure(
    tmp_path, monkeypatch,
):
    """One skill dir failing to download must not abort the pack."""
    repo = FakeRepo("acme", "pack", "main", {
        "good-one/SKILL.md": _skill_md("good-one"),
        "broken/SKILL.md": _skill_md("broken"),
        "good-two/SKILL.md": _skill_md("good-two"),
    })
    # Discovery (raw text probe) sees broken/SKILL.md, but its actual
    # download blows up — install_pack should skip it and finish.
    repo.wire(monkeypatch, fail_download_substr="/broken/")
    mgr = SkillManager(base_dir=tmp_path)

    skills = await mgr.install_pack("https://github.com/acme/pack")
    assert sorted(s.name for s in skills) == ["good-one", "good-two"]
    assert "broken" not in mgr.names


async def test_install_pack_raises_when_nothing_succeeds(
    tmp_path, monkeypatch,
):
    repo = FakeRepo("acme", "pack", "main", {
        "a/SKILL.md": _skill_md("a"),
        "b/SKILL.md": _skill_md("b"),
    })
    repo.wire(monkeypatch, fail_download_substr="SKILL.md")
    mgr = SkillManager(base_dir=tmp_path)

    with pytest.raises(RuntimeError) as exc:
        await mgr.install_pack("https://github.com/acme/pack")
    assert "failed" in str(exc.value)
    assert mgr.installed == []


async def test_large_files_skipped_on_download(tmp_path, monkeypatch):
    """Top-level files over 2 MB are not downloaded."""
    repo = FakeRepo(
        "acme", "solo", "main",
        {
            "SKILL.md": _skill_md("solo-skill"),
            "model.bin": "pretend-huge",
            "small.txt": "ok",
        },
        size_overrides={"model.bin": 5 * 1024 * 1024},
    )
    repo.wire(monkeypatch)
    mgr = SkillManager(base_dir=tmp_path)

    await mgr.install("https://github.com/acme/solo")
    dest = tmp_path / "skills" / "solo"
    assert (dest / "small.txt").exists()
    assert not (dest / "model.bin").exists()
