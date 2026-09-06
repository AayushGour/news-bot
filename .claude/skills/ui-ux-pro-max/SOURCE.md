# Source & attribution

Vendored from **ui-ux-pro-max-skill** by Next Level Builder.
- Upstream: https://github.com/nextlevelbuilder/ui-ux-pro-max-skill
- License: MIT (see `LICENSE` in this folder) — © 2024 Next Level Builder.

Only the `ui-ux-pro-max` skill is vendored (the upstream repo ships several).
Pure Python stdlib — no pip install needed. To update, re-copy this skill dir
from the upstream repo, excluding `__pycache__`/`*.pyc`.

## Local change vs upstream
`SKILL.md` command paths were changed from `${CLAUDE_PLUGIN_ROOT}/.claude/skills/ui-ux-pro-max/scripts/search.py`
to the project-root-relative `.claude/skills/ui-ux-pro-max/scripts/search.py`. `CLAUDE_PLUGIN_ROOT`
is set only when a skill loads as a *plugin*; vendored into a project's `.claude/skills/` it is unset,
which produced a broken path. The script resolves its own data via `Path(__file__)`, so it is
cwd-independent — only the path to `search.py` matters, and it is run directly via Bash (not the Skill tool).
Re-apply this change if you re-copy from upstream.
