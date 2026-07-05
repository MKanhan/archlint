<p align="center">
  <img src=".github/banner.png" alt="archlint — architectural guardrails for coding with AI" width="100%">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/python-3.8%2B-3776AB" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/deps-stdlib%20only-0c0e13" alt="stdlib only">
  <img src="https://img.shields.io/badge/Claude%20Code-hook-eb369b" alt="Claude Code hook">
</p>

# archlint

A lightweight **Claude Code hook** that catches architectural drift **while your AI
agent is writing code** — by comparing edits against an architecture blueprint you
already keep in `CLAUDE.md`.

No daemon. No new config file. No LLM in the hot path. One Python script, standard
library only.

> It's the **seatbelt, not the steering wheel**: it surfaces drift the instant it
> happens — still inside the agent's turn, while fixing is cheap — and the agent
> decides what to do.

This repo is the free, **MIT-licensed lite tier** (the core: layers, `forbid`, the live
hook, `check`, real-world import resolution, four languages). The **Pro** bundle adds the
professional suite — see [Lite vs Pro](#lite-vs-pro) and **[archlint.pro](https://archlint.pro)**.

## What it does

While Claude Code edits your project:

- **After every `Write`/`Edit`/`MultiEdit`**, archlint parses the file, extracts its
  imports, and checks them against the layer rules in your blueprint. Cross a forbidden
  boundary (UI reaching into the database directly, say) and it surfaces a short,
  actionable note to the agent.
- **At the end of every turn (`Stop`)**, it emits a checkpoint of any unresolved drift,
  so the agent has one last chance to fix it before declaring done.

In **lenient** mode (default), drift is reported as context and the agent tends to
self-correct on the next step. In **strict** mode (`ARCHLINT_MODE=strict`), drift becomes
a hard block until it's gone.

## Install

### As a Claude Code plugin (recommended)

```
/plugin marketplace add MKanhan/archlint
/plugin install archlint@archlint
```

Start a new session to activate the hook, then declare your architecture (below).

### Manual

```bash
mkdir -p .claude/hooks
cp archlint.py .claude/hooks/archlint.py
```

Merge the `PostToolUse` + `Stop` blocks from [`settings.example.json`](settings.example.json)
into `.claude/settings.json`, and add `.archlint/` state files to your `.gitignore`.

## The blueprint

Add an `## Architecture` section to your `CLAUDE.md` (or `AGENTS.md` / `ARCHITECTURE.md`).
Inside it, a fenced `archlint` block declares your layers and rules:

````markdown
## Architecture

```archlint
layer routes   = src/routes/**, src/api/**
layer services = src/services/**
layer repos    = src/repos/**
layer ui       = src/ui/**, src/components/**

routes   -> services
services -> repos
ui       -> services

forbid ui -> repos
forbid * -> src/legacy/**
```
````

| Statement                        | Meaning                                            |
|----------------------------------|----------------------------------------------------|
| `layer NAME = glob, glob, ...`   | Define a layer by one or more file globs.          |
| `X -> Y`                         | `X` is allowed to depend on `Y`.                   |
| `forbid X -> Y`                  | Explicit prohibition (overrides allow).            |
| `forbid * -> path/glob/**`       | Anything depending on `path/...` is forbidden.     |
| `set max-file-lines N`           | Oversized-file smell threshold (default 500).      |

External imports (npm / pip / stdlib) are ignored — no false positives for packages you
don't own. A line that doesn't parse is **never silently dropped**: archlint surfaces it,
so a typo'd `forbid` can't quietly disable itself. With no blueprint, archlint does
nothing — it never breaks anyone's setup.

## Supported languages

**Python, TypeScript/JavaScript, Go, and Rust.** Import resolution understands the shapes
real projects use — tsconfig path aliases (`@/services/x`), the Python `src/` layout,
`go.mod` module prefixes, and Rust `crate::`/`self::`/`super::` paths.

## Beyond Claude Code

`check` runs the same engine as a CLI, for pre-commit and CI:

```bash
python3 .claude/hooks/archlint.py check   # text report, exit 1 on drift
```

## Modes

| Variable             | Values                        | Effect                                                    |
|----------------------|-------------------------------|-----------------------------------------------------------|
| `ARCHLINT_MODE`      | `lenient` (default), `strict` | `strict` blocks the agent on drift; `lenient` notifies.   |
| `ARCHLINT_BLUEPRINT` | path                          | Override the blueprint location.                          |
| `ARCHLINT_QUIET`     | `1`                           | Silence non-blocking feedback (blocks still surface).     |

## Lite vs Pro

The **lite** tier in this repo is genuinely useful on its own — the live guardrail across
four languages. **Pro** ($20, one-time) adds the professional layer:

| Lite (this repo, free · MIT)                          | Pro — [archlint.pro](https://archlint.pro)                              |
|-------------------------------------------------------|------------------------------------------------------------------------|
| Layers, `->` / `forbid`, blueprint diagnostics        | **Layer graph** viz (Mermaid / DOT / SVG / PNG)                        |
| Live hook (lenient + strict) + `check`                | **Drift report** — the self-correction rate                            |
| Import resolution (tsconfig, `src/`, go.mod, crate::) | **Cycle detection** with named witness edges                           |
| Python · TS/JS · Go · Rust                             | **`forbid!`** hard blocks · public-API **leakage** smells              |
| Forbidden / undeclared / file-size findings           | **`init`** (draft a blueprint) · **`baseline`** (adopt on legacy)      |
|                                                       | **SARIF** / GitHub code scanning · **Cursor** · **Java + C#**          |
|                                                       | **One-command install** + auto `@import` wiring for a dedicated blueprint |

## Limitations

- **Imports and globs, not semantics.** It catches a forbidden import boundary, not
  "this function name suggests a layer crossing" — that would need an LLM, kept off the
  hot path.
- **It doesn't fix things.** It surfaces drift; the agent decides. The seatbelt, not the
  steering wheel.

## License

MIT © Marcelo Kanhan. The lite tier is generated from the full source by stripping the
Pro regions (single source of truth). The Pro bundle is licensed separately — see
[archlint.pro](https://archlint.pro).

---

<p align="center"><sub>Built by <a href="https://www.kanhan.com.br">M. Kanhan</a> · <a href="https://archlint.pro">archlint.pro</a></sub></p>
