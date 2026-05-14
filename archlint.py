#!/usr/bin/env python3
"""
archlint — architectural drift detector for Claude Code.

A single-file hook that:
  1. Reads an architecture blueprint from CLAUDE.md or ARCHITECTURE.md.
  2. On every Write/Edit/MultiEdit, checks the edited file against the
     declared layer rules (deterministic, AST/regex-based).
  3. On Stop (end-of-turn), emits a checkpoint summary so the agent
     can self-correct before declaring done.

Stdlib only. No network. No LLM calls in the hot path.

Hook input arrives on stdin as JSON; output goes to stdout as JSON.
Exit code 0 = continue. Exit code 2 = block (used sparingly; the
JSON `decision: block` channel is preferred and gives a reason).

Install:
    Place this file at  .claude/hooks/archlint.py  (chmod +x)
    Add to .claude/settings.json:

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

Environment:
    ARCHLINT_MODE        lenient (default) | strict
    ARCHLINT_BLUEPRINT   path override; defaults to CLAUDE.md, ARCHITECTURE.md, SPEC.md (first found)
    ARCHLINT_QUIET       1 to silence non-blocking output
"""

from __future__ import annotations

import ast
import fnmatch
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Paths & state
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
STATE_DIR = PROJECT_DIR / ".archlint"
STATE_DIR.mkdir(exist_ok=True)
TURN_STATE = STATE_DIR / "turn.json"  # accumulates findings within a turn
DRIFT_LOG = STATE_DIR / "drift.log"

BLUEPRINT_CANDIDATES = ("CLAUDE.md", "ARCHITECTURE.md", "SPEC.md", "ROADMAP.md")
MODE = os.environ.get("ARCHLINT_MODE", "lenient").lower()
QUIET = os.environ.get("ARCHLINT_QUIET") == "1"

# ---------------------------------------------------------------------------
# Blueprint parsing
# ---------------------------------------------------------------------------


@dataclass
class Rules:
    layers: dict[str, list[str]] = field(default_factory=dict)  # name -> globs
    allowed: set[tuple[str, str]] = field(default_factory=set)  # (src, dst)
    forbidden: list[tuple[str, str]] = field(default_factory=list)  # (src_pattern, dst_pattern)
    raw_block: str = ""

    def layer_of(self, rel_path: str) -> str | None:
        """Return the layer name a path belongs to, or None."""
        # Normalize to forward slashes for glob matching
        p = rel_path.replace(os.sep, "/")
        for name, globs in self.layers.items():
            for g in globs:
                if fnmatch.fnmatch(p, g):
                    return name
        return None


def find_blueprint() -> Path | None:
    override = os.environ.get("ARCHLINT_BLUEPRINT")
    if override:
        path = Path(override)
        if not path.is_absolute():
            path = PROJECT_DIR / path
        return path if path.exists() else None
    for name in BLUEPRINT_CANDIDATES:
        path = PROJECT_DIR / name
        if path.exists():
            return path
    return None


# Match a fenced block: ```archlint ... ```
_FENCE_RE = re.compile(r"```archlint\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def parse_blueprint(text: str) -> Rules:
    """Extract rules from a markdown blueprint.

    The blueprint must contain a fenced code block with language `archlint`.
    Grammar (one statement per line; # starts a comment):

        layer <name> = <glob>, <glob>, ...
        <name> -> <name>               (allowed dependency)
        forbid <name|glob> -> <name|glob>
    """
    rules = Rules()
    match = _FENCE_RE.search(text)
    if not match:
        return rules
    rules.raw_block = match.group(1)

    for raw_line in rules.raw_block.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue

        # layer NAME = glob, glob
        m = re.match(r"^layer\s+([A-Za-z0-9_\-]+)\s*=\s*(.+)$", line)
        if m:
            name = m.group(1)
            globs = [g.strip() for g in m.group(2).split(",") if g.strip()]
            rules.layers[name] = globs
            continue

        # forbid X -> Y
        m = re.match(r"^forbid\s+(\S+)\s*->\s*(\S+)$", line)
        if m:
            rules.forbidden.append((m.group(1), m.group(2)))
            continue

        # X -> Y  (allowed dependency)
        m = re.match(r"^(\S+)\s*->\s*(\S+)$", line)
        if m:
            rules.allowed.add((m.group(1), m.group(2)))
            continue

        # Unknown line — ignore silently. Future: collect for diagnostics.

    return rules


# ---------------------------------------------------------------------------
# Import extraction
# ---------------------------------------------------------------------------


def extract_imports(file_path: Path) -> list[str]:
    """Return raw module strings imported by `file_path`. Language-aware-ish."""
    suffix = file_path.suffix.lower()
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return []

    if suffix == ".py":
        return _imports_python(text)
    if suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
        return _imports_js(text)
    if suffix == ".go":
        return _imports_go(text)
    if suffix in {".rs"}:
        return _imports_rust(text)
    return []


def _imports_python(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                # Preserve relative-import dots if present
                prefix = "." * (node.level or 0)
                out.append(prefix + node.module)
            elif node.level:
                out.append("." * node.level)
    return out


_JS_IMPORT_RE = re.compile(
    r"""(?mx)
    ^\s*(?:
        import\s+(?:[^'"`;]+?\s+from\s+)?['"]([^'"]+)['"]
      | (?:const|let|var)\s+[^=;]+?=\s*require\(\s*['"]([^'"]+)['"]\s*\)
      | export\s+(?:\*|\{[^}]*\})\s+from\s+['"]([^'"]+)['"]
    )
    """
)


def _imports_js(text: str) -> list[str]:
    out: list[str] = []
    for m in _JS_IMPORT_RE.finditer(text):
        spec = m.group(1) or m.group(2) or m.group(3)
        if spec:
            out.append(spec)
    return out


_GO_IMPORT_BLOCK_RE = re.compile(r"import\s*\(([^)]*)\)", re.DOTALL)
_GO_IMPORT_SINGLE_RE = re.compile(r'import\s+"([^"]+)"')
_GO_QUOTED_RE = re.compile(r'"([^"]+)"')


def _imports_go(text: str) -> list[str]:
    out: list[str] = []
    for block in _GO_IMPORT_BLOCK_RE.findall(text):
        out.extend(_GO_QUOTED_RE.findall(block))
    out.extend(_GO_IMPORT_SINGLE_RE.findall(text))
    return out


_RUST_USE_RE = re.compile(r"^\s*use\s+([^;{]+)", re.MULTILINE)


def _imports_rust(text: str) -> list[str]:
    return [m.group(1).strip() for m in _RUST_USE_RE.finditer(text)]


# ---------------------------------------------------------------------------
# Import → layer resolution
# ---------------------------------------------------------------------------


def resolve_import_to_path(importer: Path, spec: str) -> Path | None:
    """Best-effort: map an import spec to a project-relative path.

    Only resolves things that look like local imports (./, ../, or a
    project-root-relative path). External package imports return None
    and are ignored by layer rules.
    """
    # Python relative import (".pkg" or "..pkg"). No slashes — those are JS/TS.
    if spec.startswith(".") and "/" not in spec:
        # Walk up `level` dirs from the importer
        level = len(spec) - len(spec.lstrip("."))
        rest = spec[level:].replace(".", "/")
        base = importer.parent
        for _ in range(level - 1):
            base = base.parent
        candidate = (base / rest) if rest else base
        return _first_existing_module(candidate)

    # JS/TS relative
    if spec.startswith("./") or spec.startswith("../"):
        candidate = (importer.parent / spec).resolve()
        return _first_existing_module(candidate)

    # Try treating as project-root-relative (e.g. "src/services/user")
    candidate = PROJECT_DIR / spec
    resolved = _first_existing_module(candidate)
    if resolved:
        return resolved

    # Python dotted path — try project root
    if re.fullmatch(r"[A-Za-z0-9_.]+", spec):
        candidate = PROJECT_DIR / spec.replace(".", os.sep)
        return _first_existing_module(candidate)

    return None


_MODULE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs")


def _first_existing_module(base: Path) -> Path | None:
    if base.is_file():
        return base
    # Try adding each known extension. `with_suffix` only works if `base`
    # already has a suffix to swap; for suffix-less imports like
    # "../repos/user_repo" we need to append.
    for suf in _MODULE_SUFFIXES:
        cand = base.parent / (base.name + suf)
        if cand.is_file():
            return cand
        # Also handle the case where base does have a dotted suffix already
        # (e.g. a relative import resolved into a path with dots).
        if base.suffix:
            swapped = base.with_suffix(suf)
            if swapped.is_file():
                return swapped
    # index.* in a directory
    if base.is_dir():
        for suf in _MODULE_SUFFIXES:
            cand = base / f"index{suf}"
            if cand.is_file():
                return cand
        for suf in _MODULE_SUFFIXES:
            cand = base / f"__init__{suf}"
            if cand.is_file():
                return cand
    return None


# ---------------------------------------------------------------------------
# Rule checking
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    kind: str        # "forbidden" | "undeclared" | "smell"
    file: str        # relative path
    detail: str      # human-readable
    suggestion: str = ""


def _matches_target(target: str, layer: str | None, rel_path: str) -> bool:
    """A forbid target can be a layer name OR a glob over paths."""
    if layer is not None and target == layer:
        return True
    if any(c in target for c in "*?["):
        return fnmatch.fnmatch(rel_path.replace(os.sep, "/"), target)
    return False


def check_file(file_path: Path, rules: Rules) -> list[Finding]:
    """Check a single edited file against the rules."""
    findings: list[Finding] = []
    try:
        rel = file_path.resolve().relative_to(PROJECT_DIR)
    except ValueError:
        return findings  # outside the project; skip
    rel_str = str(rel)

    src_layer = rules.layer_of(rel_str)
    imports = extract_imports(file_path)

    for spec in imports:
        target_path = resolve_import_to_path(file_path, spec)
        if target_path is None:
            continue  # external dependency
        try:
            target_rel = str(target_path.resolve().relative_to(PROJECT_DIR))
        except ValueError:
            continue
        dst_layer = rules.layer_of(target_rel)
        forbidden_hit = False

        # 1. Forbidden rules (explicit blacklist)
        for src_pat, dst_pat in rules.forbidden:
            src_match = (
                src_pat == "*"
                or (src_layer is not None and src_pat == src_layer)
                or _matches_target(src_pat, src_layer, rel_str)
            )
            dst_match = (
                dst_pat == "*"
                or (dst_layer is not None and dst_pat == dst_layer)
                or _matches_target(dst_pat, dst_layer, target_rel)
            )
            if src_match and dst_match:
                forbidden_hit = True
                findings.append(Finding(
                    kind="forbidden",
                    file=rel_str,
                    detail=f"{rel_str} imports {target_rel} (forbidden: {src_pat} -> {dst_pat})",
                    suggestion=_suggest_for_forbidden(src_pat, dst_pat, rules),
                ))

        # 2. Undeclared cross-layer dependency — only if not already flagged above.
        if forbidden_hit:
            continue
        if (
            src_layer is not None
            and dst_layer is not None
            and src_layer != dst_layer
            and (src_layer, dst_layer) not in rules.allowed
        ):
            # Only flag if there ARE any allowed edges from this src — otherwise
            # the layer has no declared outgoing rules and we stay quiet.
            if any(s == src_layer for s, _ in rules.allowed):
                findings.append(Finding(
                    kind="undeclared",
                    file=rel_str,
                    detail=(
                        f"{rel_str} ({src_layer}) imports {target_rel} ({dst_layer}) "
                        f"but `{src_layer} -> {dst_layer}` is not declared"
                    ),
                    suggestion=(
                        f"Either route through an allowed layer, or add "
                        f"`{src_layer} -> {dst_layer}` to the blueprint if intentional."
                    ),
                ))

    # 3. Lightweight smells (one for v1: oversized files)
    try:
        line_count = sum(1 for _ in file_path.open("r", encoding="utf-8", errors="replace"))
    except OSError:
        line_count = 0
    if line_count > 500:
        findings.append(Finding(
            kind="smell",
            file=rel_str,
            detail=f"{rel_str} is {line_count} lines — consider splitting",
            suggestion="Large files often indicate a missing layer or mixed concerns.",
        ))

    return findings


def _suggest_for_forbidden(src_pat: str, dst_pat: str, rules: Rules) -> str:
    """Suggest a known-good route: src -> ? -> dst, one hop."""
    for mid in rules.layers:
        if (src_pat, mid) in rules.allowed and (mid, dst_pat) in rules.allowed:
            return f"Route through `{mid}` instead: {src_pat} -> {mid} -> {dst_pat}."
    return f"This crosses a forbidden boundary. Find an allowed path from `{src_pat}`."


# ---------------------------------------------------------------------------
# Hook event handlers
# ---------------------------------------------------------------------------


def _load_turn_state() -> dict:
    if TURN_STATE.exists():
        try:
            return json.loads(TURN_STATE.read_text())
        except json.JSONDecodeError:
            return {"findings": [], "started": time.time()}
    return {"findings": [], "started": time.time()}


def _save_turn_state(state: dict) -> None:
    TURN_STATE.write_text(json.dumps(state))


def _emit(payload: dict) -> None:
    """Write hook output JSON and exit."""
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()
    sys.exit(0)


def handle_post_tool_use(event: dict, rules: Rules) -> None:
    """Per-edit check. Surface findings as additionalContext (lenient)
    or as a soft block (strict)."""
    tool_input = event.get("tool_input", {}) or {}
    edited: list[str] = []
    # Write / Edit
    if "file_path" in tool_input:
        edited.append(tool_input["file_path"])
    # MultiEdit
    if "edits" in tool_input and isinstance(tool_input["edits"], list):
        if "file_path" in tool_input:
            pass  # already added
        else:
            for e in tool_input["edits"]:
                if isinstance(e, dict) and "file_path" in e:
                    edited.append(e["file_path"])

    all_findings: list[Finding] = []
    for fp in edited:
        path = Path(fp)
        if not path.is_absolute():
            path = PROJECT_DIR / path
        if not path.exists():
            continue
        all_findings.extend(check_file(path, rules))

    if not all_findings:
        _emit({})

    # Persist for the Stop checkpoint
    state = _load_turn_state()
    for f in all_findings:
        state["findings"].append({
            "kind": f.kind, "file": f.file,
            "detail": f.detail, "suggestion": f.suggestion,
        })
    _save_turn_state(state)

    # Log for history
    with DRIFT_LOG.open("a") as log:
        for f in all_findings:
            log.write(f"{int(time.time())}\t{f.kind}\t{f.detail}\n")

    message = _format_findings(all_findings, header="archlint: drift detected on this edit")

    if MODE == "strict":
        _emit({
            "decision": "block",
            "reason": message,
        })
    # Lenient: surface to the model, but don't block.
    _emit({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": message,
        }
    })


def handle_stop(event: dict, rules: Rules) -> None:
    """End-of-turn checkpoint: aggregate and summarize."""
    # Prevent loops: if Claude is already stopping because of us, let it stop.
    if event.get("stop_hook_active"):
        TURN_STATE.unlink(missing_ok=True)
        _emit({})

    state = _load_turn_state()
    findings_raw = state.get("findings", [])
    TURN_STATE.unlink(missing_ok=True)

    if not findings_raw:
        _emit({})

    findings = [Finding(**f) for f in findings_raw]
    message = _format_findings(findings, header="archlint checkpoint: unresolved drift before stopping")

    if MODE == "strict":
        _emit({
            "decision": "block",
            "reason": message + "\n\nResolve these before declaring done.",
        })
    _emit({
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": message,
        }
    })


def _format_findings(findings: Iterable[Finding], header: str) -> str:
    lines = [header, ""]
    by_kind: dict[str, list[Finding]] = {}
    for f in findings:
        by_kind.setdefault(f.kind, []).append(f)

    labels = {
        "forbidden": "Forbidden dependencies",
        "undeclared": "Undeclared cross-layer dependencies",
        "smell": "Code smells",
    }
    for kind in ("forbidden", "undeclared", "smell"):
        items = by_kind.get(kind)
        if not items:
            continue
        lines.append(f"{labels[kind]}:")
        for f in items:
            lines.append(f"  - {f.detail}")
            if f.suggestion:
                lines.append(f"    → {f.suggestion}")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: archlint.py {post|stop|check}\n")
        sys.exit(1)
    action = sys.argv[1]

    blueprint = find_blueprint()
    if blueprint is None:
        # No blueprint — silently no-op. archlint should never break a workflow.
        _emit({})

    rules = parse_blueprint(blueprint.read_text(encoding="utf-8", errors="replace"))
    if not rules.layers:
        # Blueprint exists but no archlint block — also a no-op.
        _emit({})

    if action == "post":
        try:
            event = json.load(sys.stdin)
        except json.JSONDecodeError:
            event = {}
        handle_post_tool_use(event, rules)
    elif action == "stop":
        try:
            event = json.load(sys.stdin)
        except json.JSONDecodeError:
            event = {}
        handle_stop(event, rules)
    elif action == "check":
        # Manual one-shot: scan all tracked files. Useful as a pre-commit hook.
        run_full_scan(rules)
    else:
        sys.stderr.write(f"unknown action: {action}\n")
        sys.exit(1)


def run_full_scan(rules: Rules) -> None:
    """CLI mode: archlint check — scan every source file in the project."""
    exts = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs"}
    skip_dirs = {".git", "node_modules", ".venv", "venv", "dist", "build", ".archlint", "__pycache__"}
    all_findings: list[Finding] = []
    for root, dirs, files in os.walk(PROJECT_DIR):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            if Path(name).suffix.lower() in exts:
                all_findings.extend(check_file(Path(root) / name, rules))

    if not all_findings:
        print("archlint: no drift detected.")
        sys.exit(0)
    print(_format_findings(all_findings, header=f"archlint: {len(all_findings)} finding(s)"))
    sys.exit(1)


if __name__ == "__main__":
    main()
