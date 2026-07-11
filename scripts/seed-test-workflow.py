#!/usr/bin/env python3
"""Seed a minimal "echo pipeline" workflow into the running Nexus server.

Usage
-----
  python3 scripts/seed-test-workflow.py

What it does
------------
  1. Locates the running server's SQLite DB and skills dir under the
     macOS app-data root (~/Library/Application Support/RuneProtocol).
  2. Drops 3 minimal Claude-Code-style agent files into the skills dir
     (in `.claude/agents/` flat layout): echo-strategist, echo-writer,
     echo-publisher. Each just rephrases its input — fast + cheap to
     run, no external tools needed.
  3. Finds the most-recently-logged-in user in the users table.
  4. Inserts a workflow row referencing those three skills with one
     "topic" input field.

After running, refresh the Workflows view in the desktop app — the
"Echo Pipeline" workflow will be at the top of the list. Click it,
type anything in the topic field, hit Run.

Why this script exists
----------------------
Phase 2 (starter pack installer + skill discovery UI) isn't built
yet — the right pane in Workflows is showing the 4 starter pack
*placeholder* tiles, no install button. This script is a stopgap so
you can validate the end-to-end runtime today.

Not part of the production build path. Don't ship.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ── Locate the server's data root ────────────────────────────────────


def find_rune_home() -> Path:
    """The desktop bundle runs the server with cd $RUNE_HOME. On macOS
    that's ~/Library/Application Support/RuneProtocol; on Linux it's
    ~/.config/RuneProtocol (XDG). Override with RUNE_HOME env if you
    point your server somewhere unusual."""
    env = os.environ.get("RUNE_HOME")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "RuneProtocol"
    return Path.home() / ".config" / "RuneProtocol"


RUNE_HOME = find_rune_home()
# The bundled .env at server/.env still ships DATABASE_URL pointing at
# `rune_server.db` (the pre-rebrand name). New installs that wire
# DATABASE_URL themselves use `nexus_server.db`. Probe for both;
# whichever exists is the live DB.
_CANDIDATE_DBS = (
    RUNE_HOME / "rune_server.db",
    RUNE_HOME / "nexus_server.db",
)
DB_PATH = next((p for p in _CANDIDATE_DBS if p.exists()), _CANDIDATE_DBS[0])
SKILLS_DIR = RUNE_HOME / ".nexus" / "skills"


# ── Skill bodies ─────────────────────────────────────────────────────


SKILLS = {
    # ── Content Studio: the 5-agent content pipeline from @sairahul1 ─
    # Each agent stays in lane. The prompts are deliberately
    # opinionated — that's the whole point: a specialist isn't a
    # generalist with a hat, it's an LLM session with a tight remit.
    "content-strategist.md": """---
name: content-strategist
description: Returns angle, hook and brief. Doesn't write the article.
---

You are a content strategist. Your job is the BRIEF. Not the article.

Read the WORKFLOW INPUTS above (topic, audience, platform). Then:
1. Identify the saturated angle — what everyone is already saying
   about this topic.
2. Find the contrarian angle — what the data actually shows, or what
   people overlook. THIS is what you'll output.
3. Write the HOOK — the first line of the eventual piece. Must be a
   statement, not a question. Punchy.
4. List 3 SUBPOINTS — concrete claims the writer can build the
   article on.
5. List 2 things to AVOID — common framings that have become cliche
   on this topic.

Output format (no other prose):

ANGLE: <one sentence>
HOOK: <the literal first line of the piece>
SUBPOINTS:
- <point 1>
- <point 2>
- <point 3>
AVOID:
- <framing 1>
- <framing 2>

End with: "Avoid this: [the angle everyone else is taking]."
""",

    "content-researcher.md": """---
name: content-researcher
description: Returns 5 sources + 3 stats + 3 contrarian data points.
---

You are a research analyst. You don't write articles. You find the
facts that make the argument real.

You'll see a HANDOFF block above with the strategist's brief. Read
it, then:
1. Identify 5 primary sources that would support or test the angle.
   No SEO roundups. Prefer research papers, official reports,
   direct interviews, primary journalism.
2. Pull 3 statistics that directly support the brief's angle. Be
   specific: the actual number, year, source.
3. Find 3 CONTRARIAN data points — facts that complicate the angle.
   This is the most important part of your output. Skip it and the
   article will read like every other piece.

Output format:

SOURCES:
1. <source — 1-line summary>
2. ...

KEY FACTS:
- <fact with citation>
- ...

CONTRARIAN DATA:
- <fact that complicates the angle, with citation>
- ...

ONE QUOTE WORTH USING: "<short quote>"

End with: "Confidence: High / Medium / Low — [one sentence reason]."
""",

    "content-writer.md": """---
name: content-writer
description: Writes the full draft using the brief + research. Doesn't edit.
---

You are a writer. Execute the brief. Don't question it.

You'll see TWO things above:
1. The strategist's brief (angle, hook, subpoints).
2. The researcher's pack (sources, facts, contrarian data).

Write the full draft using THIS structure:

  Hook (literal first line — use the strategist's HOOK verbatim)
  Setup (2-3 sentences framing the problem)
  Argument: 3 subpoints, each its own paragraph. Cite inline.
  Contrarian section: the data that complicates the picture.
  Conclusion + CTA.

Constraints:
- The hook is line one. Don't move it.
- Use the contrarian data points — they're the moat against
  generic AI slop.
- No throat-clearing. No "in today's world". No "the rise of".
- 800-1200 words.
- Write at the audience's reading level (see WORKFLOW INPUTS).

End with: "[DRAFT COMPLETE — word count: X — pass to Editor]"
""",

    "content-editor.md": """---
name: content-editor
description: Cuts 30%. Sharpens hook + close. Returns edited draft + change log.
---

You are a senior editor. You make the writing earn its length.

You'll see the writer's draft above. Read it once without editing.
Then:

1. Cut every sentence that doesn't move the reader forward or
   prove the argument. Target: 30% shorter than the input.
2. Sharpen the opening — the first 3 sentences should feel
   inevitable, not warm-up.
3. Rewrite the closing — the last line must be the line a reader
   would screenshot.

Banned words (delete or rewrite around): leverage, robust,
seamless, delve, unleash, groundbreaking, game-changer,
in today's world, the rise of, navigating the landscape.

Banned punctuation: em dashes (use periods or commas).

Output format:
  EDITED DRAFT
  ────────────
  <the edited text>

  CHANGE LOG (5 lines max):
  - <what you cut and why>
  - ...

End with: 'The strongest line in this draft is: "<quote it>"'
""",

    "content-publisher.md": """---
name: content-publisher
description: Formats the edited draft for the target platform. Doesn't rewrite.
---

You are a production editor. You format for distribution. You do
NOT rewrite content.

The platform is in WORKFLOW INPUTS up the stack (look for it).
The platforms we support:

* **twitter** / **twitter/x thread**: Tweet 1 is the hook. Tweet 2
  is the setup. Tweets 3-8 are the argument. Tweet 9 is the CTA.
  Every tweet <= 280 chars. Number every tweet `1/`, `2/`, etc.
  9 tweets MAX.

* **linkedin**: First 3 lines must be the hook + setup (LinkedIn
  cuts the preview at ~210 chars). One CTA on the last line.

* **blog**: Title (H1), 155-char meta description, 3-5 H2
  subheads, conclusion with CTA.

* **newsletter**: Subject line, preview text, H2 every 300 words,
  one CTA at the end.

Output ONLY the formatted content. No preamble, no commentary on
what you did. The content goes straight to the publish step.

End with: "[PUBLISH READY — Platform: X — Word count: Y]"
""",
}


# ── Workflow definition ──────────────────────────────────────────────


WORKFLOW = {
    "name": "Content Studio",
    "description": (
        "5-agent content pipeline. Strategist → Researcher → Writer "
        "→ Editor → Publisher. Pick a topic, an audience, and a "
        "platform; get publish-ready content in one pass. Each "
        "agent runs in its own context so the editor doesn't defer "
        "to the writer and the strategist doesn't try to write the "
        "article."
    ),
    "definition": {
        "inputs": [
            {
                "key": "topic",
                "label": "Topic",
                "type": "text",
                "required": True,
                "options": [],
            },
            {
                "key": "audience",
                "label": "Audience",
                "type": "text",
                "required": True,
                "options": [],
            },
            {
                "key": "platform",
                "label": "Platform",
                "type": "select",
                "required": True,
                "options": [
                    "Twitter/X thread",
                    "LinkedIn",
                    "Blog post",
                    "Newsletter",
                ],
            },
        ],
        "steps": [
            {"skill": "content-strategist", "model": None, "label": "Strategist"},
            {"skill": "content-researcher", "model": None, "label": "Researcher"},
            {"skill": "content-writer",     "model": None, "label": "Writer"},
            {"skill": "content-editor",     "model": None, "label": "Editor"},
            {"skill": "content-publisher",  "model": None, "label": "Publisher"},
        ],
        "metadata": {"source": "seed-test-workflow.py"},
    },
}


# ── Execution ────────────────────────────────────────────────────────


def main() -> int:
    print(f"RUNE_HOME = {RUNE_HOME}")
    if not RUNE_HOME.exists():
        sys.stderr.write(
            f"\nERROR: {RUNE_HOME} doesn't exist.\n"
            "Either the app hasn't been launched yet, or RUNE_HOME is "
            "pointing somewhere unexpected. Launch Nexus once to "
            "create the data dir, then re-run this script.\n"
        )
        return 1

    if not DB_PATH.exists():
        existing = [p for p in _CANDIDATE_DBS if p.exists()]
        sys.stderr.write(
            f"\nERROR: server DB not found.\n"
            f"  Looked for:\n"
        )
        for p in _CANDIDATE_DBS:
            sys.stderr.write(f"    - {p}\n")
        sys.stderr.write(
            "  Launch Nexus + log in once so the server creates the "
            "database, then re-run.\n"
        )
        return 1
    print(f"DB_PATH   = {DB_PATH}")

    # 1. Write skill files.
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, body in SKILLS.items():
        path = SKILLS_DIR / filename
        path.write_text(body, encoding="utf-8")
        print(f"  wrote skill → {path}")

    # 2. Find a user to attach the workflow to. In single-user mode
    #    we expect exactly one row in `users`; if there are more,
    #    pick the most-recently-updated (active user).
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        # Pre-flight: ensure the workflow tables exist. The running
        # server's database.init_db() creates them on startup, but if
        # you're running this script against a DB built by an OLDER
        # server build (pre-Phase-1a), the tables won't be there yet.
        # We mirror the schema from packages/server/nexus_server/
        # database.py here, all CREATE TABLE IF NOT EXISTS so it's
        # safe to re-run after a rebuild.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS nexus_workflows (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                definition TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_nexus_workflows_user
              ON nexus_workflows(user_id, archived, updated_at DESC);

            CREATE TABLE IF NOT EXISTS nexus_workflow_runs (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL,
                inputs TEXT NOT NULL DEFAULT '{}',
                error_message TEXT NOT NULL DEFAULT '',
                current_step INTEGER NOT NULL DEFAULT 0,
                total_steps INTEGER NOT NULL DEFAULT 0,
                total_cost_usd REAL NOT NULL DEFAULT 0.0,
                started_at TIMESTAMP NOT NULL,
                finished_at TIMESTAMP,
                anchor_tx TEXT,
                FOREIGN KEY (workflow_id) REFERENCES nexus_workflows(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE INDEX IF NOT EXISTS idx_nexus_workflow_runs_user
              ON nexus_workflow_runs(user_id, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_nexus_workflow_runs_workflow
              ON nexus_workflow_runs(workflow_id, started_at DESC);

            CREATE TABLE IF NOT EXISTS nexus_workflow_run_steps (
                run_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                skill_name TEXT NOT NULL,
                status TEXT NOT NULL,
                input TEXT NOT NULL DEFAULT '',
                output TEXT NOT NULL DEFAULT '',
                model_used TEXT NOT NULL DEFAULT '',
                cost_usd REAL NOT NULL DEFAULT 0.0,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                error_message TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (run_id, step_index),
                FOREIGN KEY (run_id) REFERENCES nexus_workflow_runs(id)
            );
            """
        )
        conn.commit()
        print("  ensured workflow tables exist")
        rows = conn.execute(
            "SELECT id, display_name FROM users ORDER BY updated_at DESC LIMIT 1",
        ).fetchall()
        if not rows:
            sys.stderr.write(
                "\nERROR: no users in the database. Log into Nexus "
                "with your username + password once, then re-run.\n"
            )
            return 1
        user_id = rows[0]["id"]
        display = rows[0]["display_name"]
        print(f"\nseeding workflow for user: {display} ({user_id})")

        # 3. Insert (or replace) the workflow row.
        wf_id = "wf_" + uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()

        # Clean up any prior "Echo Pipeline (test)" so re-runs of the
        # script don't pile up duplicates.
        existing = conn.execute(
            "SELECT id FROM nexus_workflows WHERE user_id = ? AND name = ?",
            (user_id, WORKFLOW["name"]),
        ).fetchall()
        for row in existing:
            old_id = row["id"]
            # Cascade delete: drop the run-steps + runs first, FK-style.
            for run_row in conn.execute(
                "SELECT id FROM nexus_workflow_runs WHERE workflow_id = ?",
                (old_id,),
            ).fetchall():
                conn.execute(
                    "DELETE FROM nexus_workflow_run_steps WHERE run_id = ?",
                    (run_row["id"],),
                )
            conn.execute(
                "DELETE FROM nexus_workflow_runs WHERE workflow_id = ?",
                (old_id,),
            )
            conn.execute("DELETE FROM nexus_workflows WHERE id = ?", (old_id,))
            print(f"  removed prior copy {old_id}")

        conn.execute(
            """
            INSERT INTO nexus_workflows
                (id, user_id, name, description, definition,
                 created_at, updated_at, archived)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                wf_id, user_id,
                WORKFLOW["name"], WORKFLOW["description"],
                json.dumps(WORKFLOW["definition"]),
                now, now,
            ),
        )
        conn.commit()
        print(f"  inserted workflow {wf_id}")
    finally:
        conn.close()

    print(
        "\nDone. Open the desktop → user menu (▾) → Workflows. "
        "If the view's already open, click the ↻ icon in the source "
        "list header to reload. You'll see 'Echo Pipeline (test)' "
        "at the top — pick it, type any topic, hit Run."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
