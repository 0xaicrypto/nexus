# Research Workspace — Visual Mock

This directory holds the **authoritative high-fidelity visual mockup** of
the Research Workspace UI. It supersedes any ad-hoc sketches in the
main design doc (`docs/design/RESEARCH_WORKSPACE_DESIGN.md`) for
visual decisions: typography, palette, spacing, component composition.

## How this maps to code

(Verified against `packages/desktop-v2/src/` as of the most recent
docs audit; line numbers may drift but the symbols are stable.)

| Mock element | Production component | Status |
|---|---|---|
| Top `[ 患者 ｜ 研究 ]` toggle | `src/App.tsx` → `WorkspaceSwitcher` | ✓ |
| `RESEARCH` sidebar | `src/components/research-workspace.tsx` → `StudiesSidebar` | ✓ |
| Study Detail 7 tabs | `StudyDetail` + tab components in the same file | ✓ |
| KPI 4 cards | `KPICard` | ✓ |
| Activity feed | `ActivityFeed` | ✓ |
| Eligibility candidate cards | `CandidateCard` | ✓ |
| Invite modal | `InviteModal` | ✓ |
| Visit checklist modal | `VisitChecklistModal` | ✗ not yet implemented |
| Safety stream | `SafetyTab` | ✓ |
| Schedule gantt | `ScheduleTab` | ✓ |
| Research Chat with focus chip | `ChatTab` | ✓ |
| Reports tab | `ReportsTab` | ✓ |

The default landing for fresh installs is the Research Workspace
(`store.ts` `activeWorkspace` defaults to `'research'`). Returning
users see whichever workspace they were on last — the choice is
sticky via `localStorage`. Both behaviours are intentional per
design §0 (research-first by default) + §3.1 (segmented toggle on
top).

## Design tokens — extracted into

- `packages/desktop-v2/src/styles/design-tokens.css`
- `packages/desktop-v2/tailwind.config.ts` (the `theme.extend` block)

When updating the mock, also update the tokens file. Don't let them
drift.
