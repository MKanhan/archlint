---
name: check
description: Scan the whole project for architectural drift against the archlint blueprint (layer rules in CLAUDE.md). Use when the user asks to check architecture, find layer violations, or run archlint.
---

# archlint: check

Run a full architectural scan and report the findings.

1. Run from the project root:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/archlint.py" check
   ```
   If `${CLAUDE_PLUGIN_ROOT}` is not set in this shell, the script is the
   bundled `archlint.py` next to this plugin (or `.claude/hooks/archlint.py`
   in a per-project install) — run that path instead.

2. Exit code `1` means drift was found; `0` means clean. Relay the findings
   verbatim (they are short and actionable), then offer to fix them by routing
   through allowed layers or adjusting the blueprint.
