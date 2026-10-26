"""Wrapper tests without a real Codex: a fake `codex` on PATH writes events and the report.

python3 -m pytest -q tests
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "plugins" / "codex-subagent" / "skills" / "codex" / "codex_agent.py"

FAKE = r"""#!/usr/bin/env python3
import json, os, subprocess, sys, time
args = sys.argv[1:]
if "--help" in args:
    print(os.environ.get("FAKE_HELP", "--ignore-user-config\n--ignore-rules\n--disable <FEATURE>"))
    sys.exit(0)
if args[:2] == ["debug", "models"]:
    if "FAKE_CATALOG" not in os.environ:  # an older CLI without the command
        sys.exit(2)
    print(os.environ["FAKE_CATALOG"])
    sys.exit(0)
prompt = sys.stdin.read()
log = os.environ["FAKE_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps({"argv": args, "cwd": os.getcwd(), "prompt": prompt}) + "\n")
answer = args[args.index("-o") + 1]
mode = os.environ.get("FAKE_MODE", "ok")
thread = args[args.index("resume") + 1] if "resume" in args else "thread-1"

def emit(event):
    print(json.dumps(event), flush=True)

emit({"type": "thread.started", "thread_id": thread})
emit({"type": "turn.started"})
if mode == "hang":
    child = subprocess.Popen(["sleep", "600"], start_new_session=True)  # like exec_command: its own group
    open(os.environ["FAKE_CHILD"], "w").write(str(child.pid))
    time.sleep(600)
if mode in ("busy", "busy_midway", "busy_forever"):
    counter = os.environ["FAKE_LOG"] + ".busy"
    seen = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(seen + 1))
    time.sleep(float(os.environ.get("FAKE_BUSY_DELAY", "0")))  # the CLI retries 429 itself before giving up
    if mode == "busy_forever" or seen < 2:
        if mode == "busy_midway":
            emit({"type": "item.started", "item": {"type": "command_execution", "command": "rg -n x ."}})
        emit({"type": "error", "message": "exceeded retry limit, last status: 429 Too Many Requests"})
        emit({"type": "turn.failed", "error": {"message": "exceeded retry limit, last status: 429 Too Many Requests"}})
        sys.exit(1)
if mode == "busy_then_auth":
    emit({"type": "error", "message": "stream error: 429 Too Many Requests; retrying 1/5"})
    emit({"type": "turn.failed", "error": {"message": "unexpected status 401 Unauthorized"}})
    sys.exit(1)
if mode == "fail":
    emit({"type": "error", "message": "You've hit your usage limit"})
    emit({"type": "turn.failed", "error": {"message": "You've hit your usage limit"}})
    sys.exit(1)
emit({"type": "item.completed", "item": {"type": "agent_message", "text": "Looking for the code first."}})
emit({"type": "item.started", "item": {"type": "command_execution", "command": "/bin/zsh -lc 'rg -n x .'"}})
emit({"type": "item.completed", "item": {"type": "command_execution", "command": "/bin/zsh -lc 'rg -n x .'", "exit_code": 1}})
emit({"type": "item.completed", "item": {"type": "reasoning", "text": "**Checking** the flow"}})
if mode == "edit":
    emit({"type": "item.completed", "item": {"type": "file_change", "status": "failed",
          "changes": [{"path": os.path.abspath("static"), "kind": "add"}]}})
if mode in ("edit", "edit_front"):
    open("api.py", "a").write("# patched\n")
    changes = [{"path": os.path.abspath("api.py"), "kind": "update"}]
    if mode == "edit_front":
        os.makedirs("static", exist_ok=True)
        open("static/app.css", "w").write(".btn{}\n")
        changes.append({"path": os.path.abspath("static/app.css"), "kind": "add"})
    else:
        open("notes.txt", "w").write("shell edit or another writer\n")  # no file_change: not a Codex patch
    emit({"type": "item.completed", "item": {"type": "file_change", "changes": changes}})
if mode == "weird":
    print("[1, 2]", flush=True)
    print("not json at all", flush=True)
    emit({"type": "item.completed", "item": {"type": "file_change", "changes": "oops"}})
if mode == "empty":
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 1}})
    sys.exit(0)
emit({"type": "item.completed", "item": {"type": "agent_message", "text": "Answer: it's all in api.py:1."}})
emit({"type": "turn.completed", "usage": {"input_tokens": 12000, "cached_input_tokens": 8000, "output_tokens": 900}})
open(answer, "w").write("Answer: it's all in api.py:1.")
"""

MODEL_ENV = ("CODEX_AGENT_MODEL", "CODEX_AGENT_EFFORT", "CODEX_AGENT_HEAVY_MODEL", "CODEX_AGENT_HEAVY_EFFORT")
ALL_EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra"]


def catalog(*drop: str) -> str:
    """`codex debug models` output, best model first; `drop` leaves models out."""
    models = [
        ("gpt-6.1-sol", "list", ALL_EFFORTS),
        ("gpt-6-astra", "list", ALL_EFFORTS[:5]),
        ("gpt-6.1-terra", "list", ALL_EFFORTS),
        ("gpt-6-sol", "list", ALL_EFFORTS),
        ("gpt-6-luna", "list", ALL_EFFORTS[:3]),
        ("gpt-hidden", "hide", ALL_EFFORTS),
    ]
    return json.dumps({"models": [
        {"slug": slug, "visibility": visibility, "priority": index, "base_instructions": "…",
         "supported_reasoning_levels": [{"effort": effort, "description": "…"} for effort in efforts]}
        for index, (slug, visibility, efforts) in enumerate(models, 1) if slug not in drop
    ]})  # fmt: skip


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    clean = {k: v for k, v in os.environ.items() if k not in MODEL_ENV and k != "CODEX_AGENT_YOLO"}
    return {
        **clean,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CODEX_AGENT_HOME": str(tmp_path / "home"),
        "FAKE_LOG": str(tmp_path / "calls.jsonl"),
        "FAKE_CHILD": str(tmp_path / "child.pid"),
        "FAKE_CATALOG": catalog(),
    }


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    (path / "api.py").write_text("def handler():\n    return 1\n")
    (path / "dirty.py").write_text("x = 1\n")
    git = ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    (path / "dirty.py").write_text("x = 2\n")  # dirty before the run: must not be attributed to Codex
    return path


def run(env: dict[str, str], *args: str, mode: str = "ok", timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env={**env, "FAKE_MODE": mode},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def calls(env: dict[str, str]) -> list[dict]:
    return [json.loads(line) for line in Path(env["FAKE_LOG"]).read_text().splitlines()]


def runs(env: dict[str, str]) -> list[dict]:
    root = Path(env["CODEX_AGENT_HOME"]) / "runs"
    return [json.loads((d / "meta.json").read_text()) for d in sorted(root.iterdir())]


def model_of(call: dict) -> str | None:
    argv = call["argv"]
    return argv[argv.index("-m") + 1] if "-m" in argv else None


def test_explore_runs_in_sandbox_and_is_isolated(env, repo):
    result = run(env, "-C", str(repo), "where is handler?")
    assert result.returncode == 0, result.stdout + result.stderr
    call = calls(env)[0]
    argv = call["argv"]
    assert argv[:3] == ["exec", "--ignore-user-config", "--ignore-rules"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert 'sandbox_mode="workspace-write"' in argv and "sandbox_workspace_write.network_access=true" in argv
    assert model_of(call) == "gpt-6.1-terra"
    assert 'model_reasoning_effort="xhigh"' in argv and not any("context_window" in a for a in argv)
    assert "multi_agent" in argv and "hooks" in argv and argv[-1] == "-"
    assert Path(call["cwd"]).resolve() == repo.resolve() and argv[argv.index("-C") + 1] == str(repo)
    assert "scouting subagent" in call["prompt"] and "Commands run in a sandbox" in call["prompt"]
    assert "Don't stop at the first plausible cause" in call["prompt"] and "Keep the code clean" not in call["prompt"]
    assert call["prompt"].rstrip().endswith("where is handler?")
    out = result.stdout
    assert "codex gpt-6.1-terra · xhigh · explore · sandbox" in out and "Codex patches" not in out
    assert "$ rg -n x ." in out and "✗ exit 1" in out
    assert "» Looking for the code first." in out and "… Checking the flow" in out
    assert "■ OK" in out and "12k in (8k cached) · 900 out" in out
    assert out.rstrip().endswith("Answer: it's all in api.py:1.")
    assert out.count("Answer: it's all in api.py:1.") == 1  # the final message isn't repeated as a "»" line
    meta = runs(env)[0]
    assert meta["status"] == "ok" and meta["thread"] == "thread-1" and meta["mode"] == "explore"
    assert meta["model"] == "gpt-6.1-terra" and meta["effort"] == "xhigh" and meta["yolo"] is False


@pytest.mark.parametrize("flag", ["--heavy", "--sol", "--backend"])
def test_hard_bug_and_backend_run_on_the_heavy_model(env, repo, flag):
    result = run(env, "-C", str(repo), flag, "x")
    assert result.returncode == 0, result.stdout + result.stderr
    call = calls(env)[0]
    assert model_of(call) == "gpt-6.1-sol" and 'model_reasoning_effort="high"' in call["argv"]
    assert "codex gpt-6.1-sol · high" in result.stdout and runs(env)[0]["model"] == "gpt-6.1-sol"


def test_default_models_come_from_env(env, repo):
    custom = {**env, "CODEX_AGENT_MODEL": "gpt-6-astra", "CODEX_AGENT_EFFORT": "low", "CODEX_AGENT_HEAVY_MODEL": "gpt-6-sol"}
    assert run(custom, "-C", str(repo), "x").returncode == 0
    assert run(custom, "-C", str(repo), "--heavy", "x").returncode == 0
    first, second = calls(env)
    assert model_of(first) == "gpt-6-astra" and 'model_reasoning_effort="low"' in first["argv"]
    assert model_of(second) == "gpt-6-sol" and 'model_reasoning_effort="high"' in second["argv"]


def test_default_model_the_account_lacks_gives_way_to_codex_default(env, repo):
    result = run({**env, "FAKE_CATALOG": catalog("gpt-6.1-terra")}, "-C", str(repo), "x")
    assert result.returncode == 0, result.stdout + result.stderr
    argv = calls(env)[0]["argv"]
    assert "-m" not in argv and not any("model_reasoning_effort" in a for a in argv)
    assert "codex default model · default effort" in result.stdout
    assert "note: gpt-6.1-terra isn't among this account's models, so Codex picks its default model" in result.stdout
    assert runs(env)[0]["model"] is None


@pytest.mark.parametrize(
    ("flags", "model", "effort"),
    [
        (["--model", "astra", "--effort", "max"], "gpt-6-astra", "max"),  # part of the name, the user's effort
        (["--model", "sol"], "gpt-6.1-sol", "xhigh"),  # the newest Sol, the mode's effort
        (["--model", "GPT 6 Sol", "--heavy"], "gpt-6-sol", "high"),
        (["--model", "gpt-hidden"], "gpt-hidden", "xhigh"),  # an exact slug works even when the picker hides it
        (["--effort", "low"], "gpt-6.1-terra", "low"),
    ],
)
def test_the_model_the_user_names(env, repo, flags, model, effort):
    result = run(env, "-C", str(repo), *flags, "x")
    assert result.returncode == 0, result.stdout + result.stderr
    call = calls(env)[0]
    assert model_of(call) == model and f'model_reasoning_effort="{effort}"' in call["argv"]
    assert f"codex {model} · {effort}" in result.stdout and "note:" not in result.stdout


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (["--model", "nope"], "--model: the account has no 'nope'; its models: gpt-6.1-sol, gpt-6-astra"),
        (["--model", "luna", "--effort", "max"], "--effort: gpt-6-luna supports low, medium, high, not 'max'"),
        (["--model", "hidden"], "the account has no 'hidden'"),  # parts of names only match listed models
    ],
)
def test_a_model_or_effort_the_account_lacks_is_an_error(env, repo, flags, message):
    result = run(env, "-C", str(repo), *flags, "x")
    assert result.returncode == 2 and message in result.stderr
    assert not Path(env["FAKE_LOG"]).exists()


def test_a_default_effort_the_model_lacks_gives_way_to_its_own(env, repo):
    result = run(env, "-C", str(repo), "--model", "luna", "x")
    assert result.returncode == 0, result.stdout + result.stderr
    call = calls(env)[0]
    assert model_of(call) == "gpt-6-luna" and not any("model_reasoning_effort" in a for a in call["argv"])
    assert "note: gpt-6-luna has no xhigh effort, so it runs on its default" in result.stdout


def test_without_a_catalog_names_go_to_codex_as_they_are(env, repo):
    plain = {k: v for k, v in env.items() if k != "FAKE_CATALOG"}
    assert run(plain, "-C", str(repo), "--model", "astra", "--effort", "ultra", "x").returncode == 0
    call = calls(env)[0]
    assert model_of(call) == "astra" and 'model_reasoning_effort="ultra"' in call["argv"]


def test_models_lists_the_account_models(env):
    result = run(env, "--models")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].startswith("gpt-6.1-sol  ") and lines[0].endswith("ultra  ← --heavy, --backend")
    assert any(line.startswith("gpt-6.1-terra") and line.endswith("← exploration") for line in lines)
    assert "gpt-6-luna     low, medium, high" in result.stdout and "gpt-hidden" not in result.stdout
    assert not Path(env["FAKE_LOG"]).exists()
    plain = {k: v for k, v in env.items() if k != "FAKE_CATALOG"}
    result = run(plain, "--models")
    assert result.returncode == 1 and "can't list models" in result.stdout


def test_yolo_drops_the_sandbox(env, repo):
    assert run(env, "-C", str(repo), "--yolo", "x").returncode == 0
    assert run({**env, "CODEX_AGENT_YOLO": "1"}, "-C", str(repo), "x").returncode == 0
    for call in calls(env):
        assert "--dangerously-bypass-approvals-and-sandbox" in call["argv"]
        assert not any("sandbox_mode" in a for a in call["argv"]) and "Access is full" in call["prompt"]
    assert runs(env)[0]["yolo"] is True


def test_old_cli_gets_only_the_flags_it_knows(env, repo):
    result = run({**env, "FAKE_HELP": "--model <MODEL>"}, "-C", str(repo), "x")
    assert result.returncode == 0, result.stdout + result.stderr
    argv = calls(env)[0]["argv"]
    assert "--ignore-user-config" not in argv and "--ignore-rules" not in argv and "--disable" not in argv
    assert "note: this Codex CLI has no --ignore-user-config, --ignore-rules" in result.stdout


def test_missing_codex_cli(env, repo, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = run({**env, "PATH": str(empty)}, "-C", str(repo), "x")
    assert result.returncode == 1 and "Codex CLI not found on PATH" in result.stderr


def test_backend_separates_codex_patches_from_other_changes(env, repo):
    result = run(env, "-C", str(repo), "--backend", "--name", "be", "build the api", mode="edit")
    assert result.returncode == 0, result.stdout + result.stderr
    prompt = calls(env)[0]["prompt"]
    assert "subagent that writes the code" in prompt and "Do only the backend" in prompt
    assert "Carry the change through" in prompt and "No defensive code for things that can't happen" in prompt
    assert "What's left: frontend, migrations" in prompt and "In UI code" not in prompt
    out = result.stdout
    assert "backend · sandbox" in out and "← api.py" in out
    assert "Codex patches: api.py\n" in out
    assert "also changed during the run" in out and out.rstrip().count("notes.txt") == 1
    assert "dirty.py" not in out  # was dirty before the run and didn't change
    assert "⚠" not in out and "✗ patch failed: static" in out  # a failed patch isn't an edit
    meta = runs(env)[0]
    assert meta["patched"] == ["api.py"] and meta["other_changes"] == ["notes.txt"] and "-be-" in meta["id"]


def test_backend_outside_git(env, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = run(env, "-C", str(plain), "--backend", "x", mode="edit")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Codex patches: api.py" in result.stdout and "not a git repo" in result.stdout


def test_backend_flags_frontend_patched_by_codex(env, repo):
    result = run(env, "-C", str(repo), "--backend", "build the api", mode="edit_front")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Codex patches: api.py, static/app.css" in result.stdout
    assert "⚠ Codex edited frontend files — revert if this is UI work: static/app.css" in result.stdout


def test_allow_frontend(env, repo):
    result = run(env, "-C", str(repo), "--backend", "--allow-frontend", "build it", mode="edit_front")
    assert result.returncode == 0, result.stdout + result.stderr
    prompt = calls(env)[0]["prompt"]
    assert "frontend included" in prompt and "Do only the backend" not in prompt
    assert "In UI code, reuse the project's components" in prompt and "What's left: migrations" in prompt
    assert "Codex patches: api.py, static/app.css" in result.stdout and "⚠" not in result.stdout


def test_backend_in_subdir_uses_repo_paths(env, repo):
    sub = repo / "svc"
    sub.mkdir()
    (sub / "api.py").write_text("x = 1\n")
    result = run(env, "-C", str(sub), "--backend", "x", mode="edit")
    assert result.returncode == 0, result.stdout + result.stderr
    # Codex patches and git status both count from the repo root: svc/api.py isn't in "also changed"
    meta = runs(env)[0]
    assert meta["patched"] == ["svc/api.py"] and meta["other_changes"] == ["svc/notes.txt"]


def test_malformed_events_do_not_break_the_run(env, repo):
    result = run(env, "-C", str(repo), "x", mode="weird")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not json at all" in result.stdout and "! could not parse event" in result.stdout
    assert "■ OK" in result.stdout


def test_explore_that_edits_is_flagged(env, repo):
    result = run(env, "-C", str(repo), "where is handler?", mode="edit")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Codex patches: api.py" in result.stdout
    assert "⚠ exploration edited files — review and revert: api.py" in result.stdout


def test_resume_keeps_dir_and_thread_and_sets_mode_per_call(env, repo):
    assert run(env, "-C", str(repo), "--name", "first", "question").returncode == 0
    first = runs(env)[0]["id"]
    result = run(env, "--resume", first[:-5], "--backend", "now build it")
    assert result.returncode == 0, result.stdout + result.stderr
    call = calls(env)[1]
    argv = call["argv"]
    assert argv[:3] == ["exec", "resume", "thread-1"] and "-C" not in argv
    assert 'sandbox_mode="workspace-write"' in argv and Path(call["cwd"]).resolve() == repo.resolve()
    assert "subagent that writes the code" in call["prompt"]
    assert call["prompt"].startswith("Continuation of the same task") and "## Follow-up" in call["prompt"]
    assert f"resumes {first}" in result.stdout
    assert model_of(call) == "gpt-6.1-sol"  # exploration ran on Terra, backend on top of it on Sol
    # found by thread id too; without --backend it's read-only again, the model is the parent's
    assert run(env, "--resume", "thread-1", "another question").returncode == 2  # thread-1 belongs to two runs
    second = next(m["id"] for m in runs(env) if m["parent"] == first)
    assert run(env, "--resume", second, "another question").returncode == 0
    assert "scouting subagent" in calls(env)[2]["prompt"]
    assert model_of(calls(env)[2]) == "gpt-6.1-sol"
    assert run(env, "--resume", first, "and one more").returncode == 0
    assert model_of(calls(env)[3]) == "gpt-6.1-terra"


def test_task_file_is_appended(env, repo, tmp_path):
    task = tmp_path / "task.md"
    task.write_text("Detailed spec")
    assert run(env, "-C", str(repo), "-f", str(task), "In short:").returncode == 0
    assert "## Task\n\nIn short:\n\nDetailed spec" in calls(env)[0]["prompt"]


def test_failure_is_reported(env, repo):
    result = run(env, "-C", str(repo), "x", mode="fail")
    assert result.returncode == 1
    assert "! You've hit your usage limit" in result.stdout and "■ ERROR" in result.stdout
    assert runs(env)[0]["status"] == "error"
    assert len(calls(env)) == 1  # a usage limit isn't a queue: no retries


def test_busy_slots_wait_and_start_fresh(env, repo):
    result = run({**env, "CODEX_AGENT_BUSY_PAUSE": "0.05,0.1"}, "-C", str(repo), "x", mode="busy")
    assert result.returncode == 0 and "■ OK" in result.stdout
    assert result.stdout.count("⏳ 429") == 2
    sent = calls(env)
    assert len(sent) == 3 and all("resume" not in c["argv"] for c in sent)


def test_busy_midway_resumes_the_same_thread(env, repo):
    result = run({**env, "CODEX_AGENT_BUSY_PAUSE": "0.05,0.1"}, "-C", str(repo), "x", mode="busy_midway")
    assert result.returncode == 0
    last = calls(env)[-1]
    assert last["argv"][:4] == ["exec", "resume", "thread-1", "--ignore-user-config"]
    assert "Continue the same task" in last["prompt"]


def test_busy_gives_up_after_wait_budget(env, repo):
    busy_env = {**env, "CODEX_AGENT_BUSY_PAUSE": "0.05,0.1"}
    result = run(busy_env, "-C", str(repo), "--busy-wait", "0.004", "x", mode="busy_forever")
    assert result.returncode == 1 and "giving up" in result.stdout and "■ ERROR" in result.stdout


def test_busy_wait_budget_counts_attempt_time(env, repo):
    busy_env = {**env, "CODEX_AGENT_BUSY_PAUSE": "0.05,0.1", "FAKE_BUSY_DELAY": "0.35"}
    result = run(busy_env, "-C", str(repo), "--busy-wait", "0.004", "x", mode="busy_forever")
    assert result.returncode == 1 and "giving up" in result.stdout
    assert len(calls(env)) == 1  # one attempt already ate the 0.24 s budget


def test_transient_429_does_not_mask_fatal_error(env, repo):
    result = run(env, "-C", str(repo), "x", mode="busy_then_auth")
    assert result.returncode == 1 and "⏳ 429" not in result.stdout
    assert len(calls(env)) == 1


def test_empty_answer(env, repo):
    result = run(env, "-C", str(repo), "x", mode="empty")
    assert result.returncode == 3 and "■ EMPTY" in result.stdout
    assert "last message (not a report):\nLooking for the code first." in result.stdout


def test_timeout_kills_codex_and_its_children(env, repo):
    started = time.time()
    result = run(env, "-C", str(repo), "--timeout", "0.03", "x", mode="hang")
    assert result.returncode == 124 and "■ TIMEOUT" in result.stdout
    assert time.time() - started < 20
    assert_dead(int(Path(env["FAKE_CHILD"]).read_text()))


def test_sigterm_stops_codex_and_its_children(env, repo):
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT), "-C", str(repo), "x"],
        env={**env, "FAKE_MODE": "hang"},
        stdout=subprocess.PIPE,
        text=True,
    )
    child_file = Path(env["FAKE_CHILD"])
    deadline = time.time() + 15
    while not child_file.exists() and time.time() < deadline:
        time.sleep(0.1)
    process.send_signal(signal.SIGTERM)
    out, _ = process.communicate(timeout=20)
    assert process.returncode == 130 and "■ STOPPED" in out
    assert_dead(int(child_file.read_text()))


def assert_dead(pid: int) -> None:
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    pytest.fail(f"child process {pid} survived the stop")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["x"], "-C /abs/project is required"),
        (["-C", "rel", "x"], "absolute path"),
        (["-C", "/nonexistent-dir-xyz", "x"], "no folder"),
        (["-C", "/tmp"], "a task is required"),
        (["--resume", "nope", "x"], "no runs yet"),
        (["-C", "/tmp", "-f", "/nonexistent-file-xyz"], "can't read"),
    ],
)
def test_usage_errors(env, args, message):
    result = run(env, *args)
    assert result.returncode == 2 and message in result.stderr
    assert not Path(env["FAKE_LOG"]).exists()


def test_dry_run_starts_nothing(env, repo):
    result = run(env, "-C", str(repo), "--dry-run", "x")
    assert result.returncode == 0 and result.stdout.startswith("codex exec --ignore-user-config")
    assert not Path(env["FAKE_LOG"]).exists() and not (Path(env["CODEX_AGENT_HOME"]) / "runs").exists()


def test_old_runs_are_pruned(env, repo):
    old = Path(env["CODEX_AGENT_HOME"]) / "runs" / "old"
    old.mkdir(parents=True)
    stamp = time.time() - 8 * 86400
    os.utime(old, (stamp, stamp))
    assert run(env, "-C", str(repo), "x").returncode == 0
    assert not old.exists()


def test_run_id_collision_takes_a_free_folder(env, repo, monkeypatch):
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SCRIPT.parent))
    import codex_agent

    for key in ("PATH", "FAKE_LOG", "FAKE_CHILD"):
        monkeypatch.setenv(key, env[key])
    monkeypatch.setenv("FAKE_MODE", "ok")
    monkeypatch.setattr(codex_agent, "RUNS", Path(env["CODEX_AGENT_HOME"]) / "runs")
    ids = iter(["taken", "taken", "fresh"])
    monkeypatch.setattr(codex_agent, "new_run_id", lambda _label: next(ids))
    (codex_agent.RUNS / "taken").mkdir(parents=True)
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        assert codex_agent.main(["-C", str(repo), "x"]) == 0
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    assert (codex_agent.RUNS / "fresh" / "answer.md").exists()
    assert not any((codex_agent.RUNS / "taken").iterdir())


def test_frontend_heuristic():
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SCRIPT.parent))
    from codex_agent import clean_command, looks_frontend

    assert looks_frontend("web/src/Chat.tsx") and looks_frontend("app/templates/mail.html")
    assert looks_frontend("frontend/src/api.ts") and not looks_frontend("server/api.py")
    assert not looks_frontend("bot/handlers/web_search.py")
    assert clean_command("/bin/zsh -lc \"python3 -c 'print(1)'\"") == "python3 -c 'print(1)'"
    assert clean_command("/bin/zsh -lc 'ls -la'") == "ls -la"
    assert clean_command("""/bin/zsh -lc 'rg -g '"'"'!.git'"'"' x'""") == "rg -g '!.git' x"
