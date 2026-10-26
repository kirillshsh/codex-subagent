#!/usr/bin/env python3
"""Codex CLI as a Claude subagent: exploration on the default model, hard bugs (--heavy) and backend work on request
on the heavy model.

    python3 codex_agent.py -C /abs/project "task"
    python3 codex_agent.py -C /abs/project --heavy "hard bug"
    python3 codex_agent.py -C /abs/project --backend -f /abs/task.md
    python3 codex_agent.py --resume <run> "follow-up"

Codex runs in a workspace-write sandbox (writes inside the project and /tmp, network on), or with full access under
--yolo, and without ~/.codex/config.toml: no MCP, notify, plugins or account connectors. The mode is a role in the
prompt: exploration doesn't edit the project, backend edits only the backend. Prints one line per step, then the
summary, the changes in the tree and the full report; everything is kept in ~/.claude/codex-agent/runs/<run>/.

Models: CODEX_AGENT_MODEL / CODEX_AGENT_EFFORT (exploration), CODEX_AGENT_HEAVY_MODEL / CODEX_AGENT_HEAVY_EFFORT
(--heavy and --backend). A model the account doesn't have falls back to Codex's default model.

Exit codes: 0 — report ready, 1 — Codex failed or isn't installed, 2 — bad arguments, 3 — finished without a report,
124 — timeout, 130 — interrupted.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

# Exploration runs on Terra xhigh: close to Sol, but several times faster; on max it isn't better and wanders outside
# exploration. Sol is for hard bugs (--heavy) and backend work. A continuation (--resume) keeps the parent's model
# unless a flag asks for the heavy one.
DEFAULT_MODEL = (os.environ.get("CODEX_AGENT_MODEL") or "gpt-6.1-terra", os.environ.get("CODEX_AGENT_EFFORT") or "xhigh")
HEAVY_MODEL = (
    os.environ.get("CODEX_AGENT_HEAVY_MODEL") or "gpt-6.1-sol", os.environ.get("CODEX_AGENT_HEAVY_EFFORT") or "high"
)  # fmt: skip
RUNS = Path(os.environ.get("CODEX_AGENT_HOME") or Path.home() / ".claude" / "codex-agent") / "runs"
KEEP_DAYS = 7
IDLE_NOTE = 60.0  # seconds of silence before a "… thinking" line, so live output doesn't look stuck
TASK_FILE_LIMIT = 400_000
# OpenAI answers 429 when all of the account's parallel slots are taken. Such a run doesn't fail but waits for a slot,
# so any number of workers can be queued: the extra ones start by themselves when others finish.
BUSY = re.compile(r"\b429\b|too many requests", re.IGNORECASE)
BUSY_WAIT_MIN = 240.0  # minutes to wait for a slot before giving up
BUSY_PAUSE = (15.0, 90.0)  # first and longest pause between attempts, seconds
CONTINUE_AFTER_BUSY = "Continue the same task from where you stopped."
# The account has no such model (or no such effort for it): one more attempt on Codex's defaults.
MISSING_MODEL = re.compile(
    r"model\b.{0,120}?\b(?:not supported|not found|does not exist|not available|unavailable|do not have access)"
    r"|unknown model|invalid model|model_not_found|unsupported value.{0,120}?reasoning",
    re.IGNORECASE | re.DOTALL,
)

# A bare agent without account connectors, browser, plugins or hooks.
DISABLED_FEATURES = (
    "apps", "plugins", "remote_plugin", "browser_use", "browser_use_external", "computer_use", "image_generation",
    "multi_agent", "goals", "hooks", "tool_suggest", "in_app_browser", "skill_search", "sleep_tool",
)  # fmt: skip
# Older Codex CLIs lack some of these flags, and an unknown flag fails the run, so only known ones are passed.
ISOLATION_FLAGS = ("--ignore-user-config", "--ignore-rules")
SANDBOX = (
    'sandbox_mode="workspace-write"', "sandbox_workspace_write.network_access=true", 'approval_policy="never"'
)  # fmt: skip

FRONT_EXT = {".tsx", ".jsx", ".vue", ".svelte", ".astro", ".css", ".scss", ".sass", ".less", ".html", ".htm"}
FRONT_DIRS = {
    "frontend", "front", "client", "web", "webapp", "ui", "components", "pages", "static", "public", "styles",
    "assets", "templates", "miniapp", "mini-app", "mini_app",
}  # fmt: skip

SHELL_WRAPPER = re.compile(r"""^/bin/(?:ba|z)?sh -l?c (?:'(.*)'|"(.*)")$""", re.DOTALL)

COMMON_HEAD = """You were started by another agent (Claude, the orchestrator) in non-interactive mode: the orchestrator \
reads your report, not the user. Rules from AGENTS.md about the length and format of replies to the user don't apply \
here: make the report as complete as accuracy needs, without filler."""

SANDBOX_ACCESS = """Commands run in a sandbox: you can write inside the project and /tmp, the network is on; if the \
sandbox blocks something the task needs, say so in the report instead of working around it."""
YOLO_ACCESS = "Access is full, with no sandbox."

EXPLORE_RULES = """You are a scouting subagent. {head}

Rules for this run:
- This is exploration: don't edit project files, don't commit, don't create branches. {access} Otherwise you're free \
to run tests and scripts, reproduce the bug, start services and use the network; put temporary files in /tmp. Stop \
anything you started in the background before reporting.
- Work autonomously and don't ask questions. If something is ambiguous, pick a reasonable reading and note it in the \
report.
- Scope: the project {dir} and the paths the task names explicitly. Don't explore the environment, processes, \
~/.codex, ~/.claude or unrelated folders unless the task names them.
- Search with rg and read the ranges you need, not whole files. Back every conclusion with code or a run, not a guess.

The report is your last message, sized to the question: a few lines for a narrow question, detail for a mechanism.
1. The direct answer to the task's question, as the first paragraph.
2. Facts with path:line references (paths from the project root) and short code quotes where they prove the point.
3. Separate what you verified (read the code, ran a command) from assumptions.
4. What you couldn't verify and why."""

BACKEND_ONLY = """Do only the backend for the task: server code, APIs, databases and migrations, background jobs, CLIs \
and scripts, their tests. Don't touch the frontend (layout, styles, components, client code, UI copy), even if the \
task isn't finished without it: describe in the report what the frontend needs, the orchestrator will do it."""
FULL_STACK = "Do the task, frontend included."

BACKEND_RULES = """You are a subagent that writes the code. {head}

Rules for this run:
- {scope}
- Edit code only inside {dir}. Don't commit, push, create branches or worktrees, or deploy: delivery rules from \
AGENTS.md (commit, push, release scripts, deploy) are off for this run, the orchestrator delivers after checking. \
{access} Install dependencies, start a database and services for tests. Stop anything you started in the background \
before reporting.
- Work autonomously and don't ask questions. If something is ambiguous, pick a reasonable option and note it in the \
report.
- Minimal diff in the style of the neighbouring code; comments only where the code isn't clear without them. Don't \
weaken or delete tests to get a green run.
- Check the change with the project's tests or linters that cover it.

The report is your last message:
1. What you did, in one paragraph.
2. Changed files: path — what changed.
3. How you checked and the result (command → outcome); what you didn't check and why.
4. What's left: {left}migrations, configs, risks."""

RESUME_HEAD = "Continuation of the same task. The rules for this turn follow and replace the previous ones, then the follow-up."


def rules(mode: str, workdir: Path, yolo: bool = False, frontend: bool = False) -> str:
    access = YOLO_ACCESS if yolo else SANDBOX_ACCESS
    if mode == "backend":
        scope, left = (FULL_STACK, "") if frontend else (BACKEND_ONLY, "frontend, ")
        return BACKEND_RULES.format(head=COMMON_HEAD, dir=workdir, access=access, scope=scope, left=left)
    return EXPLORE_RULES.format(head=COMMON_HEAD, dir=workdir, access=access)


def build_prompt(mode: str, workdir: Path, task: str, resumed: bool, yolo: bool = False, frontend: bool = False) -> str:
    body = rules(mode, workdir, yolo, frontend)
    if resumed:
        return f"{RESUME_HEAD}\n\n{body}\n\n## Follow-up\n\n{task}\n"
    return f"{body}\n\n## Task\n\n{task}\n"


def pick_model(heavy: bool, backend: bool, parent: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """(model, effort); (None, None) means Codex's own defaults."""
    if heavy or backend:
        return HEAVY_MODEL
    if parent:
        return parent.get("model"), parent.get("effort")
    return DEFAULT_MODEL


class SetupError(Exception):
    """Codex CLI is missing or broken: one line to stderr and exit code 1."""


def cli_support() -> tuple[list[str], bool]:
    """Isolation flags this Codex CLI knows and whether it has --disable."""
    try:
        out = subprocess.run(
            ["codex", "exec", "--help"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL
        ).stdout  # fmt: skip
    except FileNotFoundError:
        raise SetupError("Codex CLI not found on PATH: npm install -g @openai/codex, then codex login") from None
    except (OSError, subprocess.SubprocessError) as error:
        raise SetupError(f"codex exec --help failed: {error}") from None
    flags = [flag for flag in ISOLATION_FLAGS if re.search(rf"{flag}\b(?!-)", out)]
    return flags, bool(re.search(r"--disable[ =<]", out))


def codex_command(
    workdir: Path, answer: Path, thread: str | None, model: tuple[str | None, str | None], yolo: bool = False,
    support: tuple[list[str], bool] = (list(ISOLATION_FLAGS), True),
) -> list[str]:  # fmt: skip
    # `exec resume` doesn't know -C: the folder comes from the process cwd.
    flags, can_disable = support
    name, effort = model
    command = ["codex", "exec", *(["resume", thread] if thread else []), *flags]
    command += ["--skip-git-repo-check", *([] if thread else ["-C", str(workdir)])]
    if yolo:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        for item in SANDBOX:
            command += ["-c", item]
    if name:
        command += ["-m", name]
    if effort:
        command += ["-c", f'model_reasoning_effort="{effort}"']
    if can_disable:
        for feature in DISABLED_FEATURES:
            command += ["--disable", feature]
    return [*command, "--json", "-o", str(answer), "-"]


# Run folders ------------------------------------------------------------------------------------------------------


class UsageError(Exception):
    """Bad invocation: one line to stderr and exit code 2."""


def new_run_id(label: str | None) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", (label or "").lower()).strip("-")[:24]
    return "-".join(part for part in (time.strftime("%y%m%d-%H%M%S"), slug, secrets.token_hex(2)) if part)


def prune() -> None:
    if not RUNS.is_dir():
        return
    cutoff = time.time() - KEEP_DAYS * 86400
    for folder in RUNS.iterdir():
        try:
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            continue


def read_meta(folder: Path) -> dict[str, Any] | None:
    try:
        return json.loads((folder / "meta.json").read_text())
    except (OSError, ValueError):
        return None


def find_run(key: str) -> dict[str, Any]:
    """A previous run by id, a unique prefix of it, or Codex's thread id."""
    if not RUNS.is_dir():
        raise UsageError(f"--resume: no runs yet ({RUNS})")
    exact = RUNS / key
    if exact.is_dir() and (meta := read_meta(exact)):
        return meta
    hits = [
        m for f in sorted(RUNS.iterdir()) if (m := read_meta(f)) and (m["id"].startswith(key) or m.get("thread") == key)
    ]
    if not hits:
        raise UsageError(f"--resume: run {key!r} not found")
    if len({m["id"] for m in hits}) > 1:
        raise UsageError(f"--resume: {key!r} matches several runs: {', '.join(m['id'] for m in hits)}")
    return hits[0]


# Changes in the tree ----------------------------------------------------------------------------------------------


def git_state(workdir: Path) -> tuple[Path, dict[str, tuple[str, int, int]]] | None:
    """Repo root and git status paths (from the root) with status, mtime and size, so an edit to an already dirty file
    still shows."""
    try:
        out = subprocess.run(
            ["git", "-C", str(workdir), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout  # fmt: skip
        top = subprocess.run(
            ["git", "-C", str(workdir), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    state: dict[str, tuple[str, int, int]] = {}
    entries = out.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            index += 1  # the old name comes next
        try:
            stat = (Path(top) / path).stat()
            state[path] = (status, stat.st_mtime_ns, stat.st_size)
        except OSError:
            state[path] = (status, 0, -1)
    return Path(top), state


def looks_frontend(path: str) -> bool:
    parts = Path(path).parts
    return Path(path).suffix.lower() in FRONT_EXT or any(part.lower() in FRONT_DIRS for part in parts[:-1])


# Live output ------------------------------------------------------------------------------------------------------


def short(text: str, limit: int = 180) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clean_command(command: str) -> str:
    """`/bin/zsh -lc 'rg -n x'`, as Codex reports it, is shown as `rg -n x`."""
    match = SHELL_WRAPPER.match(command.strip())
    if not match:
        return command
    if match.group(1) is not None:
        return match.group(1).replace("'\\''", "'").replace("'\"'\"'", "'")
    return re.sub(r'\\([\\"$`])', r"\1", match.group(2))


def duration(seconds: float) -> str:
    minutes, sec = divmod(int(seconds), 60)
    return f"{minutes}m{sec:02d}s" if minutes else f"{sec}s"


def tokens(count: int) -> str:
    return f"{count / 1000:.0f}k" if count >= 1000 else str(count)


class Run:
    def __init__(self, base: Path, started: float):
        self.base = base  # what edit paths are relative to: the repo root or the run folder
        self.started = started
        self.thread: str | None = None
        self.usage: dict[str, Any] | None = None
        self.errors: list[str] = []
        self.failed = False
        self.fatal = ""  # the turn.failed message: the CLI retries early 429s itself, this one says why the turn ended
        self.edited: list[str] = []
        self.last_message = ""
        self.pending = ""  # an interim message: printed only if steps follow it, the final one is shown by the report
        self.worked = False  # Codex already did something: after a 429 continue its thread instead of starting over
        self.lock = threading.Lock()
        self.last_print = time.time()

    def reason(self) -> str:
        return self.fatal or (self.errors[-1] if self.errors else "")

    def busy(self, code: int) -> bool:
        """The attempt failed on a 429 (the account's parallel slots are taken), not on a usage limit or its own error."""
        text = self.reason()
        return (code != 0 or self.failed) and bool(BUSY.search(text)) and "usage limit" not in text.lower()

    def missing_model(self, code: int, stderr: str) -> bool:
        """The attempt failed before any work because the account has no such model or effort."""
        failed = code != 0 or self.failed
        return failed and not self.worked and bool(MISSING_MODEL.search(f"{self.reason()}\n{stderr}"))

    def retry(self) -> None:
        self.failed = False
        self.fatal = ""
        self.errors = []
        self.pending = ""

    def say(self, line: str) -> None:
        with self.lock:
            print(line, flush=True)
            self.last_print = time.time()

    def flush_pending(self) -> None:
        if self.pending:
            self.say(f"» {short(self.pending, 240)}")
            self.pending = ""

    def relative(self, path: str) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.base.resolve()))
        except (ValueError, OSError):
            return path

    def event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        item = event.get("item") or {}
        item_type = item.get("type")
        if kind == "item.started" or (kind == "item.completed" and item_type != "error"):
            self.worked = True
        if kind == "thread.started":
            self.thread = event.get("thread_id")
        elif kind == "item.started" and item_type == "command_execution":
            self.flush_pending()
            command = clean_command(str(item.get("command") or "")).strip().replace("\n", " ⏎ ")
            self.say(f"$ {short(command)}")
        elif kind == "item.completed" and item_type == "command_execution":
            code = item.get("exit_code")
            if code not in (0, None):
                self.say(f"  ✗ exit {code}")
        elif kind == "item.completed" and item_type == "agent_message":
            self.flush_pending()
            self.pending = self.last_message = str(item.get("text") or "")
        elif kind == "item.completed" and item_type == "reasoning":
            self.flush_pending()
            text = re.sub(r"\*\*", "", str(item.get("text") or ""))
            if text.strip():
                self.say(f"… {short(text, 200)}")
        elif kind == "item.completed" and item_type == "file_change":
            self.flush_pending()
            paths = [self.relative(str(c.get("path"))) for c in item.get("changes") or [] if c.get("path")]
            if item.get("status") == "failed":  # the attempt ended but the patch didn't apply; git_state sees a partial one
                self.say(f"  ✗ patch failed: {', '.join(paths)}")
            else:
                self.edited += paths
                self.say(f"← {', '.join(paths)}")
        elif kind == "item.completed" and item_type == "web_search":
            self.flush_pending()
            self.say(f"✱ web: {short(str(item.get('query') or ''))}")
        elif kind == "item.completed" and item_type == "mcp_tool_call":
            self.flush_pending()
            self.say(f"· {item.get('server')}.{item.get('tool')}")
        elif kind == "item.completed" and item_type == "error":
            message = str(item.get("message") or "")
            if "unrecognized configuration" not in message:
                self.say(f"! {short(message, 300)}")
        elif kind == "turn.completed":
            self.usage = event.get("usage")
        elif kind in {"turn.failed", "error"}:
            error = event.get("error")
            message = str((error.get("message") if isinstance(error, dict) else error) or event.get("message") or "")
            if kind == "turn.failed":
                self.failed, self.fatal = True, message
            self.errors.append(message)
            self.say(f"! {short(message, 300)}")


# Main -------------------------------------------------------------------------------------------------------------


def busy_pauses() -> tuple[float, float]:
    """First and longest pause while waiting for a slot; tests shorten them with CODEX_AGENT_BUSY_PAUSE="0.1,0.2"."""
    raw = os.environ.get("CODEX_AGENT_BUSY_PAUSE")
    if not raw:
        return BUSY_PAUSE
    first, longest = (float(part) for part in raw.split(","))
    return first, longest


def execute(
    command: list[str], prompt: str, workdir: Path, run: Run, state: dict[str, Any], events: Any, stderr: Any,
    timeout_min: float | None,
) -> int:  # fmt: skip
    """One `codex exec` attempt: events go to events.jsonl and live output, the result is the process exit code."""
    process = subprocess.Popen(
        command, cwd=workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
        text=True, encoding="utf-8", errors="replace", start_new_session=True,
    )  # fmt: skip
    state["process"] = process
    timer = None
    if timeout_min is not None:  # without --timeout it runs as long as it needs
        timer = threading.Timer(timeout_min * 60, lambda: (state.__setitem__("timed_out", True), kill_tree(process.pid)))
        timer.daemon = True
        timer.start()
    try:
        if state["stopped"]:  # a signal arrived while the process was starting
            kill_tree(process.pid)
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except BrokenPipeError:
            pass
        for line in process.stdout:
            events.write(line)
            events.flush()
            try:
                event = json.loads(line)
            except ValueError:
                if line.strip():
                    run.say(short(line, 300))
                continue
            try:
                run.event(event)
            except Exception as error:  # noqa: BLE001 — a malformed event must not kill the run
                run.say(f"! could not parse event: {error!r}")
        return process.wait()
    finally:
        if timer is not None:
            timer.cancel()
        state["process"] = None
        if process.poll() is None:  # the wrapper is failing: Codex in its own session must not be left orphaned
            kill_tree(process.pid)


def process_groups(root: int) -> set[int]:
    """Codex's process group and those of all its descendants: Codex commands run in their own groups and outlive a
    killpg on Codex."""
    groups = {root}
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return groups
    children: dict[int, list[tuple[int, int]]] = {}
    for line in out.splitlines():
        fields = line.split()
        if len(fields) == 3 and all(field.isdigit() for field in fields):
            pid, ppid, pgid = map(int, fields)
            children.setdefault(ppid, []).append((pid, pgid))
    stack, seen = [root], {root}
    while stack:
        for pid, pgid in children.get(stack.pop(), []):
            if pid not in seen:
                seen.add(pid)
                groups.add(pgid)
                stack.append(pid)
    return groups


def signal_groups(groups: set[int], sig: int) -> set[int]:
    """Signals the groups; returns the ones still alive."""
    alive = set()
    for group in groups:
        try:
            os.killpg(group, sig)
            alive.add(group)
        except (ProcessLookupError, PermissionError):
            continue
    return alive


def kill_tree(pid: int) -> None:
    """SIGTERM to Codex's group and its commands' groups, SIGKILL to whatever is left after 3 seconds."""
    groups = signal_groups(process_groups(pid), signal.SIGTERM)
    for _ in range(30):
        if not groups:
            return
        time.sleep(0.1)
        groups = signal_groups(groups, 0)
    signal_groups(groups, signal.SIGKILL)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex CLI as a Claude subagent: exploration, hard bugs and backend.")
    parser.add_argument("prompt", nargs="?", help="the task; a long one goes in a file via -f")
    parser.add_argument("-C", "--cd", dest="workdir", help="project root, absolute path")
    parser.add_argument("-f", "--file", help="file with the task (appended after prompt)")
    parser.add_argument("--backend", action="store_true", help="the 'writes backend code' role instead of exploration")
    parser.add_argument("--allow-frontend", action="store_true", help="with --backend: frontend may be edited too")
    parser.add_argument("--heavy", "--sol", dest="heavy", action="store_true", help="hard bug: the heavy model")
    parser.add_argument(
        "--yolo", action="store_true", default=os.environ.get("CODEX_AGENT_YOLO") == "1",
        help="no sandbox and no approvals (default: CODEX_AGENT_YOLO=1)",
    )  # fmt: skip
    parser.add_argument("--resume", metavar="RUN", help="continue a run: its id, a prefix of it, or the thread id")
    parser.add_argument("--name", help="label inside the run id")
    parser.add_argument("--timeout", type=float, help="minutes before stopping (default: no limit)")
    parser.add_argument(
        "--busy-wait", type=float, default=BUSY_WAIT_MIN,
        help=f"minutes to wait for a free slot on 429 (default {BUSY_WAIT_MIN:g})",
    )  # fmt: skip
    parser.add_argument("--dry-run", action="store_true", help="print the command and prompt, don't start Codex")
    args = parser.parse_args(argv)
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.busy_wait < 0:
        parser.error("--busy-wait can't be negative")
    if not args.prompt and not args.file:
        parser.error("a task is required: text or -f file")
    return args


def model_label(model: tuple[str | None, str | None]) -> str:
    return f"{model[0] or 'default model'} · {model[1] or 'default effort'}"


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    parent = find_run(args.resume) if args.resume else None
    if parent and not parent.get("thread"):
        raise UsageError(f"--resume: run {parent['id']} has no thread id — Codex never started, nothing to continue")
    workdir_arg = args.workdir or (parent["dir"] if parent else None)
    if not workdir_arg:
        raise UsageError("-C /abs/project is required")
    if not os.path.isabs(workdir_arg):
        raise UsageError(f"-C: needs an absolute path, not {workdir_arg!r}")
    workdir = Path(workdir_arg)
    if not workdir.is_dir():
        raise UsageError(f"-C: no folder {workdir}")
    if parent and Path(parent["dir"]).resolve() != workdir.resolve():
        raise UsageError(f"--resume: run {parent['id']} ran in {parent['dir']}, not in {workdir}")

    task = args.prompt or ""
    if args.file:
        path = Path(args.file).expanduser()
        try:
            body = path.read_text()
        except OSError as error:
            raise UsageError(f"-f: can't read {path}: {error.strerror}") from None
        if len(body) > TASK_FILE_LIMIT:
            raise UsageError(f"-f: file is over {TASK_FILE_LIMIT // 1000}k characters — shorten the task")
        task = f"{task}\n\n{body}".strip()
    if not task.strip():
        raise UsageError("the task is empty")

    support = cli_support()
    mode = "backend" if args.backend else "explore"
    frontend = args.backend and args.allow_frontend
    model = pick_model(args.heavy, args.backend, parent)
    thread = parent["thread"] if parent else None
    prompt = build_prompt(mode, workdir, task, bool(parent), args.yolo, frontend)
    run_id = new_run_id(args.name)
    if not args.dry_run:
        prune()
        # Parallel runs in the same second: if the folder is taken, take a new id instead of writing into someone else's.
        for _ in range(20):
            try:
                (RUNS / run_id).mkdir(parents=True)
                break
            except FileExistsError:
                run_id = new_run_id(args.name)
        else:
            raise UsageError(f"couldn't get a free run folder in {RUNS}")
    folder = RUNS / run_id
    answer = folder / "answer.md"
    command = codex_command(workdir, answer, thread, model, args.yolo, support)
    if args.dry_run:
        print(shlex.join(command))
        print(f"\n--- prompt ({len(prompt)} characters) ---\n{prompt}")
        return 0

    (folder / "prompt.md").write_text(prompt)
    meta: dict[str, Any] = {
        "id": run_id, "dir": str(workdir), "mode": mode, "model": model[0], "effort": model[1], "yolo": args.yolo,
        "parent": parent["id"] if parent else None, "thread": thread, "status": "running", "started_at": time.time(),
    }  # fmt: skip
    write_meta(folder, meta)

    before = git_state(workdir)
    resumed = f" · resumes {parent['id']}" if parent else ""
    access = "full access (yolo)" if args.yolo else "sandbox"
    print(f"codex {model_label(model)} · {mode} · {access} · {workdir} · run {run_id}{resumed}", flush=True)
    missing = [flag for flag in ISOLATION_FLAGS if flag not in support[0]]
    if missing:
        print(f"note: this Codex CLI has no {', '.join(missing)}: your ~/.codex config applies — update Codex", flush=True)

    run = Run(before[0] if before else workdir, meta["started_at"])
    state: dict[str, Any] = {"timed_out": False, "stopped": False, "process": None}
    halt = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        state["stopped"] = True
        halt.set()
        if state["process"] is not None:
            kill_tree(state["process"].pid)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)
    quiet = threading.Event()

    def idle() -> None:
        while not quiet.wait(5):
            if state["process"] is not None and time.time() - run.last_print >= IDLE_NOTE:
                run.say(f"… thinking · {duration(time.time() - run.started)}")

    threading.Thread(target=idle, daemon=True).start()
    pause, max_pause = busy_pauses()
    waited, budget = 0.0, args.busy_wait * 60
    attempt_command, attempt_prompt = command, prompt
    try:
        with (folder / "events.jsonl").open("w") as events, (folder / "stderr.log").open("w") as stderr:
            while True:
                attempt_started = time.monotonic()
                try:
                    code = execute(attempt_command, attempt_prompt, workdir, run, state, events, stderr, args.timeout)
                except OSError as error:
                    meta.update(status="error", error=str(error), finished_at=time.time())
                    write_meta(folder, meta)
                    print(f"■ ERROR · codex failed to start: {error}", flush=True)
                    return 1
                if state["timed_out"] or state["stopped"]:
                    break
                stderr_text = (folder / "stderr.log").read_text(errors="replace")
                if (model[0] or model[1]) and run.missing_model(code, stderr_text):
                    run.say(f"! {model_label(model)} isn't available on this account — retrying on Codex's defaults")
                    model = (None, None)
                    meta.update(model=None, effort=None)
                    write_meta(folder, meta)
                    attempt_command = codex_command(workdir, answer, thread, model, args.yolo, support)
                    run.retry()
                    continue
                if not run.busy(code):
                    break
                waited += time.monotonic() - attempt_started  # the attempt itself waits too: the CLI retries 429s inside
                if waited >= budget:
                    run.say(f"! no free OpenAI slot after {duration(waited)} — giving up")
                    break
                sleep_for = min(pause * random.uniform(0.7, 1.3), budget - waited)
                run.say(f"⏳ 429: all Codex slots are busy — waiting {sleep_for:.0f}s · queued for {duration(waited)}")
                if halt.wait(sleep_for):
                    break
                waited += sleep_for
                pause = min(pause * 1.6, max_pause)
                if run.worked and run.thread:  # cut off mid-work: continue the same thread instead of starting over
                    attempt_command = codex_command(workdir, answer, run.thread, model, args.yolo, support)
                    attempt_prompt = build_prompt(mode, workdir, CONTINUE_AFTER_BUSY, True, args.yolo, frontend)
                run.retry()
    finally:
        quiet.set()

    elapsed = time.time() - run.started
    text = answer.read_text().strip() if answer.exists() else ""
    if state["timed_out"]:
        status, exit_code = f"TIMEOUT · exceeded {args.timeout:g} min", 124
    elif state["stopped"]:
        status, exit_code = "STOPPED · interrupted", 130
    elif code != 0 or run.failed:
        status, exit_code = f"ERROR · codex exit {code}", 1
    elif not text:
        status, exit_code = "EMPTY · finished without a report, rerun", 3
    else:
        status, exit_code = "OK", 0

    usage = run.usage or {}
    spent = ""
    if usage:
        cached = int(usage.get("cached_input_tokens") or 0)
        spent = f" · {tokens(int(usage.get('input_tokens') or 0))} in ({tokens(cached)} cached) · {tokens(int(usage.get('output_tokens') or 0))} out"

    # Codex's edits are only its patches. Anything else that changed in the tree during the run isn't attributed to it:
    # it may be its shell commands, or the orchestrator doing the frontend in parallel, or another session.
    patched = sorted(set(run.edited))
    other: list[str] = []
    after = git_state(workdir)
    if before is not None and after is not None:
        old, new = before[1], after[1]
        moved = {p for p, v in new.items() if old.get(p) != v} | {p for p in old if p not in new}
        other = sorted(moved - set(patched))

    meta.update(
        status=status.split(" ·")[0].lower(), exit_code=exit_code, codex_exit=code, thread=run.thread or thread,
        finished_at=time.time(), elapsed=round(elapsed, 1), usage=usage or None, patched=patched, other_changes=other,
        errors=run.errors[-5:], answer=str(answer) if text else None,
    )  # fmt: skip
    write_meta(folder, meta)

    print(
        f"\n■ {status} · {duration(elapsed)}{spent} · run {run_id} · thread {run.thread or thread or '—'}", flush=True
    )
    if exit_code in (1, 3) and not run.errors:
        tail = (folder / "stderr.log").read_text(errors="replace").strip().splitlines()[-5:]
        for line in tail:
            print(f"stderr: {short(line, 300)}")
    # The tree is diffed after exploration too: an edit there breaks the role, it isn't expected.
    if mode == "backend" or patched or other:
        print(f"Codex patches: {', '.join(patched) if patched else 'none'}")
    if other:
        print(f"also changed during the run (not a Codex patch: its shell commands, you, or another session): {', '.join(other)}")
    elif mode == "backend" and before is None:
        print("not a git repo: changes made by shell commands aren't visible, check them against the report")
    if mode == "explore" and patched:
        print(f"⚠ exploration edited files — review and revert: {', '.join(patched)}")
    front = [path for path in patched if looks_frontend(path)] if not frontend else []
    if front:
        print(f"⚠ Codex edited frontend files — revert if this is UI work: {', '.join(front)}")
    if text:
        print(f"report: {answer}\n\n{text}", flush=True)
    elif run.last_message:
        print(f"\nlast message (not a report):\n{run.last_message}", flush=True)
    return exit_code


def write_meta(folder: Path, meta: dict[str, Any]) -> None:
    temp = folder / "meta.json.tmp"
    temp.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    temp.replace(folder / "meta.json")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except UsageError as error:
        print(f"codex_agent: {error}", file=sys.stderr)
        sys.exit(2)
    except SetupError as error:
        print(f"codex_agent: {error}", file=sys.stderr)
        sys.exit(1)
