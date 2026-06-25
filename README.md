# Telegram Agent Bridges

Lightweight Telegram glue for local coding-agent CLIs.

This repository is intentionally narrow in scope:

- no backend
- no database
- no queue
- no web app
- just `Telegram <-> local CLI <-> local session`

It currently includes:

- **`tgbridge/`** — a unified bridge (`Telegram -> local CLI`). One shared core
  plus thin provider adapters for **Claude Code**, **Codex**, and **Gemini**.
  Run one instance per provider/bot.
- **`mcp/telegram/`** — an MCP server for the **inverse** direction
  (`agent -> Telegram`): any coding agent reports/asks over a fixed Telegram
  channel and continues with the human's reply. See
  [mcp/telegram/README.md](mcp/telegram/README.md).

## What the bridge does

- Send a plain Telegram message to your local coding agent
- Anchor a Telegram chat to an existing local session
- Resume that session on the next message
- List recent local sessions (`/sessions`, with page buttons)
- Expose a small control surface: status, tail, stop, pwd, cd

The goal is not to build a full agent platform. The goal is to keep a running
local agent reachable from mobile with the smallest possible amount of glue.

## Design

The bridge stays deliberately simple:

- one shared core (`tgbridge/core.py`) for all Telegram I/O, command dispatch,
  state, pagination, and task monitoring
- per-provider adapters under `tgbridge/providers/` — everything that differs
  between Claude / Codex / Gemini lives behind one interface
- one `.env` per instance under `instances/<name>/`, selected by
  `BRIDGE_PROVIDER`
- a single shared repo-root `.venv` created by `run-tgbridge.sh`
- optional `systemd --user` template (`tgbridge@<instance>.service`)

## Repository layout

```text
tgbridge/
  core.py            # shared Telegram core
  providers/         # base, claude, codex, gemini adapters
instances/
  claude/  codex/  gemini/   # one .env per bot/provider
mcp/
  telegram/          # agent -> Telegram MCP server
systemd/
run-tgbridge.sh      # run one instance in the foreground
tgbridgectl.sh       # manage instances via systemd --user
```

## Quick start

```bash
cp instances/claude/.env.example instances/claude/.env
vi instances/claude/.env        # set BRIDGE_PROVIDER, token, allowed chat ids
./run-tgbridge.sh claude        # creates the shared ./.venv on first run
```

Swap `claude` for `codex` or `gemini` to run another provider. Each instance is
a separate bot; run as many as you like side by side.

## Session model

Every provider follows the same model:

- a Telegram chat can be anchored to one local session
- the next plain message resumes that session
- `/sessions` shows recent local sessions with page buttons; `/sessions 2` opens
  a page directly
- `/session use ...` switches the anchor
- `/session new` clears the anchor

Automated/headless sessions can be hidden from `/sessions` via
`<PREFIX>_EXCLUDE_CWDS` (e.g. `CLAUDE_EXCLUDE_CWDS`, `CODEX_EXCLUDE_CWDS`), a
comma-separated list of cwd prefixes. It **defaults to `/home/ubuntu/loop-engine`**,
so on hosts where loop-engine (or any noisy headless driver) lives elsewhere you
must point it at the real path, e.g.
`CLAUDE_EXCLUDE_CWDS=/ext_hdd/workspace/you/loop-engine`. Set it to empty to
disable filtering. Excluded sessions stay on disk and remain resumable.

Provider-specific session sources:

- Codex: `~/.codex/session_index.jsonl` (current rollout store)
- Claude:
  - `~/.claude/sessions/*.json`
  - `~/.claude/projects/**/*.jsonl`
- Gemini: `--session-id` is known at launch and anchored immediately

## MCP: agent -> Telegram

`mcp/telegram/` is the inverse direction. A single long-running MCP daemon owns
one Telegram bot and exposes three tools to any connected agent:

- `telegram_notify(message)` — one-way update
- `telegram_ask(question, timeout)` — ask and block until a human replies
- `telegram_check(since_id)` — non-blocking drain of new messages

It shares the same `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_CHAT_IDS`
conventions but is a standalone async module. Run it against a **separate bot**
from the bridge so the two `getUpdates` pollers do not collide. See
[mcp/telegram/README.md](mcp/telegram/README.md).

## systemd

The bridge ships a templated user unit. `tgbridgectl.sh` wraps the common
operations:

```bash
./tgbridgectl.sh install              # install tgbridge@.service + enable linger
./tgbridgectl.sh start   claude       # start+enable tgbridge@claude
./tgbridgectl.sh status               # all instances
./tgbridgectl.sh logs    claude 200   # tail logs
```

The template assumes the repo lives at `~/telegram-agent-bridges`; edit
`systemd/tgbridge@.service` if you clone it elsewhere. The MCP server has its own
unit at `systemd/telegram-relay-mcp.service`.

If you previously ran a bridge in `screen`, stop it before enabling systemd so
Telegram does not see two `getUpdates` pollers for the same bot.

## Troubleshooting

- **`/sessions` buttons do nothing when tapped** — inline-button taps arrive as
  `callback_query` updates, which Telegram only delivers if `allowed_updates` is
  sent to `getUpdates` as a JSON-encoded array. Passing a Python list to
  `requests` serializes it as repeated keys
  (`allowed_updates=message&allowed_updates=callback_query`), which Telegram
  ignores — so messages work but button taps are silently dropped. Fixed by
  JSON-encoding the value; make sure you are running a build that includes it.
- **`/sessions` is flooded with headless/loop sessions** — set
  `<PREFIX>_EXCLUDE_CWDS` (see [Session model](#session-model)); the default only
  matches `/home/ubuntu/loop-engine`.

## Security

- Intended for private, self-hosted use
- Restrict `TELEGRAM_ALLOWED_CHAT_IDS` to your own account
- Review provider CLI permission flags before unattended use
