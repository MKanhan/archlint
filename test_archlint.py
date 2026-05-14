#!/usr/bin/env python3
"""End-to-end tests for archlint.

Builds a temp project with a CLAUDE.md blueprint, then simulates the JSON
events Claude Code would send to PostToolUse and Stop hooks, asserting on
the JSON the hook returns.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).parent / "archlint.py"

BLUEPRINT = """\
# Project

Some prose.

## Architecture

We follow a layered structure.

```archlint
layer routes   = src/routes/**
layer services = src/services/**
layer repos    = src/repos/**
layer models   = src/models/**
layer ui       = src/ui/**

routes   -> services
services -> repos
repos    -> models
ui       -> services

forbid ui -> repos
forbid ui -> models
forbid routes -> repos
forbid * -> src/legacy/**
```
"""


def make_project(tmp: Path) -> Path:
    """Create a fake project tree and return its root."""
    (tmp / "CLAUDE.md").write_text(BLUEPRINT)
    src = tmp / "src"
    for sub in ("routes", "services", "repos", "models", "ui", "legacy"):
        (src / sub).mkdir(parents=True, exist_ok=True)
        (src / sub / "__init__.py").write_text("")

    # A clean services file
    (src / "services" / "user.py").write_text(
        "from src.repos import user_repo\n"
        "def get_user(uid):\n"
        "    return user_repo.find(uid)\n"
    )
    # A clean repo
    (src / "repos" / "user_repo.py").write_text(
        "from src.models import user\n"
        "def find(uid):\n"
        "    return user.User(uid)\n"
    )
    # A model
    (src / "models" / "user.py").write_text(
        "class User:\n"
        "    def __init__(self, uid):\n"
        "        self.uid = uid\n"
    )
    # Legacy file (anything depending on it should trip the forbid * -> legacy/**)
    (src / "legacy" / "old.py").write_text("def old(): pass\n")
    return tmp


def run_hook(action: str, project: Path, event: dict, mode: str = "lenient") -> dict:
    """Invoke the hook the way Claude Code does and parse its JSON output."""
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env["ARCHLINT_MODE"] = mode
    proc = subprocess.run(
        [sys.executable, str(HOOK), action],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"hook exited {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    out = proc.stdout.strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise AssertionError(f"hook produced non-JSON output: {out!r} ({e})")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_clean_edit_returns_empty():
    with tempfile.TemporaryDirectory() as d:
        proj = make_project(Path(d))
        # Edit a services file that imports a repo — this is allowed.
        edited = proj / "src" / "services" / "user.py"
        event = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(edited)},
        }
        result = run_hook("post", proj, event)
        assert result == {}, f"expected no findings, got {result}"


def test_forbidden_ui_to_repo_is_flagged():
    with tempfile.TemporaryDirectory() as d:
        proj = make_project(Path(d))
        bad = proj / "src" / "ui" / "user_view.py"
        bad.write_text(
            "from src.repos import user_repo\n"
            "def render(uid): return user_repo.find(uid)\n"
        )
        event = {"tool_name": "Write", "tool_input": {"file_path": str(bad)}}
        result = run_hook("post", proj, event)
        ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "forbidden" in ctx.lower(), f"expected forbidden warning, got: {ctx!r}"
        assert "ui" in ctx and "repos" in ctx, f"missing layer names in: {ctx!r}"
        # The suggestion engine should propose services as a bridge.
        assert "services" in ctx, f"expected 'route through services' suggestion, got: {ctx!r}"


def test_undeclared_crossing_is_flagged():
    """services -> models is not declared (services -> repos is); flag it."""
    with tempfile.TemporaryDirectory() as d:
        proj = make_project(Path(d))
        bad = proj / "src" / "services" / "leaky.py"
        bad.write_text(
            "from src.models import user\n"
            "def x(): return user.User(1)\n"
        )
        event = {"tool_name": "Write", "tool_input": {"file_path": str(bad)}}
        result = run_hook("post", proj, event)
        ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "undeclared" in ctx.lower() or "not declared" in ctx.lower(), \
            f"expected undeclared warning, got: {ctx!r}"


def test_legacy_glob_forbid():
    """`forbid * -> src/legacy/**` should catch anything depending on legacy."""
    with tempfile.TemporaryDirectory() as d:
        proj = make_project(Path(d))
        bad = proj / "src" / "services" / "uses_legacy.py"
        bad.write_text(
            "from src.legacy import old\n"
            "def x(): return old.old()\n"
        )
        event = {"tool_name": "Write", "tool_input": {"file_path": str(bad)}}
        result = run_hook("post", proj, event)
        ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "legacy" in ctx.lower(), f"expected legacy warning, got: {ctx!r}"


def test_strict_mode_blocks():
    with tempfile.TemporaryDirectory() as d:
        proj = make_project(Path(d))
        bad = proj / "src" / "ui" / "view.py"
        bad.write_text("from src.repos import user_repo\n")
        event = {"tool_name": "Write", "tool_input": {"file_path": str(bad)}}
        result = run_hook("post", proj, event, mode="strict")
        assert result.get("decision") == "block", f"expected block, got: {result}"
        assert "forbidden" in result.get("reason", "").lower()


def test_stop_aggregates_turn():
    with tempfile.TemporaryDirectory() as d:
        proj = make_project(Path(d))

        bad1 = proj / "src" / "ui" / "a.py"
        bad1.write_text("from src.repos import user_repo\n")
        run_hook("post", proj, {"tool_name": "Write", "tool_input": {"file_path": str(bad1)}})

        bad2 = proj / "src" / "ui" / "b.py"
        bad2.write_text("from src.models import user\n")
        run_hook("post", proj, {"tool_name": "Write", "tool_input": {"file_path": str(bad2)}})

        # Stop event
        result = run_hook("stop", proj, {"stop_hook_active": False})
        ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "checkpoint" in ctx.lower(), f"expected checkpoint summary, got: {ctx!r}"
        assert "a.py" in ctx and "b.py" in ctx, f"expected both files in summary: {ctx!r}"


def test_stop_hook_active_short_circuits():
    """If stop_hook_active is true, don't loop — just allow stop."""
    with tempfile.TemporaryDirectory() as d:
        proj = make_project(Path(d))
        bad = proj / "src" / "ui" / "x.py"
        bad.write_text("from src.repos import user_repo\n")
        run_hook("post", proj, {"tool_name": "Write", "tool_input": {"file_path": str(bad)}})
        result = run_hook("stop", proj, {"stop_hook_active": True})
        assert result == {}, f"expected empty allow-stop, got: {result}"


def test_no_blueprint_is_noop():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        # No CLAUDE.md at all.
        event = {"tool_name": "Edit", "tool_input": {"file_path": str(proj / "foo.py")}}
        result = run_hook("post", proj, event)
        assert result == {}


def test_blueprint_without_archlint_block_is_noop():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d)
        (proj / "CLAUDE.md").write_text("# Project\n\nSome prose with no archlint block.\n")
        event = {"tool_name": "Edit", "tool_input": {"file_path": str(proj / "foo.py")}}
        result = run_hook("post", proj, event)
        assert result == {}


def test_multiedit_input():
    with tempfile.TemporaryDirectory() as d:
        proj = make_project(Path(d))
        bad = proj / "src" / "ui" / "multi.py"
        bad.write_text("from src.repos import user_repo\n")
        event = {
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": str(bad),
                "edits": [{"old_string": "x", "new_string": "y"}],
            },
        }
        result = run_hook("post", proj, event)
        ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "forbidden" in ctx.lower(), f"got: {ctx!r}"


def test_js_import_extraction():
    """Make sure non-Python files participate in checking."""
    with tempfile.TemporaryDirectory() as d:
        proj = make_project(Path(d))
        # Drop in a TS file under ui/
        ts = proj / "src" / "ui" / "Widget.ts"
        ts.write_text(
            "import { findUser } from '../repos/user_repo';\n"
            "export const w = () => findUser(1);\n"
        )
        # ...and a corresponding .ts in repos so the resolver can find it
        (proj / "src" / "repos" / "user_repo.ts").write_text("export const findUser = (id: number) => id;\n")
        event = {"tool_name": "Write", "tool_input": {"file_path": str(ts)}}
        result = run_hook("post", proj, event)
        ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "forbidden" in ctx.lower(), f"TS import not caught: {ctx!r}"


def test_check_command_exits_nonzero_on_drift():
    """`archlint.py check` is a CI/pre-commit entrypoint."""
    with tempfile.TemporaryDirectory() as d:
        proj = make_project(Path(d))
        (proj / "src" / "ui" / "bad.py").write_text("from src.repos import user_repo\n")
        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = str(proj)
        proc = subprocess.run(
            [sys.executable, str(HOOK), "check"],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert proc.returncode == 1, f"expected exit 1, got {proc.returncode}: {proc.stdout}"
        assert "forbidden" in proc.stdout.lower()


# ---------------------------------------------------------------------------

def main():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = []
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            failures.append(t.__name__)
        except Exception as e:
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
            failures.append(t.__name__)
    print()
    print(f"{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
