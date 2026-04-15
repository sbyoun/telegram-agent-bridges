# Codex Bridge

Lightweight Telegram bridge for Codex CLI.

Use it when you want to keep a local Codex session reachable from Telegram without adding any extra service layer.

## Commands

- `/start`
- `/help`
- `/sessions`
- `/session current`
- `/session use <number|session_id>`
- `/session new`
- `/run <prompt>`
- `/status`
- `/tail`
- `/stop`
- `/pwd`
- `/cd <path>`

With `TELEGRAM_PLAIN_TEXT_AS_RUN=1`, plain text messages are treated as task input.
