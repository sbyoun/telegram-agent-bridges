# Telegram Relay MCP

A single long-running MCP daemon bound to **one Telegram bot + chat**. Any
coding agent (Claude Code, Codex, Cursor, ...) connects over streamable HTTP and
uses it to keep a human in the loop **from the agent side**.

This is the inverse of the unified bridge in this repo:

- `tgbridge/` : **Telegram → CLI** (Telegram drives the agent; run via
  `run-tgbridge.sh` with an `instances/<provider>/.env`)
- `mcp/telegram` : **Agent → Telegram** (the agent reports/asks and continues)

It shares the same Telegram env conventions (`TELEGRAM_BOT_TOKEN`,
`TELEGRAM_ALLOWED_CHAT_IDS`) but is a standalone async module — it does not
import `tgbridge.core` (that core is synchronous; this server is asyncio). Run
it against a **separate bot** from the bridge so the two `getUpdates` pollers do
not collide.

Because this daemon owns the *only* Telegram long-poll loop, multiple agents can
share one channel without the classic "two `getUpdates` pollers" conflict.

## Tools

| Tool | Behaviour |
| --- | --- |
| `telegram_notify(message)` | Send a one-way update, return immediately. |
| `telegram_ask(question, timeout_seconds=600)` | Send a question, **block until a human replies**, return the reply text. |
| `telegram_check(since_message_id=0)` | Non-blocking drain of unmatched human messages. |

### Reply routing on a shared channel

When several agents share one channel, replies are matched in this order:

1. **Native reply** — the human uses Telegram's "reply" on the question message;
   matched exactly via `reply_to_message_id`.
2. **FIFO fallback** — if exactly one `ask` is pending, the next human message
   fulfills it.
3. Anything unmatched lands in the inbox for `telegram_check()`.

## Quick start

Requires **Python 3.10+** (the `mcp` package does not support 3.8/3.9).
`run.sh` auto-selects a `python3.1x` interpreter, or `uv` if present.

```bash
cd mcp/telegram
cp .env.example .env
vi .env            # set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS
./run.sh           # bootstraps ./.venv and serves on 127.0.0.1:8765
```

The MCP endpoint is `http://127.0.0.1:8765/mcp` (streamable HTTP).

## Connecting an agent

### Claude Code

```bash
claude mcp add --transport http telegram-relay http://127.0.0.1:8765/mcp
```

### Generic MCP client config

```json
{
  "mcpServers": {
    "telegram-relay": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

## Example agent usage

- "Run the migration, then `telegram_notify` me when it finishes."
- "Before deleting the prod table, `telegram_ask` me for confirmation and only
  proceed if I reply `yes`."
- Long unattended task: the agent `telegram_ask`s whenever it hits a decision it
  cannot make alone, and continues with the human's reply.

## systemd (optional)

```bash
mkdir -p ~/.config/systemd/user
cp ../../systemd/telegram-relay-mcp.service ~/.config/systemd/user/
# edit WorkingDirectory / ExecStart / ConditionPathExists if cloned elsewhere
systemctl --user daemon-reload
systemctl --user enable --now telegram-relay-mcp.service
```

## Security

- Intended for private, self-hosted use.
- Restrict `TELEGRAM_ALLOWED_CHAT_IDS` to your own account/channel.
- The MCP endpoint binds to `127.0.0.1` by default — do not expose it publicly
  without putting authentication in front of it.

### telegram_approve (inline buttons)

| Tool | Behaviour |
| --- | --- |
| `telegram_approve(question, options=["승인","거부"], timeout_seconds=600)` | Send an approval request with inline buttons; **block until a button is pressed or the message gets a text reply**. Self-contained per message (`callback_data` carries the request id), so many agents can have approvals in flight on one channel. On decision the message is edited in place (outcome stamped, keyboard removed). |
