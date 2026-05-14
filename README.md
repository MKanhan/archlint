# archlint

A lightweight Claude Code hook that catches architectural drift while the agent
is writing code — by comparing edits against an architecture blueprint you
already keep in `CLAUDE.md`.

No daemon. No new config file. No LLM in the hot path. One Python script,
stdlib only.

## What it does

While Claude Code edits your project:

- **After every `Write`/`Edit`/`MultiEdit`**, archlint parses the file,
  extracts its imports, and checks them against the layer rules declared in
  your blueprint. If a forbidden boundary is crossed (e.g. UI reaching into
  the database directly), it surfaces a short, actionable note to Claude.
- **At the end of every turn (`Stop`)**, archlint emits a checkpoint summary
  of any unresolved drift, so Claude has one last chance to fix it before
  declaring done.

In `lenient` mode (the default), drift is reported as `additionalContext` and
Claude tends to self-correct on the next step. In `strict` mode, drift becomes
a `decision: block`, which forces Claude to keep working until the violation
is gone.

## The blueprint

Add an `## Architecture` section to your `CLAUDE.md` (or `ARCHITECTURE.md`,
`SPEC.md`, `ROADMAP.md` — first one found wins). Inside it, a fenced
`archlint` block declares your layers and rules:

````markdown
## Architecture

We follow a layered structure: routes → services → repositories → models.
The UI must never reach into the database directly.

```archlint
layer routes   = src/routes/**, src/api/**
layer services = src/services/**
layer repos    = src/repos/**
layer models   = src/models/**
layer ui       = src/ui/**, src/components/**

routes   -> services
services -> repos
repos    -> models
ui       -> services

forbid ui -> repos
forbid ui -> models
forbid routes -> repos
forbid * -> src/legacy/**
```
````

Grammar (one statement per line, `#` starts a comment):

| Statement                           | Meaning                                        |
|-------------------------------------|------------------------------------------------|
| `layer NAME = glob, glob, ...`      | Define a layer by one or more file globs.      |
| `X -> Y`                            | `X` is allowed to depend on `Y`.               |
| `forbid X -> Y`                     | Explicit prohibition (overrides allow).        |
| `forbid * -> path/glob/**`          | Anything depending on `path/...` is forbidden. |

External imports (npm packages, pip packages, stdlib) are ignored.

## Install

Drop the script into your project:

```bash
mkdir -p .claude/hooks
curl -fsSL https://raw.githubusercontent.com/MKanhan/archlint/main/archlint.py \
  -o .claude/hooks/archlint.py
chmod +x .claude/hooks/archlint.py
```

Then merge this into `.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "Write|Edit|MultiEdit",
      "hooks": [{
        "type": "command",
        "command": "python3 $CLAUDE_PROJECT_DIR/.claude/hooks/archlint.py post"
      }]
    }],
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "python3 $CLAUDE_PROJECT_DIR/.claude/hooks/archlint.py stop"
      }]
    }]
  }
}
```

Add `.archlint/` to your `.gitignore` (it's where the per-turn state and the
drift log live).

## Use it outside Claude Code too

```bash
# Pre-commit / CI: scan the whole project, exit 1 on drift.
python3 .claude/hooks/archlint.py check
```

Wire it into git as a pre-commit hook:

```bash
# .git/hooks/pre-commit
#!/usr/bin/env bash
python3 .claude/hooks/archlint.py check
```

## Modes

| Env var               | Values                  | Effect                                                                            |
|-----------------------|-------------------------|-----------------------------------------------------------------------------------|
| `ARCHLINT_MODE`       | `lenient` (default), `strict` | `strict` makes drift block Claude via `decision: block`; `lenient` just notifies. |
| `ARCHLINT_BLUEPRINT`  | path                    | Override blueprint location.                                                       |
| `ARCHLINT_QUIET`      | `1`                     | Suppress non-blocking output (reserved for future use).                            |

## Supported languages

Import extraction works for: Python (AST), TypeScript/JavaScript (regex),
Go (regex), Rust (regex). External imports and unresolved specs are simply
skipped — no false positives for third-party packages.

Adding a language is ~15 lines: a regex (or AST) for import extraction, and
a couple of suffixes in `_MODULE_SUFFIXES`.

## What it doesn't do (yet)

- **Doesn't auto-generate the blueprint.** You write the rules once. A
  companion `archlint init` that asks Claude to draft an initial blueprint
  from the existing tree is the obvious next step.
- **Doesn't do deep semantic analysis.** It works on imports and globs. It
  won't catch "this function name suggests a layer crossing" — that needs
  an LLM, which we'd rather keep optional.
- **Doesn't fix things.** It surfaces drift; Claude decides what to do. By
  design — we're the seatbelt, not the steering wheel.

## Why a hook and not a static analyzer

Static analyzers run at CI time, after the architecture has already drifted.
Hooks run *inside the agent loop*, while there's still a chance to course-
correct cheaply. The whole point is to prevent drift from compounding across
the 40 tool-calls of a single turn — not just to detect it after the fact.

Both jobs are valuable. `archlint check` exists for the CI/pre-commit case;
the hook is for the live-coding case. Same code, same rules.

## License

MIT.
