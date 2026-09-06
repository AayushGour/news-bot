# AI Dev Org Harness

A lean software team as tight agent prompts — **10 roles, 2 modes** — one full runtime (Claude Code) plus a local-model design sketch. Plan the work, then build it. Short prompts, direct action, few handoffs.

Plan mode gathers requirements and designs; agile dev mode builds; `reviewer` gives an independent code + integration review and `tester` validates against acceptance criteria — either can reject work back to the owner. No feature ships unreviewed or untested.

| Folder | Runtime | Use |
|---|---|---|
| `claude-code/` | Claude Code subagents (`.claude/agents/*.md`, Task tool) | run with Claude Code |
| `local/`       | llama.cpp + Hermes **design sketch** — the dispatcher seams are unimplemented (`run.py` raises until wired) and no hooks run there, so the board integrity rules are not enforced | sketch for running a small local model |
| `tools/`       | custom tools (MCP servers / HTTP APIs) shared by both runtimes | extend agent capabilities |

Both share: files-as-memory (no external DB), grep-as-code-graph, one-line logs, skippable ceremony for small tasks.

## Setup

One script installs the team into any project — cross-platform (Windows/Linux/macOS), only needs Python 3:

```bash
python3 setup-team.py <path-to-your-project>
```

`claude-code/.claude/` already mirrors the exact layout an installed project uses, so setup is a straight copy:

- `.claude/agents/*.md` — the 10 agent prompts
- `CLAUDE.md` → project root — the **one shared rulebook**: orchestrator brief + org rules (DONE gate, integrity rules, team formation, delegation, cost + consent rules). The main thread auto-loads it; every spawned agent reads it first (junior-dev carries a self-contained mini-rulebook instead). Framework file: `--force` upgrades it — but only when the existing root `CLAUDE.md` is the harness's own (checked by a marker line); a user's personal `CLAUDE.md` is never touched
- `.claude/agent-template.md` — skeleton the architect copies into `.claude/agents/` when authoring a project specialist
- `.claude/coding-standards.md` · `.claude/project-context.md` · `.claude/task-board.md` · `.claude/design.md` — working docs (start as templates)
- `.claude/skills/ui-ux-pro-max/` — UI/UX design-intelligence skill the `ux-designer` queries (searchable local DB of styles/palettes/fonts/UX rules; pure stdlib, no pip install). Vendored from [nextlevelbuilder/ui-ux-pro-max-skill](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill) (MIT).
- `.claude/skills/` — on-demand expertise (progressive disclosure, ~100 tokens each until triggered): `security-review` + `differential-review` (Trail of Bits, CC-BY-SA) for reviews, `data-modeling` for schema changes, `tdd` + `diagnosing-bugs` (Matt Pocock, MIT) for devs, `webapp-testing` (Anthropic, Apache-2.0) + `property-based-testing` (Trail of Bits) for tester, `prd` (GitHub, MIT) for the BA. Each vendored skill carries a `SOURCE.md` (origin, commit, license) and was security-audited before vendoring
- a **board-lint hook** (`tools/board_lint.py`, PreToolUse on Edit/Write/MultiEdit **and Bash**) — blocks `status:done` lines whose `evidence:` ref is missing or doesn't resolve (file must exist; `#T<id>` anchors must appear in it), and blocks Bash writes to the board outright so every change flows through the linted Edit/Write path. A **debounced PostToolUse hook** (`tools/code_review_graph/refresh_graph.py`) keeps the code-brain graph fresh after source edits — no one has to remember to rebuild it
- this `README.md` → `.claude/README.md` (kept inside `.claude/`, never overwrites your project's own root `README.md`)
- `.mcp.json` at the project root — the **only** file placed there, because Claude Code discovers project MCP servers only from `<project>/.mcp.json`, not from `.claude/` (tool paths rewritten to absolute)

Safe to re-run:
- **Default** — skips every file that already exists (no data touched).
- **`--force`** — updates the *framework* files (agents, instructions, skills, `.claude/README.md`, `.mcp.json`) to the latest version, but **never** overwrites your per-project working docs (`project-context.md`, `coding-standards.md`, `task-board.md`, `design.md`) — those are seeded once and stay yours. Use `--force` to upgrade the team without losing project state.

### Optional: a `setup-team` command (run it from anywhere)
So you can type `setup-team <path-to-project>` in any folder instead of `cd`-ing to the harness first. The script resolves the target relative to your current directory, so the alias works from anywhere. Replace `/ABSOLUTE/PATH/TO/harness` with this repo's path (`pwd` here on macOS/Linux, `(Get-Location).Path` in PowerShell).

**macOS / Linux — zsh** (default on modern macOS), append to `~/.zshrc`:
```bash
echo 'alias setup-team="python3 /ABSOLUTE/PATH/TO/harness/setup-team.py"' >> ~/.zshrc
source ~/.zshrc
```
**Linux — bash**, same but `~/.bashrc`:
```bash
echo 'alias setup-team="python3 /ABSOLUTE/PATH/TO/harness/setup-team.py"' >> ~/.bashrc
source ~/.bashrc
```
**Windows — PowerShell**, add a function to your profile (aliases can't carry a fixed argument, so use a function that forwards `@args`):
```powershell
Add-Content $PROFILE 'function setup-team { python "C:\ABSOLUTE\PATH\TO\harness\setup-team.py" @args }'
. $PROFILE
```
Then from any project folder:
```bash
setup-team <path-to-your-project>
```

**Then start a new Claude Code session inside that project folder** — the freshly-installed `.claude/agents/` is discovered when the session starts. (Once a session is running, new or edited agent files *under the existing* `.claude/agents/` hot-load within seconds — that's what lets the architect add a project specialist mid-project; see [Team formation](#team-formation).) Describe the work; the main thread routes to the right agent, or call one via the `Task` tool.

## Roster (10 core + project specialists)
| Agent | Role |
|---|---|
| business-analyst | requirements, clarify, research/fact-check |
| project-manager  | intake/triage, track, status, coordinate, project record |
| architect        | design, standards, split into tasks, delegate, technical record |
| product-engineer | feasibility, prioritize by impact, spikes to de-risk, shape work |
| ux-designer      | flows, wireframes, design system, accessibility (Nielsen + WCAG) |
| senior-dev       | hard tasks, quality check, review junior, debug, severity read |
| junior-dev       | smaller build/edit tasks, debug |
| devops           | CI/CD, deploy, networking, cloud |
| reviewer         | independent code + integration review (Google eng-practices), can reject |
| tester           | unit/integration/API/blackbox, automated scripts, can reject |

The **architect is team lead**. These 10 are the standing team; for a project with a real domain gap the architect can author a **specialist** agent (see [Team formation](#team-formation)).

## Two modes
- **Plan mode** — `business-analyst` gathers + clarifies requirements → `architect` designs, sets standards, runs the [team self-review](#team-formation), then splits into tasks, pulling in `ux-designer` (UI) and `product-engineer` (feasibility/spikes). No code.
- **Agile dev mode** — `architect` delegates → `senior-dev` / `junior-dev` / `devops` (+ any specialist) build → `reviewer` reviews code + integration → `tester` validates → done. `project-manager` tracks + documents throughout.

Start in plan mode; switch to dev mode once the plan + tasks exist. Small/obvious change → skip plan mode, build at size S (a board line + pasted evidence still required).

## Team formation
Once the plan is clear and before task-split, the **architect** (team lead) reviews the 10 core roles against what the project actually needs, with `project-manager` (coordination) and `product-engineer` (feasibility) consulting. Default is to reuse the 10; only a genuine *ongoing* domain gap (ML, mobile/iOS, data engineering, security, a niche framework — not a one-off task) justifies a new agent. If so, the architect copies `.claude/agent-template.md` → `.claude/agents/<name>.md`, fills it house-style (reads `CLAUDE.md`, satisfies the DONE gate, own log file), and records why in `.claude/project-context.md` (`## Team`). Claude Code hot-loads the new agent within seconds — delegatable the same session — and PM adds it to the roster. Keep the team as small as the work allows.

## Incoming requests — intake + triage
Every new bug/change goes to **project-manager** first (the front door).
1. PM logs it and sets **priority** (P0 critical → P3 low — urgency/when to fix).
2. PM routes by type (asks senior-dev for the **severity/complexity** read only on borderline cases):
   - new / unclear requirement → **business-analyst**
   - clear small fix → **senior-dev** → does it, or delegates to **junior-dev**
   - complex / architectural / cross-cutting → **architect** → pulls **ux-designer** + **product-engineer** to plan → task split
3. Then the normal build flow: build → **reviewer** → **tester** → done.

Priority = business urgency (PM owns). Severity = technical impact/complexity (senior-dev owns). Different axes — don't conflate them.

## Source of truth = files (not chat)
```
.claude/project-context.md    what we're building, why, constraints, design, decisions   (BA seeds; architect + PM keep current)
.claude/coding-standards.md   stack, conventions, how to run tests                        (architect)
.claude/task-board.md         tasks + owner + priority + status                           (architect creates; PM keeps honest; each updates own)
.claude/design.md             flows, states, components, accessibility AC                 (ux-designer; UI projects only)
.claude/logs/<agent>.md       one log file per agent, that agent appends only             (each agent, own file only)
```
"Analyze the code" = Grep / Glob / Read. Reuse before you write — no duplicates.

**Logging — one file per agent (no shared file, no lock):** each agent writes **only** its own `.claude/logs/<agent>.md`. Because no two agents ever write the same file, parallel agents never collide. One line per task at handoff/done: `- <date> [T<id>] one-line summary`. To see who-did-what, read/concat `.claude/logs/*.md` (PM does this for status reports).

**Who documents:** architect owns the *technical* record; project-manager owns the *project* record. User-facing docs split three ways: **architect** → overview + setup; **senior-dev** → API/usage reference for what they built; **tester** → verified how-to. One voice, no overlap.

## Design principles (why the prompts are short)
- Few agents, sharp prompts, direct action — coordination tax is what kills agent orgs.
- Shared rules live in one root `CLAUDE.md`, not repeated per agent.
- Memory = plain files; "analyze the codebase" = grep/glob/read.
- Log one line per task, not 15 fields per action.
- senior-dev + reviewer review; tester can reject. Neither ceremony runs on a typo.
- Prod-grade baseline is non-negotiable: DRY, no magic strings (constants module), env config read from one place, lint clean before handoff. See `coding-standards.md`'s Non-negotiables.

## Custom tools
`tools/` holds capabilities beyond files+shell (web search, APIs, MCP). One core impl per tool, wired to Claude Code (MCP in `claude-code/.mcp.json`) and local (`tools/registry.py`). Included: `web_search/` → SearXNG (local, `SEARXNG_URL`), `deepwiki/` → public-repo docs+Q&A (remote MCP). Needs `pip install mcp`. See `tools/README.md`.

## Quick start
- Install the team into a project: `python3 setup-team.py <path>` (see [Setup](#setup))
- Local (llama.cpp/Hermes) details: see `local/README.md`
- Add a tool: see `tools/README.md`

## Superseded
`CLAUDE_MASTER_PROMPT.md` is the original generator spec (kept for reference). The earlier generated output lives in `old/` — superseded by `claude-code/` and `local/`. It relied on nonexistent tools (GBrain, Code Review Graph, `claude memory add`), had two drifted rosters, and ~170-line boilerplate prompts. Safe to delete once you've confirmed the new folders.
