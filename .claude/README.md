# AI Dev Org Harness

A lean software team as tight agent prompts — **10 roles, 2 modes** — one full runtime (Claude Code) plus a local-model design sketch. Plan the work, then build it. Short prompts, direct action, few handoffs.

Plan mode gathers requirements and designs; agile dev mode builds; `reviewer` gives an independent code + integration review and `tester` validates against acceptance criteria — either can reject work back to the owner. No feature ships unreviewed or untested.

| Folder | Runtime | Use |
|---|---|---|
| `claude-code/` | Claude Code subagents (`.claude/agents/*.md`, Task tool) | run with Claude Code |
| `installer/`   | interactive multi-platform installer behind `setup-team.py` — reads `claude-code/` as the one source | install the team |
| `local/`       | llama.cpp + Hermes **design sketch** (no agent copies of its own — loads the canonical `claude-code/` agents + rulebook) — the dispatcher seams are unimplemented (`run.py` raises until wired) and no hooks run there, so the board integrity rules are not enforced | sketch for running a small local model |
| `tools/`       | custom tools (MCP servers / HTTP APIs) shared by both runtimes | extend agent capabilities |

Both share: files-as-memory (no external DB), grep-as-code-graph, one-line logs, skippable ceremony for small tasks.

## Setup

One script installs the team into any project, for one or more agent platforms — cross-platform (Windows/Linux/macOS), only needs Python 3. In a terminal it asks for the project folder, the platforms (installed ones pre-selected), and whether to overwrite framework files:

```bash
python3 setup-team.py                                                 # guided
python3 setup-team.py <path-to-your-project>                          # guided platform pick
python3 setup-team.py <path> --platforms claude,cursor --yes [--force] # scripted, no prompts
```

Without a terminal (CI, pipes) it never prompts: platforms default to whatever the project already has (else `claude`), and questions take their default answer — so the original one-argument form still works.

**Platforms** (pick any mix; installed CLIs are pre-selected):

| Platform | Rulebook | Skills | Agents | MCP | Harness hooks (DONE gate, code brain) | Verified live |
|---|---|---|---|---|---|---|
| Claude Code | `.claude/CLAUDE.md` → `@../AGENTS.md` | links in `.claude/skills` | native | `.mcp.json` | native | ✓ |
| GitHub Copilot | native | native | native (`.claude/agents`) | native (`.mcp.json`) | native (`.claude/settings.json`) | ✓ |
| Cursor | native | native | native (`.claude/agents`) | `.cursor/mcp.json` | native (Claude hooks toggle) | — needs `cursor-agent login` |
| OpenAI Codex | native | native | stubs `.codex/agents/*.toml` | `.codex/config.toml` block | `.codex/hooks.json` via `tools/hook_adapter.py` | ✓ incl. gate blocking |
| OpenCode | native | native | stubs `.opencode/agents/*.md` | `opencode.json` | plugin `.opencode/plugins/harness-hooks.js` | ✓ incl. gate blocking |
| Antigravity CLI (Gemini CLI's successor) | native | native | stubs `.agents/agents/*.md` | `.agents/mcp_config.json` | `.agents/hooks.json` (no session events) | — not installed here |
| Hermes Agent | native (20k-char cap) | by path | via `delegate_task` | snippet for `~/.hermes/config.yaml` | none (user-global only) | partial (context loads) |
| Amp | native | native | — (roles run inline) | `.amp/settings.json` | none | — needs `amp login` |
| Kiro | native | `skill://` glob | stubs `.kiro/agents/*.json` (`file://` prompt) | `.kiro/settings/mcp.json` | none | — not installed here |

Agent stubs never copy a role prompt — they point at `.claude/agents/<role>.md` (Codex and OpenCode were checked following the pointer). Generated JSON/TOML is merged key-by-key into any config you already have, and removed again on deselect. Specialists the architect authors are picked up by re-running setup.

**No duplication — in this repo or in your project.** `claude-code/` is the single definition of the team and already has the installed layout; `installer/` (behind `setup-team.py`) copies it once and adds per platform only what that platform can't read natively. Re-runs remember the selection in `.agents/harness.json`; deselecting a platform removes only the files generated for it (anything you edited is kept).

What lands in the project:

| Path | What | Read by |
|---|---|---|
| `AGENTS.md` | the **one shared rulebook** — orchestrator brief + org rules (DONE gate, integrity rules, team formation, delegation, cost + consent, platform notes), inside a `harness:begin/end` block so your own text around it survives | every platform natively |
| `.claude/CLAUDE.md` | a block containing just `@../AGENTS.md` — in `.claude/`, not the root, because Copilot reads a root `CLAUDE.md` *and* `AGENTS.md` and would load the rulebook twice. Your own root `CLAUDE.md` is never touched; Claude Code loads both | Claude Code (only if selected) |
| `.agents/skills/` | on-demand expertise (progressive disclosure): `security-review` + `differential-review` (Trail of Bits, CC-BY-SA), `data-modeling`, `tdd` + `diagnosing-bugs` (Matt Pocock, MIT), `webapp-testing` (Anthropic, Apache-2.0), `property-based-testing` (Trail of Bits), `prd` (GitHub, MIT), `ui-ux-pro-max` ([nextlevelbuilder](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill), MIT). Each carries a `SOURCE.md` and was security-audited before vendoring | every platform; agents also read them by path |
| `.claude/skills/<n>` | links to `.agents/skills/<n>` (symlink → Windows junction → copy), git-ignored | Claude Code (only if selected) |
| `.claude/agents/*.md` | the 10 agent prompts (+ `agent-template.md` for project specialists) | Claude Code, Cursor, Copilot |
| `.claude/settings.json` | hooks: **board-lint** (`tools/board_lint.py`, PreToolUse on Edit/Write/MultiEdit **and Bash** — blocks `status:done` without a resolving `evidence:` ref, and Bash writes to the board) + a debounced **code-brain refresh** (`tools/graphify/refresh_graph.py`) + session start/stop | Claude Code, Cursor, Copilot |
| `.mcp.json` | MCP servers (web-search, graphify, deepwiki, playwright), tool paths made absolute | Claude Code, Copilot |
| `.claude/coding-standards.md` · `project-context.md` · `task-board.md` · `design.md` | working docs (start as templates) | every agent, by path |
| `.claude/README.md` | this README (never overwrites your own root `README.md`) | you |

Safe to re-run:
- **Default** — skips every file that already exists (no data touched).
- **`--force`** — updates the *framework* files (agents, rulebook block, skills, hooks, `.mcp.json`) to the latest version, but **never** overwrites your working docs (`project-context.md`, `coding-standards.md`, `task-board.md`, `design.md`) or your text outside the harness blocks. On an install made before `AGENTS.md`, `--force` also migrates it: the old full root `CLAUDE.md` is removed (its content is now `AGENTS.md`), and shipped skills move from `.claude/skills/` to `.agents/skills/` (files you added inside them are kept).

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
- Shared rules live in one root `AGENTS.md`, not repeated per agent.
- Memory = plain files; "analyze the codebase" = grep/glob/read.
- Log one line per task, not 15 fields per action.
- senior-dev + reviewer review; tester can reject. Neither ceremony runs on a typo.
- Prod-grade baseline is non-negotiable: DRY, no magic strings (constants module), env config read from one place, lint clean before handoff. See `coding-standards.md`'s Non-negotiables.

## Custom tools
`tools/` holds capabilities beyond files+shell (web search, APIs, MCP). One core impl per tool, wired to Claude Code (MCP in `claude-code/.mcp.json`) and local (`tools/registry.py`). Included: `web_search/` → SearXNG (local, `SEARXNG_URL`), `deepwiki/` → public-repo docs+Q&A (remote MCP), `playwright` → browser automation (`npx @playwright/mcp`, needs Node). Needs `pip install mcp`. See `tools/README.md`.

## Quick start
- Install the team into a project: `python3 setup-team.py <path>` (see [Setup](#setup))
- Local (llama.cpp/Hermes) details: see `local/README.md`
- Add a tool: see `tools/README.md`

## Superseded
`CLAUDE_MASTER_PROMPT.md` is the original generator spec (kept for reference). The earlier generated output lives in `old/` — superseded by `claude-code/` and `local/`. It relied on nonexistent tools (GBrain, `claude memory add`), had two drifted rosters, and ~170-line boilerplate prompts. Safe to delete once you've confirmed the new folders.
