# Telegram Agent Bridges

Lightweight Telegram bridges for local coding-agent CLIs.

This repository is intentionally narrow in scope:

- no backend
- no database
- no queue
- no web app
- just `Telegram -> local CLI -> local session`

It currently includes:

- `bridges/codex` for Codex CLI
- `bridges/claude` for Claude Code

## What it does

- Send a plain Telegram message to your local coding agent
- Anchor a Telegram chat to an existing local session
- Resume that session on the next message
- List recent local sessions
- Expose a small control surface: status, tail, stop, pwd, cd

The goal is not to build a full agent platform. The goal is to keep a running local agent reachable from mobile with the smallest possible amount of glue code.

## Why this exists

Most agent tooling assumes you are sitting in front of the terminal. These bridges make the local CLI usable from Telegram without changing the underlying agent workflow.

The design stays deliberately simple:

- provider-specific Python script
- `.env` config
- optional `systemd --user` service
- no abstraction layer beyond what is necessary

## Repository layout

```text
bridges/
  claude/
  codex/
systemd/
```

## Quick start

Example with Codex:

```bash
cd bridges/codex
cp .env.example .env
set -a
source .env
set +a
python3 bridge.py
```

Example with Claude:

```bash
cd bridges/claude
cp .env.example .env
set -a
source .env
set +a
python3 bridge.py
```

## Session model

Both bridges follow the same model:

- a Telegram chat can be anchored to one local session
- the next plain message resumes that session
- `/sessions` shows recent local sessions
- `/session use ...` switches the anchor
- `/session new` clears the anchor

Provider-specific session sources:

- Codex: `~/.codex/session_index.jsonl`
- Claude:
  - `~/.claude/sessions/*.json`
  - `~/.claude/projects/**/*.jsonl`

Claude titles follow the saved session metadata:

- latest `custom-title`
- fallback `ai-title`
- fallback `(none)`

## systemd

Example user services are included under `systemd/`.

```bash
mkdir -p ~/.config/systemd/user
cp systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now codex-telegram-bridge.service
systemctl --user enable --now claude-telegram-bridge.service
```

## Security

- Intended for private, self-hosted use
- Restrict `TELEGRAM_ALLOWED_CHAT_IDS` to your own account
- Review provider CLI permission flags before unattended use
