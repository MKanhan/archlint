# archlint

Claude Code hook that catches architectural drift while the agent is writing
code. Stdlib-only Python. Reads layer rules from a fenced ` ```archlint ` block
inside `CLAUDE.md` (or `ARCHITECTURE.md` / `SPEC.md` / `ROADMAP.md`); checks
edited files' imports against those rules on PostToolUse and Stop events.

## Repo layout

```
archlint/
├── archlint.py            # single-file hook + CLI (630 lines, stdlib only)
├── test_archlint.py       # 12 end-to-end tests (stdlib only)
├── settings.example.json  # example .claude/settings.json hook block
├── README.md              # user-facing docs
├── LICENSE                # MIT
└── CLAUDE.md              # this file (project notes)
```

## Development

```bash
python3 test_archlint.py
```

No deps to install. Tests build a tmp project + simulate hook JSON I/O.

## Adding a language

`extract_imports()` dispatches by file suffix. Add 15-line regex (or
preferred parser) for new language. See `_imports_python` (AST),
`_imports_js` (regex), `_imports_go` (regex), `_imports_rust` (regex) for
the pattern.

`_MODULE_SUFFIXES` tuple needs the new extensions for path resolution.

## Design decisions

- **Stdlib only.** Hook should never break a project's setup. No `pip install`.
- **Lenient by default.** Drift becomes `additionalContext` (the LLM gets the
  message but isn't forced to act). Strict mode (`ARCHLINT_MODE=strict`) makes
  drift a `decision: block` — for when discipline matters more than velocity.
- **State per-turn, not persistent.** `.archlint/turn.json` accumulates findings
  during a turn; deleted on Stop. `.archlint/drift.log` is append-only history.
- **External imports ignored.** No false positives for npm/pip packages.
- **Imports + globs, not semantic.** Catches structural drift, not "this function
  is named like it belongs in another layer." That'd need an LLM, which we keep
  optional (off the hot path).
- **Same code, same rules for live + CI.** `archlint check` is the CLI mode,
  same parser as the hook.

## Origin

Born from prevention of structural drift in operator's own multi-macro-system
workspace (`~/Documents/`). Generalized + published as standalone OSS 2026-05-14.

## Roadmap

Not formalized yet. Likely additions if usage proves it:

- `archlint init` — Claude-drafted initial blueprint from existing tree.
- More languages: Java, Kotlin, Swift, C#, Ruby, PHP.
- Smarter smells beyond oversized files (cyclic dep detection, public API leakage).
- Severity tiers in blueprint (`forbid!` vs `forbid`).

Anything the OSS user community surfaces drives the priority.

## License

MIT. See `LICENSE`.
