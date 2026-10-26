---
name: codex
description: Codex CLI as a Claude subagent — the default code scout instead of Explore (GPT-6.1 Terra xhigh; hard bugs — --heavy, GPT-6.1 Sol high; or any model and effort the user names). Backend work only when the user explicitly asks Codex to write it; frontend only with --allow-frontend. Use for any code exploration you would hand to Explore or another subagent, when the user types /codex, or asks to hand exploration, search, diagnostics or (explicitly) backend work to Codex ("let Codex look", «кодекс», «пусть кодекс посмотрит»). Not for code review.
---

# Codex as a subagent

You orchestrate, Codex scouts: broad search, reading lots of code, tracing data flow, finding the cause of a bug, mapping the places to change. Its tokens stay out of your context; you get a report. Decisions, edits and the answer to the user stay with you.

- Hand Codex any exploration you would give Explore or another subagent. Do small things (one grep, a known file) yourself.
- `/codex <task>` — do that task through Codex.

## Modes

- **Explore** (default): where things are, how they work, why something fails (it may reproduce the bug, run tests, start services), what a change would touch, a fix plan with path:line. It doesn't edit project files.
- **Backend** (`--backend`): only when the user explicitly asks Codex to write it. Server code, APIs, databases and migrations, background jobs, CLIs and scripts, their tests. The frontend stays with you; `--allow-frontend` only when the user explicitly wants Codex on the UI too.

## Where Codex is weak

Plan around these. The wrapper already counters them in every run, so don't repeat its rules in the task.

- **UI and UX taste.** Its layouts, styles, copy and interactions come out generic or off. Keep frontend work with you; Codex may read frontend code while exploring. When the user does want Codex on the UI, pass `--allow-frontend` and review the result as you would a junior's UI.
- **Little initiative.** It answers the literal question and stops: the first plausible cause, a sub-question instead of the real one, "if you want, I can…" instead of doing it. The wrapper tells it to go all the way, but it still delivers what you ask for, so ask for the whole thing: "find the cause and the smallest fix with path:line", not "where is X called".
- **Overengineering and junk safety nets.** It adds layers, helpers and options nobody needs, try/except around everything, `or default` fallbacks, checks for states that can't happen, compatibility branches. The wrapper forbids this; your spec adds the task's own limits. After `--backend`, read the diff for exactly these and cut them before delivering.

## Models

- Explore: `gpt-6.1-terra`, effort `xhigh` (`CODEX_AGENT_MODEL`, `CODEX_AGENT_EFFORT`).
- `--heavy` and `--backend`: `gpt-6.1-sol`, effort `high` (`CODEX_AGENT_HEAVY_MODEL`, `CODEX_AGENT_HEAVY_EFFORT`).
- `--heavy` is for a hard bug: Terra's report didn't explain the cause or reports disagree; a race between client and server; money, ledgers, payments; a change across several surfaces. Start a new `--heavy` worker with the same task and a list of what Terra already checked, not `--resume`, so it doesn't lean on Terra's conclusions.
- When the user names a model or an effort ("astra on max", "luna", "sol xhigh"), pass it: `--model astra --effort max`. Part of the name is enough: `astra` is `gpt-6-astra`, `sol` the newest Sol. `--models` lists the account's models and the efforts each supports. A model or effort the account lacks is an error with the list, never a silent switch; without `--effort` the run keeps its mode's effort.
- A default model the account lacks gives way to Codex's own, with a `note:` line.
- `--resume` keeps the parent's model and effort; `--heavy`, `--backend`, `--model` or `--effort` change them for the continuation.

## Running

`codex_agent.py` sits next to this file, in the skill's base directory shown when the skill loads.

```bash
python3 <skill-dir>/codex_agent.py -C /abs/project "<task>"                             # explore
python3 <skill-dir>/codex_agent.py -C /abs/project --heavy "<task>"                     # hard bug
python3 <skill-dir>/codex_agent.py -C /abs/project --model astra --effort max "<task>"  # the model the user named
python3 <skill-dir>/codex_agent.py -C /abs/project --backend -f /abs/task.md            # backend from a spec file
python3 <skill-dir>/codex_agent.py --resume <run> "<follow-up>"                         # continue with context
python3 <skill-dir>/codex_agent.py --models                                             # the account's models
```

- Run it with background Bash (`run_in_background: true`), without pipes. It prints a line per step (`$` command, `←` edit, `»` message, `… thinking` during long silence), and you're notified when it ends; meanwhile do your own part (say, the frontend of a mixed task) or end the turn with one line. Run it from the main agent: a subagent has nobody to notify it after its turn ends.
- At the end: `■ OK · 5m49s · 462k in (391k cached) · 13k out · run <id> · thread <uuid>`, the report path and the report. If the output got cut, read `answer.md` at the `report:` path.
- The wrapper sets the model, effort, sandbox and the rules for Codex: autonomous, no questions, no commits, go all the way, the simplest code without defensive fallbacks, a path:line report. It skips `~/.codex/config.toml` (no MCP, plugins, hooks or account connectors); Codex keeps shell, file edits, network and web search. It still reads `AGENTS.md` files; the wrapper turns off their delivery rules (commit, push, deploy).
- Sandbox: by default Codex writes only inside the project and `/tmp`, network on. `--yolo` (or `CODEX_AGENT_YOLO=1`) removes the sandbox — only when the user says so, for example when the sandbox blocks the project's tests.
- Timing: explore — a narrow question under a minute, a mechanism 1–3 minutes; heavy — 5–15 minutes; a small backend task with tests 2–5. No timeout by default; `--timeout <min>` sets one.

## Writing the task

Codex doesn't see the conversation, so the task stands alone. One task per worker.

- The goal and why it matters, symptoms verbatim (what the user sees, the error text, the steps), absolute paths to what matters.
- The real question and what you want back, not one step of your own plan.
- For backend work, a spec: files, behavior, acceptance criteria, the command that proves it works, and the limits — what stays as is and what not to touch.

## Parallel runs and follow-ups

- Many parallel workers are fine. When all of the account's parallel slots are taken (429), the wrapper waits for one (`⏳ 429…`, up to 4 h, `--busy-wait <min>`) and starts by itself; a run cut off mid-work continues the same thread. Send independent questions, parts of a big exploration (a module per worker) and different angles on one question (from the symptom, from the data, from git history) as separate workers in one message; reports that disagree are a finding too.
- Never two backend workers in one tree. In a tree other sessions work in, run backend work in its own worktree.
- `--resume <run>` (run id, a prefix of it, or the thread id) continues the same Codex thread in the same folder: a follow-up on the report, or "now build the backend for what you found" — `--resume <run> --backend`, if the user asked for that. Each call sets its mode: without `--backend` it's exploration again. New topic — new worker.

## After a run

`⚠` lines (exploration edited files, Codex edited frontend files) mean revert those edits; `also changed during the run` may be your own or another session's edits, so check `git diff` before touching them. Commit and push yourself.

## Failures

Exit codes: 0 — report ready; 1 — Codex failed or isn't installed (see the `!` and `stderr:` lines: usage limit, auth, network; a 429 lands here only if no slot freed up within `--busy-wait`); 2 — bad arguments, or a model or effort the account lacks; 3 — finished without a report, rerun once; 124 — `--timeout` hit; 130 — stopped. On a usage limit or a second failure in a row, tell the user in one line and continue with an Explore subagent. Each run's prompt, events and report live in `~/.claude/codex-agent/runs/<run>/` (`CODEX_AGENT_HOME` moves it) and are deleted after 7 days.
