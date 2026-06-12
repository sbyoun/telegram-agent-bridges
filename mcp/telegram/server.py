#!/usr/bin/env python3
"""Telegram relay MCP server.

A single long-running MCP daemon bound to one Telegram bot + chat. Any coding
agent (Claude Code, Codex, Cursor, ...) connects over streamable HTTP and uses
three tools to keep a human in the loop from the agent side:

- telegram_notify(message)            -> fire-and-forget update
- telegram_ask(question, timeout)     -> ask and block until a human replies
- telegram_check(since_id)            -> non-blocking drain of new human messages

Because this daemon owns the only Telegram long-poll loop, multiple agents can
share one channel without the classic "two getUpdates pollers" conflict.

Reply routing on a shared channel:
- The human can use Telegram's native "reply" on the question message; the relay
  matches it via reply_to_message_id (exact).
- Otherwise, if exactly one ask is pending, the next human message fulfills it.
- Anything unmatched lands in the inbox for telegram_check().
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LEN = 4000


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    bot_token: str
    allowed_chat_ids: set[str]
    target_chat_id: str
    poll_timeout: int = 25
    host: str = "127.0.0.1"
    port: int = 8765

    @classmethod
    def load(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        allowed = {
            item.strip()
            for item in os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
            if item.strip()
        }
        # Where outgoing messages go. Defaults to the single allowed chat id.
        target = os.getenv("TELEGRAM_TARGET_CHAT_ID", "").strip()
        if not token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is required")
        if not allowed:
            raise SystemExit("TELEGRAM_ALLOWED_CHAT_IDS is required")
        if not target:
            if len(allowed) == 1:
                target = next(iter(allowed))
            else:
                raise SystemExit(
                    "TELEGRAM_TARGET_CHAT_ID is required when multiple "
                    "allowed chat ids are configured"
                )
        return cls(
            bot_token=token,
            allowed_chat_ids=allowed,
            target_chat_id=target,
            poll_timeout=int(os.getenv("TELEGRAM_POLL_TIMEOUT", "25")),
            host=os.getenv("MCP_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=int(os.getenv("MCP_PORT", "8765")),
        )


def chunk_text(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


# --------------------------------------------------------------------------- #
# Incoming message + pending ask bookkeeping
# --------------------------------------------------------------------------- #
@dataclass
class Incoming:
    message_id: int
    chat_id: str
    text: str
    reply_to: int | None
    date: int


@dataclass
class PendingAsk:
    question_message_id: int
    future: "asyncio.Future[Incoming]"
    created: int = 0


@dataclass
class PendingApproval:
    """A button-based approval bound to exactly one Telegram message.

    Resolved by an inline-keyboard callback (chosen option string) or by a
    native text reply to the approval message (free-form text). Self-contained
    per message, so many agents can have approvals in flight on one channel.
    """

    message_id: int
    options: list[str]
    future: "asyncio.Future[str]"


@dataclass
class Relay:
    cfg: Config
    client: httpx.AsyncClient
    # ask_id -> PendingAsk
    pending: dict[int, PendingAsk] = field(default_factory=dict)
    # ordered ask_ids for FIFO fallback routing
    pending_order: deque[int] = field(default_factory=deque)
    # unmatched human messages, kept bounded
    inbox: deque[Incoming] = field(default_factory=lambda: deque(maxlen=500))
    # approval_id -> PendingApproval (inline-keyboard approvals)
    approvals: dict[int, PendingApproval] = field(default_factory=dict)
    offset: int | None = None
    _ask_seq: int = 0
    _appr_seq: int = 0

    @property
    def base_url(self) -> str:
        return f"{API_BASE}/bot{self.cfg.bot_token}"

    # ----- outgoing ------------------------------------------------------- #
    async def send_message(
        self,
        text: str,
        reply_to: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> int | None:
        """Send (chunked) and return the message_id of the first chunk.

        reply_markup (inline keyboard) is attached to the last chunk so the
        buttons always sit directly under the visible end of the message.
        """
        first_id: int | None = None
        chunks = chunk_text(text)
        for i, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": self.cfg.target_chat_id,
                "text": chunk,
            }
            if reply_to is not None and i == 0:
                payload["reply_to_message_id"] = reply_to
            if reply_markup is not None and i == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            resp = await self.client.post(
                f"{self.base_url}/sendMessage", json=payload, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram sendMessage failed: {data}")
            if first_id is None:
                first_id = int(data["result"]["message_id"])
        return first_id

    # ----- ask lifecycle -------------------------------------------------- #
    def register_ask(self, question_message_id: int) -> "asyncio.Future[Incoming]":
        self._ask_seq += 1
        ask_id = self._ask_seq
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Incoming] = loop.create_future()
        self.pending[ask_id] = PendingAsk(question_message_id, fut)
        self.pending_order.append(ask_id)
        fut.add_done_callback(lambda _f, aid=ask_id: self._drop_ask(aid))
        return fut

    def _drop_ask(self, ask_id: int) -> None:
        self.pending.pop(ask_id, None)
        try:
            self.pending_order.remove(ask_id)
        except ValueError:
            pass

    # ----- approval lifecycle --------------------------------------------- #
    async def finalize_approval_message(
        self, message_id: int, original_text: str, outcome: str
    ) -> None:
        """Edit the approval message in place: stamp outcome, drop keyboard."""
        try:
            resp = await self.client.post(
                f"{self.base_url}/editMessageText",
                json={
                    "chat_id": self.cfg.target_chat_id,
                    "message_id": message_id,
                    "text": f"{original_text}\n\n{outcome}",
                },
                timeout=30,
            )
            resp.raise_for_status()
        except Exception as exc:  # cosmetic only — never break resolution
            print(f"[approve] edit failed: {exc}", file=sys.stderr, flush=True)

    def _route(self, msg: Incoming) -> bool:
        """Try to deliver an incoming message to a pending ask. Return matched."""
        # 0. native reply to a pending approval message -> free-form answer
        if msg.reply_to is not None:
            for appr in list(self.approvals.values()):
                if appr.message_id == msg.reply_to:
                    if not appr.future.done():
                        appr.future.set_result(msg.text)
                    return True
        # 1. exact reply match
        if msg.reply_to is not None:
            for ask_id, ask in list(self.pending.items()):
                if ask.question_message_id == msg.reply_to:
                    if not ask.future.done():
                        ask.future.set_result(msg)
                    return True
        # 2. FIFO fallback when exactly one ask is pending
        if len(self.pending_order) == 1:
            ask_id = self.pending_order[0]
            ask = self.pending.get(ask_id)
            if ask and not ask.future.done():
                ask.future.set_result(msg)
                return True
        return False

    # ----- poll loop ------------------------------------------------------ #
    async def poll_forever(self) -> None:
        while True:
            try:
                params: dict[str, Any] = {"timeout": self.cfg.poll_timeout}
                if self.offset is not None:
                    params["offset"] = self.offset
                resp = await self.client.get(
                    f"{self.base_url}/getUpdates",
                    params=params,
                    timeout=self.cfg.poll_timeout + 10,
                )
                resp.raise_for_status()
                data = resp.json()
                if not data.get("ok"):
                    await asyncio.sleep(2)
                    continue
                for update in data.get("result", []):
                    self.offset = int(update["update_id"]) + 1
                    self._handle_update(update)
            except (httpx.HTTPError, asyncio.TimeoutError):
                await asyncio.sleep(2)
            except Exception as exc:  # keep the loop alive
                print(f"[poll] error: {exc}", file=sys.stderr, flush=True)
                await asyncio.sleep(2)

    def _handle_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if callback:
            asyncio.get_running_loop().create_task(self._handle_callback(callback))
            return
        message = update.get("message") or update.get("channel_post")
        if not message:
            return
        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id not in self.cfg.allowed_chat_ids:
            return
        text = message.get("text")
        if text is None:
            return
        reply_to = None
        if message.get("reply_to_message"):
            reply_to = int(message["reply_to_message"]["message_id"])
        msg = Incoming(
            message_id=int(message["message_id"]),
            chat_id=chat_id,
            text=text,
            reply_to=reply_to,
            date=int(message.get("date", 0)),
        )
        if not self._route(msg):
            self.inbox.append(msg)

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        """Resolve an inline-keyboard press: appr:<approval_id>:<option_index>."""
        cb_id = callback.get("id", "")
        try:
            message = callback.get("message") or {}
            chat_id = str(message.get("chat", {}).get("id", ""))
            data = callback.get("data", "") or ""
            ack_text = ""
            if chat_id in self.cfg.allowed_chat_ids and data.startswith("appr:"):
                try:
                    _, appr_str, idx_str = data.split(":", 2)
                    appr = self.approvals.get(int(appr_str))
                    idx = int(idx_str)
                except (ValueError, TypeError):
                    appr, idx = None, -1
                if appr and 0 <= idx < len(appr.options):
                    chosen = appr.options[idx]
                    if not appr.future.done():
                        appr.future.set_result(chosen)
                    ack_text = chosen
                    who = (callback.get("from") or {}).get("first_name", "")
                    await self.finalize_approval_message(
                        int(message.get("message_id", appr.message_id)),
                        message.get("text", ""),
                        f"☑️ 선택: {chosen}" + (f" — {who}" if who else ""),
                    )
            # always answer the callback so the client stops the spinner
            await self.client.post(
                f"{self.base_url}/answerCallbackQuery",
                json={"callback_query_id": cb_id, "text": ack_text},
                timeout=30,
            )
        except Exception as exc:  # keep the poll loop healthy
            print(f"[callback] error: {exc}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# MCP server wiring
# --------------------------------------------------------------------------- #
_relay: Relay | None = None


def relay() -> Relay:
    if _relay is None:
        raise RuntimeError("relay not initialised")
    return _relay


@asynccontextmanager
async def lifespan(_server: FastMCP):
    global _relay
    cfg = Config.load()
    async with httpx.AsyncClient() as client:
        _relay = Relay(cfg=cfg, client=client)
        poller = asyncio.create_task(_relay.poll_forever())
        print(
            f"[relay] polling chat(s) {sorted(cfg.allowed_chat_ids)} -> "
            f"target {cfg.target_chat_id}",
            file=sys.stderr,
            flush=True,
        )
        try:
            yield
        finally:
            poller.cancel()


mcp = FastMCP("telegram-relay", lifespan=lifespan)


@mcp.tool()
async def telegram_notify(message: str) -> str:
    """Send a one-way update to the Telegram channel and return immediately.

    Use for progress reports, completion notices, or anything that does not
    need a reply. For questions where you need the human's answer before
    continuing, use telegram_ask instead.
    """
    mid = await relay().send_message(message)
    return f"sent (message_id={mid})"


@mcp.tool()
async def telegram_ask(question: str, timeout_seconds: int = 600) -> str:
    """Send a question to the Telegram channel and block until a human replies.

    The human can reply either by using Telegram's native "reply" on the
    question (most reliable when several asks are in flight) or by simply
    sending the next message. Returns the human's reply text, or a timeout
    notice if no reply arrives within timeout_seconds.
    """
    r = relay()
    mid = await r.send_message(question)
    if mid is None:
        return "error: failed to send question"
    fut = r.register_ask(mid)
    try:
        reply = await asyncio.wait_for(fut, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        if not fut.done():
            fut.cancel()
        return f"timeout: no reply within {timeout_seconds}s (question_id={mid})"
    return reply.text


@mcp.tool()
async def telegram_approve(
    question: str,
    options: list[str] | None = None,
    timeout_seconds: int = 600,
) -> str:
    """Send an approval request with inline buttons and block until decided.

    Each request is self-contained in one Telegram message: the buttons carry
    this request's id, so many agents can have approvals pending on the same
    channel without ambiguity. The human either presses a button (returns that
    option string, e.g. "승인") or replies to the message with free-form text
    (returns that text — useful for "수정해서 ~로 해줘" style answers). On
    decision the message is edited in place to record the outcome and the
    keyboard is removed. Returns "timeout: ..." if nobody decides in time.

    options defaults to ["승인", "거부"]. Keep each option short (button label).
    """
    r = relay()
    opts = [o.strip() for o in (options or ["승인", "거부"]) if o and o.strip()]
    if not opts:
        return "error: options must contain at least one non-empty label"
    # Reserve the id first so callback_data can embed it.
    r._appr_seq += 1
    appr_id = r._appr_seq
    keyboard = {
        "inline_keyboard": [
            [{"text": opt, "callback_data": f"appr:{appr_id}:{i}"}]
            for i, opt in enumerate(opts)
        ]
    }
    mid = await r.send_message(question, reply_markup=keyboard)
    if mid is None:
        return "error: failed to send approval request"
    loop = asyncio.get_running_loop()
    fut: "asyncio.Future[str]" = loop.create_future()
    r.approvals[appr_id] = PendingApproval(mid, opts, fut)
    fut.add_done_callback(lambda _f, aid=appr_id: r.approvals.pop(aid, None))
    try:
        decision = await asyncio.wait_for(fut, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        if not fut.done():
            fut.cancel()
        await r.finalize_approval_message(
            mid, question, f"⌛ 시간 초과({timeout_seconds}s) — 미결정"
        )
        return f"timeout: no decision within {timeout_seconds}s (message_id={mid})"
    return decision


@mcp.tool()
async def telegram_check(since_message_id: int = 0) -> str:
    """Drain unmatched human messages without blocking.

    Returns messages with message_id greater than since_message_id, one per
    line as "<message_id>: <text>". Pass the highest id you have seen as
    since_message_id on the next call to avoid duplicates. Returns an empty
    string when there is nothing new.
    """
    r = relay()
    fresh = [m for m in r.inbox if m.message_id > since_message_id]
    if not fresh:
        return ""
    return "\n".join(f"{m.message_id}: {m.text}" for m in fresh)


def main() -> None:
    cfg = Config.load()  # validate early; fail fast with a clear message
    mcp.settings.host = cfg.host
    mcp.settings.port = cfg.port
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
