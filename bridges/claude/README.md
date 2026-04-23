# Claude Bridge

Lightweight Telegram bridge for Claude Code.

Use it when you want Telegram access to local Claude sessions while keeping Claude's own session files and titles as the source of truth.

## Commands

- `./run-bridge.sh` creates `./.venv` on first run and installs `requirements.txt`.
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
