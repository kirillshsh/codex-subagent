---
name: codex
description: Codex CLI as a Claude subagent — the default code scout instead of Explore (GPT-6.1 Terra xhigh; hard bugs — --heavy, GPT-6.1 Sol high). Backend work only when the user explicitly asks Codex to do it; frontend only with --allow-frontend. Use for any code exploration you would hand to Explore or another subagent, when the user types /codex, or asks to hand exploration, search, diagnostics or (explicitly) backend work to Codex ("let Codex look", «кодекс», «пусть кодекс посмотрит»). Not for code review.
---

# Codex as a subagent

You orchestrate, Codex scouts: broad search, reading lots of code, tracing data flow, finding the cause of a bug, mapping the places to change. Its tokens stay out of your context; you get a report. Decisions, edits and the answer to the user stay with you.

- Hand Codex any exploration you would give Explore or another subagent. Do small things (one grep, a known file) yourself.
- `/codex <task>` — do that task through Codex.

## Modes

- **Explore** (default): where things are, how they work, why something fails (it may reproduce the bug, run tests, start services), what a change would touch, a fix plan with path:line. It doesn't edit project files.
- **Backend** (`--backend`): only when the user explicitly asks Codex to write it. Server code, APIs, databases and migrations, background jobs, CLIs and scripts, their tests. Codex leaves the frontend (layout, styles, components, client code, UI copy) to you unless the user asks for it and you pass `--allow-frontend`.

## Models

- Explore: `gpt-6.1-terra`, effort `xhigh`. Override with `CODEX_AGENT_MODEL` and `CODEX_AGENT_EFFORT`.
- `--heavy` and `--backend`: `gpt-6.1-sol`, effort `high`. Override with `CODEX_AGENT_HEAVY_MODEL` and `CODEX_AGENT_HEAVY_EFFORT`.
- If the account doesn't have a model, the wrapper retries once on Codex's default model and says so.
- `--heavy` is for a hard bug: Terra's report didn't explain the cause or reports disagree; a race between client and server; money, ledgers, payments; a change across several surfaces. Start a new `--heavy` worker with the same task and a list of what Terra already checked, not `--resume`, so it doesn't lean on Terra's conclusions.
- `--resume` keeps the parent's model; `--heavy` or `--backend` move the continuation to the heavy model.

## Running

`codex_agent.py` sits next to this file, in the skill's base directory shown when the skill loads.

```bash
python3 <skill-dir>/codex_agent.py -C /abs/project "<task>"              # explore
python3 <skill-dir>/codex_agent.py -C /abs/project --heavy "<task>"      # hard bug
python3 <skill-dir>/codex_agent.py -C /abs/project --backend "<task>"    # backend, on the user's request
python3 <skill-dir>/codex_agent.py -C /abs/project -f /abs/task.md       # long spec from a file
python3 <skill-dir>/codex_agent.py --resume <run> "<follow-up>"          # continue with context
```

- Run it with background Bash (`run_in_background: true`), without pipes. It prints a line per step (`$` command, `←` edit, `»` message, `… thinking` during long silence), and you're notified when it ends; meanwhile do your own part (say, the frontend of a mixed task) or end the turn with one line. Run it from the main agent: a subagent has nobody to notify it after its turn ends.
- At the end: `■ OK · 5m49s · 462k in (391k cached) · 13k out · run <id> · thread <uuid>`, the report path and the report. If the output got cut, read `answer.md` at the `report:` path.
- The wrapper sets the model, effort, sandbox and the rules for Codex (autonomous, no questions, no commits, a path:line report): don't repeat them in the prompt. It skips `~/.codex/config.toml` (no MCP, plugins, hooks or account connectors); Codex keeps shell, file edits, network and web search. It still reads `AGENTS.md` files; the wrapper turns off their delivery rules (commit, push, deploy).
- Sandbox: by default Codex writes only inside the project and `/tmp`, network on. `--yolo` (or `CODEX_AGENT_YOLO=1`) removes the sandbox — only when the user says so, for example when the sandbox blocks the project's tests.
- Timing: explore — a narrow question under a minute, a mechanism 1–3 minutes; heavy — 5–15 minutes; a small backend task with tests 2–5. No timeout by default; `--timeout <min>` sets one.

## Prompt

Codex doesn't see the conversation, so the prompt stands alone: goal and context, symptoms verbatim (what the user sees, the error text, the steps), absolute paths to what matters, what to find and what counts as done. One task per worker. For backend work, a spec: files, behavior, acceptance criteria, how to check.

## Parallel runs and follow-ups

- Many parallel workers are fine. When all of the account's parallel slots are taken (429), the wrapper waits for one (`⏳ 429…`, up to 4 h, `--busy-wait <min>`) and starts by itself; a run cut off mid-work continues the same thread. Send independent questions, parts of a big exploration (a module per worker) and different angles on one question (from the symptom, from the data, from git history) as separate workers in one message; reports that disagree are a finding too.
- Never two backend workers in one tree. In a tree other sessions work in, run backend work in its own worktree.
- `--resume <run>` (run id, a prefix of it, or the thread id) continues the same Codex thread in the same folder: a follow-up on the report, or "now build the backend for what you found" — `--resume <run> --backend`, if the user asked for that. Each call sets its mode: without `--backend` it's exploration again. New topic — new worker.

## Checking is on you

- After a run, `⚠` lines (exploration edited files, Codex edited frontend files) mean revert those edits; `also changed during the run` may be your own or another session's edits, so check `git diff` before touching them. Commit and push yourself.

## Failures

Exit codes: 0 — report ready; 1 — Codex failed or isn't installed (see the `!` and `stderr:` lines: usage limit, auth, network; a 429 lands here only if no slot freed up within `--busy-wait`); 2 — bad arguments; 3 — finished without a report, rerun once; 124 — `--timeout` hit; 130 — stopped. On a usage limit or a second failure in a row, tell the user in one line and continue with an Explore subagent. Each run's prompt, events and report live in `~/.claude/codex-agent/runs/<run>/` (`CODEX_AGENT_HOME` moves it) and are deleted after 7 days.
