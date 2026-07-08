# Email Send — Setup Guide & Verification

Status: **Reference** (2026-06-14)
Audience: medic (end-user) + on-call dev

The v2 `POST /api/v1/email/send` capability is already wired
end-to-end and v1's relay credentials flow into v2 automatically on
first launch. The medic only needs to **reinstall the latest .dmg**.

## Why no manual config is needed

| Piece                       | Status | Location                                                                  |
| --------------------------- | ------ | ------------------------------------------------------------------------- |
| v1 relay credentials        | ✓      | `packages/server/.env:57-58` — `NEXUS_RELAY_URL` + `NEXUS_RELAY_API_KEY`  |
| Credentials bundled into v2 | ✓      | `packages/desktop-v2/src-tauri/resources/default.env` (auto-synced)       |
| build-script auto-sync      | ✓      | `scripts/build-macos.sh:744-781` regenerates default.env each build       |
| Tauri resource declaration  | ✓      | `tauri.conf.json:51`                                                      |
| First-launch seed           | ✓      | `src-tauri/src/lib.rs:343 seed_or_merge_user_env()` runs at sidecar spawn |
| Hot-reload at send time     | ✓      | `email_send._live_relay()` re-reads env per call                          |

The first-launch seed walks bundle → `$RUNE_HOME/.env`. If the file
already exists from v1 it does a delta-merge (preserves user
overrides). Existing v1 installs likely already have the keys
since v1's `start.sh` ran the same merge — the seed step is a no-op.

## Action — what you do

```bash
# 1. Rebuild .dmg from repo root
cd packages/desktop-v2
pnpm tauri:build

# 2. Install the new .dmg
#    (auto-install pipeline handles the rest)

# 3. Open Nexus.app, sign in
# 4. Account menu (avatar, top-right) → "撰写邮件…"
#    Banner should read:
#        发送方式：通过 relay (nexus-email-relay.fly.dev)
#    Send button should be ENABLED.
```

## Verification

```bash
# (a) $RUNE_HOME/.env should have both keys
grep -E '^NEXUS_RELAY_(URL|API_KEY)=' \
  ~/Library/Application\ Support/RuneProtocol/.env
# expected output:
#   NEXUS_RELAY_URL=https://nexus-email-relay.fly.dev
#   NEXUS_RELAY_API_KEY=ea87...   (real value)

# (b) Sidecar log shows the seed
tail -50 ~/Library/Logs/Nexus/sidecar.log | grep -i 'seed\|merg'
# expected: "seeded default.env to ~/Library/.../.env"
#       or: "merged N new key(s) from default.env"

# (c) Test send
# Account → 撰写邮件 → fill in your own email → Send
# Status strip should turn green:
#   Sent via relay. To: <you>. Quota remaining today: 9.
```

## Trigger surfaces (today)

There is **no LLM tool-calling** in v2 chat yet, so the medic can't
say "Nexus please email Dr Smith" mid-conversation and have the
email fly out. Available paths today:

1. **Account menu → 撰写邮件…** — manual compose dialog. Always
   works.
2. **Patient mode → "把发现发邮件给同事"** — only enabled when the
   patient has active findings. Pre-fills To/Subject/Body with the
   findings list (pseudonymous identifiers only — no MRN/DOB).
3. **Command palette ⌘K → "撰写邮件"** — opens the same dialog.

LLM-triggered email becomes available in Phase 1 of the Scheduled
Tasks design (see `scheduled-tasks-and-calendar.md`). At that point
"现在帮我给 Dr Smith 发邮件，主题是 X，内容是 Y" produces a
fire_at=now scheduled task; user confirms → it sends.

## If sending fails (troubleshooting)

1. **Banner says "no transport configured"** — `$RUNE_HOME/.env`
   missing relay keys. Either the seed didn't run (sidecar.log will
   show why) or the bundle has stale keys. Rebuild and reinstall.
2. **Status strip says "rate limit hit"** — relay enforces 10
   sends/medic/day. Resets at UTC 00:00.
3. **Status strip says "Cannot reach relay"** — Fly.io relay is
   down (rare) or local network blocks the host. The relay status
   page is at `https://fly.io/apps/nexus-email-relay`.
4. **Status strip says "SMTP authentication failed"** — only fires
   when the relay isn't configured and the medic somehow added bad
   SMTP keys. Clear `NEXUS_SMTP_*` from `$RUNE_HOME/.env` and rely
   on relay.

## Forward look

When the LLM-tool-calling path lands (post Scheduled Tasks Phase 2):

```
Doctor: "现在帮我把 J.D. 的 CT 发现发邮件给 dr.smith@hosp.org"
Nexus:  [proposes fire_at=now scheduled send_email task]
        → inline confirmation card with full draft preview
Doctor: [Confirm]
Nexus:  → worker fires immediately → email goes out
        → toast: 邮件已发送 · dr.smith@hosp.org
```

The same safety gates apply — every send_email task, including
fire_at=now ones, requires an explicit Confirm click.
