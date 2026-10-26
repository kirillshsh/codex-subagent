# codex-subagent

[Русская версия](README.ru.md)

A Claude Code skill that uses the [Codex CLI](https://github.com/openai/codex) as a subagent. Claude hands Codex the code exploration it would otherwise give Explore: finding where things live, tracing how they work, diagnosing why something fails, mapping what a change would touch. Codex's tokens stay out of Claude's context, and Claude gets back a report with `path:line` references. On request, Codex can also write backend code.

## What you get

- **Exploration by default.** Codex reads the code, runs tests and reproduces bugs, but doesn't edit the project.
- **Hard bugs** (`--heavy`) go to a stronger model.
- **Backend work** (`--backend`) only when you ask Claude to have Codex write it. Frontend stays with Claude unless you allow it (`--allow-frontend`).
- **Sandbox.** Codex writes only inside the project and `/tmp`. `--yolo` turns the sandbox off.
- **Parallel runs.** When all your account's parallel slots are busy (HTTP 429), the wrapper waits for one instead of failing.
- **Follow-ups.** `--resume` continues the same Codex conversation.
- **A diff check after every run.** The wrapper lists Codex's patches and anything else that changed in the tree, and warns when exploration edited files or Codex touched the frontend.

## Requirements

- [Claude Code](https://claude.com/claude-code)
- Codex CLI 0.164 or newer (older versions work, with less isolation), signed in: `npm install -g @openai/codex`, then `codex login`
- Python 3.8+
- macOS or Linux

## Install

As a plugin:

```bash
claude plugin marketplace add kirillshsh/codex-subagent
```

```bash
claude plugin install codex-subagent@codex-subagent
```

Or by hand: copy `plugins/codex-subagent/skills/codex` to `~/.claude/skills/codex`.

## Use

Just ask Claude: "let Codex find why the login test fails", or type `/codex <task>`. Claude starts the wrapper in the background, keeps working, and reads the report when Codex finishes.

To run it yourself:

```bash
python3 ~/.claude/skills/codex/codex_agent.py -C /abs/path/to/project "Where is the rate limit applied to API requests?"
```

## Settings

| Variable | Default | What it sets |
| --- | --- | --- |
| `CODEX_AGENT_MODEL` | `gpt-6.1-terra` | Exploration model |
| `CODEX_AGENT_EFFORT` | `xhigh` | Exploration reasoning effort |
| `CODEX_AGENT_HEAVY_MODEL` | `gpt-6.1-sol` | Model for `--heavy` and `--backend` |
| `CODEX_AGENT_HEAVY_EFFORT` | `high` | Effort for `--heavy` and `--backend` |
| `CODEX_AGENT_YOLO` | off | `1` removes the sandbox for every run |
| `CODEX_AGENT_HOME` | `~/.claude/codex-agent` | Where runs are kept (7 days) |

If your account doesn't have a model, the wrapper retries once on Codex's default model.

## Safety

Codex runs with `approval_policy="never"`: it doesn't stop to ask. In the default sandbox it can only write inside the project and `/tmp`, but it has network access. With `--yolo` it can run any command on your machine. Use that only on projects and machines you trust.

The wrapper ignores `~/.codex/config.toml`, so your MCP servers, plugins and hooks aren't loaded into these runs.

## Tests

```bash
python3 -m pytest -q tests
```

## License

MIT
