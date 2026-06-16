#!/usr/bin/env python3
"""Unified Telegram <-> CLI bridge core.

All Telegram I/O, command dispatch, state, pagination and task monitoring live
here and are identical for every provider. Provider-specific behavior (command
building, output parsing, session listing) lives in tgbridge.providers.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from .providers.base import Command, Provider, SessionInfo

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LEN = 4000
SESSION_PAGE_SIZE = 8


class TelegramAPIError(RuntimeError):
    """Telegram request failed without exposing the bot token in logs."""


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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


@dataclass
class BridgeConfig:
    bot_token: str
    allowed_chat_ids: set[str]
    cli_bin: str
    workdir: str = "/home/ubuntu"
    model: str | None = None
    extra_args: list[str] = field(default_factory=list)
    poll_timeout: int = 3
    plain_text_as_run: bool = False
    state_dir: Path = Path("./state")

    @classmethod
    def load(cls, provider: Provider) -> "BridgeConfig":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        allowed = {
            item.strip()
            for item in os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
            if item.strip()
        }
        if not token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is required")
        if not allowed:
            raise SystemExit("TELEGRAM_ALLOWED_CHAT_IDS is required")
        pre = provider.env_prefix
        state_dir = Path(os.getenv("BRIDGE_STATE_DIR", "./state")).expanduser().resolve()
        state_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            bot_token=token,
            allowed_chat_ids=allowed,
            cli_bin=os.getenv(f"{pre}_BIN", provider.default_bin).strip() or provider.default_bin,
            workdir=os.getenv(f"{pre}_WORKDIR", "/home/ubuntu").strip() or "/home/ubuntu",
            model=os.getenv(f"{pre}_MODEL", "").strip() or None,
            extra_args=shlex.split(os.getenv(f"{pre}_EXTRA_ARGS", "").strip()),
            poll_timeout=int(os.getenv("TELEGRAM_POLL_TIMEOUT", "3")),
            plain_text_as_run=env_flag("TELEGRAM_PLAIN_TEXT_AS_RUN", False),
            state_dir=state_dir,
        )


class TelegramClient:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.base_url = f"{API_BASE}/bot{config.bot_token}"
        self.session = requests.Session()

    def _raise_for_status(self, action: str, response: requests.Response) -> None:
        if response.ok:
            return
        description = response.text.strip()
        try:
            payload = response.json()
            description = str(payload.get("description") or payload)
        except ValueError:
            pass
        raise TelegramAPIError(f"{action} failed: HTTP {response.status_code}: {description}")

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": self.config.poll_timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            params["offset"] = offset
        response = self.session.get(
            f"{self.base_url}/getUpdates",
            params=params,
            timeout=(10, self.config.poll_timeout + 20),
        )
        self._raise_for_status("getUpdates", response)
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {payload}")
        return payload.get("result", [])

    def send_message(self, chat_id: str, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        chunks = chunk_text(text)
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if reply_markup is not None and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            response = self.session.post(f"{self.base_url}/sendMessage", json=payload, timeout=20)
            self._raise_for_status("sendMessage", response)

    def edit_message_text(self, chat_id: str, message_id: int, text: str,
                          reply_markup: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response = self.session.post(f"{self.base_url}/editMessageText", json=payload, timeout=20)
        self._raise_for_status("editMessageText", response)

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        response = self.session.post(f"{self.base_url}/answerCallbackQuery", json=payload, timeout=20)
        self._raise_for_status("answerCallbackQuery", response)

    def set_my_commands(self, commands: list[dict[str, str]], scope: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"commands": commands}
        if scope is not None:
            payload["scope"] = scope
        response = self.session.post(f"{self.base_url}/setMyCommands", json=payload, timeout=20)
        self._raise_for_status("setMyCommands", response)

    def delete_my_commands(self, scope: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {}
        if scope is not None:
            payload["scope"] = scope
        response = self.session.post(f"{self.base_url}/deleteMyCommands", json=payload, timeout=20)
        self._raise_for_status("deleteMyCommands", response)


class StateStore:
    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.offset_file = state_dir / "offset.json"
        self.bridge_file = state_dir / "bridge.json"

    def load_offset(self) -> int | None:
        if not self.offset_file.exists():
            return None
        data = json.loads(self.offset_file.read_text())
        return int(data.get("offset")) if data.get("offset") is not None else None

    def save_offset(self, offset: int) -> None:
        self.offset_file.write_text(json.dumps({"offset": offset}, indent=2))

    def save_bridge_state(self, data: dict[str, Any]) -> None:
        self.bridge_file.write_text(json.dumps(data, indent=2, ensure_ascii=True))

    def load_bridge_state(self) -> dict[str, Any]:
        if not self.bridge_file.exists():
            return {}
        try:
            return json.loads(self.bridge_file.read_text())
        except json.JSONDecodeError:
            return {}


@dataclass
class TaskState:
    prompt: str
    chat_id: str
    workdir: str
    started_at: float
    command: list[str]
    process: subprocess.Popen
    session_id: str | None = None
    session_mode: str = "new"
    assistant_messages: list[str] = field(default_factory=list)
    tail: deque = field(default_factory=lambda: deque(maxlen=120))
    result_received: bool = False
    result_sent: bool = False
    done: bool = False
    returncode: int | None = None
    last_error: str | None = None

    def duration(self) -> int:
        return int(time.time() - self.started_at)


class Bridge:
    def __init__(self, config: BridgeConfig, provider: Provider):
        self.config = config
        self.provider = provider
        self.telegram = TelegramClient(config)
        self.store = StateStore(config.state_dir)
        self.current_workdir = config.workdir
        self.task_lock = threading.Lock()
        self.task: TaskState | None = None
        self.shutdown = False
        state = self.store.load_bridge_state()
        self.anchored_sessions: dict[str, str] = dict(state.get("anchored_sessions") or {})
        self.last_session_menu: dict[str, list[SessionInfo]] = {}

    @property
    def display(self) -> str:
        return self.provider.display

    # ---- telegram command registration ----
    def desired_commands(self) -> list[dict[str, str]]:
        d = self.display
        return [
            {"command": "start", "description": "Show chat id and setup status"},
            {"command": "help", "description": "Show available commands"},
            {"command": "new", "description": "Start a fresh session"},
            {"command": "sessions", "description": f"List recent {d} sessions"},
            {"command": "session", "description": "Manage the anchored session"},
            {"command": "status", "description": "Show current task state"},
            {"command": "tail", "description": f"Show recent {d} output"},
            {"command": "stop", "description": "Stop the running task"},
            {"command": "pwd", "description": "Show current workdir"},
            {"command": "cd", "description": "Change default workdir"},
            {"command": "run", "description": f"Run a {d} task"},
        ]

    def sync_telegram_commands(self) -> None:
        commands = self.desired_commands()
        try:
            self.telegram.set_my_commands(commands, {"type": "default"})
            self.telegram.set_my_commands(commands, {"type": "all_private_chats"})
        except TelegramAPIError as exc:
            print(f"telegram command sync warning: {exc}", flush=True)
        for chat_id in self.config.allowed_chat_ids:
            try:
                self.telegram.delete_my_commands({"type": "chat", "chat_id": int(chat_id)})
            except TelegramAPIError as exc:
                print(f"telegram chat command cleanup warning for {chat_id}: {exc}", flush=True)

    def bridge_snapshot(self) -> dict[str, Any]:
        task = self.task
        return {
            "provider": self.provider.name,
            "current_workdir": self.current_workdir,
            "anchored_sessions": self.anchored_sessions,
            "task": None if task is None else {
                "prompt": task.prompt,
                "chat_id": task.chat_id,
                "workdir": task.workdir,
                "started_at": task.started_at,
                "session_id": task.session_id,
                "session_mode": task.session_mode,
                "done": task.done,
                "returncode": task.returncode,
                "last_error": task.last_error,
                "tail_size": len(task.tail),
            },
        }

    def is_allowed(self, chat_id: str) -> bool:
        return chat_id in self.config.allowed_chat_ids

    def send(self, chat_id: str, text: str) -> None:
        self.telegram.send_message(chat_id, text)

    def help_text(self) -> str:
        d = self.display
        return (
            f"{d} Telegram bridge commands\n\n"
            f"/run <prompt> - run a {d} task\n"
            "/new [prompt] - start a fresh session (optionally run a prompt)\n"
            f"/sessions [page] - list recent {d} sessions (tap to switch)\n"
            "/session current - show anchored session\n"
            "/session use <n|id> - anchor this chat to a session\n"
            "/session new - clear anchor so next message creates a fresh session\n"
            "/status - show current task state\n"
            "/tail - show recent task output\n"
            "/stop - stop the running task\n"
            "/pwd - show current workdir\n"
            "/cd <path> - change default workdir\n"
            "/help - show this help"
        )

    def start_text(self, chat_id: str) -> str:
        allowlisted = "yes" if self.is_allowed(chat_id) else "no"
        return (
            f"{self.display} Telegram bridge\n\n"
            f"Your chat id: {chat_id}\n"
            f"Allowlisted: {allowlisted}\n\n"
            "Paste this into your .env if needed:\n"
            f"TELEGRAM_ALLOWED_CHAT_IDS={chat_id}\n\n"
            "After restarting the bridge, use /help to see available commands."
        )

    # ---- anchoring ----
    def current_anchor(self, chat_id: str) -> str | None:
        return self.anchored_sessions.get(chat_id)

    def set_anchor(self, chat_id: str, session_id: str | None) -> None:
        if session_id is None:
            self.anchored_sessions.pop(chat_id, None)
        else:
            self.anchored_sessions[chat_id] = session_id
        self.store.save_bridge_state(self.bridge_snapshot())

    def format_anchor_text(self, chat_id: str) -> str:
        anchor = self.current_anchor(chat_id)
        if not anchor:
            return f"No anchored session. Next message will start a new {self.display} session."
        return f"Anchored session: {anchor}"

    def format_timestamp(self, raw_ms: int) -> str:
        if not raw_ms:
            return "-"
        return datetime.fromtimestamp(raw_ms / 1000).strftime("%Y-%m-%d %H:%M")

    # ---- sessions listing / pagination ----
    def load_recent_sessions(self, limit: int | None = SESSION_PAGE_SIZE) -> list[SessionInfo]:
        # core guarantees most-recently-used first regardless of provider
        sessions = self.provider.list_sessions(limit=None)
        sessions.sort(key=lambda s: s.updated_ms, reverse=True)
        return sessions if limit is None else sessions[:limit]

    def parse_sessions_page(self, text: str) -> int | None:
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            return 1
        raw_page = parts[1].strip()
        if not raw_page.isdigit():
            return None
        page = int(raw_page)
        return page if page >= 1 else None

    def sessions_keyboard(self, sessions: list[SessionInfo], page: int, total_pages: int,
                          anchor: str | None) -> dict[str, Any] | None:
        rows: list[list[dict[str, Any]]] = []
        for idx, s in enumerate(sessions, start=1):
            label = s.name if (s.name and s.name != s.id) else s.id[:8]
            text = ("⭐ " if s.id == anchor else "") + f"{idx}. {label}"
            if len(text) > 40:
                text = text[:39] + "…"
            cb = f"use:{page}:{s.id}"
            # callback_data hard limit is 64 bytes; fall back to index if too long
            if len(cb.encode()) > 64:
                cb = f"useidx:{page}:{idx}"
            rows.append([{"text": text, "callback_data": cb}])
        nav = []
        if page > 1:
            nav.append({"text": "◀ Prev", "callback_data": f"sessions:{page - 1}"})
        if page < total_pages:
            nav.append({"text": "Next ▶", "callback_data": f"sessions:{page + 1}"})
        if nav:
            rows.append(nav)
        return {"inline_keyboard": rows} if rows else None

    def show_sessions(self, chat_id: str, page: int = 1, page_size: int = SESSION_PAGE_SIZE,
                      message_id: int | None = None) -> None:
        all_sessions = self.load_recent_sessions(limit=None)
        anchor = self.current_anchor(chat_id)
        if not all_sessions:
            self.send(chat_id, f"No {self.display} sessions were found.")
            return
        total = len(all_sessions)
        total_pages = ((total - 1) // page_size) + 1
        if page > total_pages:
            self.last_session_menu[chat_id] = []
            self.send(chat_id, f"No {self.display} sessions on page {page}. Last page is {total_pages}.")
            return
        start = (page - 1) * page_size
        sessions = all_sessions[start:start + page_size]
        self.last_session_menu[chat_id] = sessions
        lines = [f"Recent {self.display} sessions (page {page}/{total_pages}, {total} total)\n"]
        for idx, session in enumerate(sessions, start=1):
            marker = " [anchored]" if session.id == anchor else ""
            label = session.name if session.name and session.name != session.id else "(none)"
            lines.append(
                f"{idx}. {label}{marker}\n"
                f"   id: {session.id}\n"
                f"   cwd: {session.cwd or '-'}\n"
                f"   updated: {self.format_timestamp(session.updated_ms)}"
            )
        lines.append("\n아래 버튼을 눌러 세션을 바로 전환하세요. (⭐ = 현재 anchor)")
        text = "\n".join(lines)
        keyboard = self.sessions_keyboard(sessions, page, total_pages, anchor)
        if message_id is not None:
            self.telegram.edit_message_text(chat_id, message_id, text, reply_markup=keyboard)
            return
        self.telegram.send_message(chat_id, text, reply_markup=keyboard)

    def resolve_session_choice(self, chat_id: str, selector: str) -> SessionInfo | None:
        selector = selector.strip()
        sessions = self.last_session_menu.get(chat_id) or self.load_recent_sessions(limit=20)
        if selector.isdigit():
            index = int(selector) - 1
            if 0 <= index < len(sessions):
                return sessions[index]
            return None
        for session in sessions:
            if session.id == selector:
                return session
        # allow anchoring to an arbitrary id even if not in the listing
        return SessionInfo(id=selector) if selector else None

    def use_session(self, chat_id: str, selector: str) -> None:
        session = self.resolve_session_choice(chat_id, selector)
        if session is None:
            self.send(chat_id, "Session not found. Use /sessions first, then /session use <number> or pass a full session id.")
            return
        self.set_anchor(chat_id, session.id)
        self.send(
            chat_id,
            f"Anchored this chat to {self.display} session\n\n"
            f"{session.name or session.id}\n"
            f"id: {session.id}\n"
            f"updated: {self.format_timestamp(session.updated_ms)}",
        )

    # ---- message dispatch ----
    def handle_message(self, message: dict[str, Any]) -> None:
        chat_id = str(message["chat"]["id"])
        text = (message.get("text") or "").strip()
        if not text:
            return
        print(f"[{self.provider.name}] recv from {chat_id}: {text[:120]}", flush=True)
        if text == "/start":
            self.send(chat_id, self.start_text(chat_id))
            return
        if not self.is_allowed(chat_id):
            self.send(
                chat_id,
                "This chat is not allowlisted yet.\n\n"
                f"Your chat id is: {chat_id}\n"
                "Add it to TELEGRAM_ALLOWED_CHAT_IDS in the bridge .env, restart the bridge, then send /start again.",
            )
            return
        if not text.startswith("/") and self.config.plain_text_as_run:
            text = f"/run {text}"

        if text == "/help":
            self.send(chat_id, self.help_text()); return
        if text == "/sessions" or text.startswith("/sessions "):
            page = self.parse_sessions_page(text)
            if page is None:
                self.send(chat_id, "Usage: /sessions [page]"); return
            self.show_sessions(chat_id, page=page); return
        if text == "/session":
            self.send(chat_id, "Usage: /session current | /session use <n|id> | /session new"); return
        if text == "/session current":
            self.send(chat_id, self.format_anchor_text(chat_id)); return
        if text == "/session new" or text == "/new":
            self.set_anchor(chat_id, None)
            self.send(chat_id, f"🆕 새 세션 모드. 다음 메시지가 새 {self.display} 세션이 됩니다."); return
        if text.startswith("/new "):
            self.set_anchor(chat_id, None)
            self.start_task(chat_id, text[len("/new "):].strip()); return
        if text.startswith("/session use "):
            self.use_session(chat_id, text[len("/session use "):].strip()); return
        if text == "/status":
            self.send(chat_id, self.status_text(chat_id)); return
        if text == "/tail":
            self.send(chat_id, self.tail_text()); return
        if text == "/stop":
            self.stop_task(chat_id); return
        if text == "/pwd":
            self.send(chat_id, f"Current workdir: {self.current_workdir}"); return
        if text.startswith("/cd "):
            self.change_workdir(chat_id, text[4:].strip()); return
        if text.startswith("/run "):
            self.start_task(chat_id, text[5:].strip()); return
        if text == "/run":
            self.send(chat_id, "Usage: /run <prompt>"); return
        self.send(chat_id, "Unknown command. Use /help.")

    def handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        callback_id = str(callback_query.get("id") or "")
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        message_id = message.get("message_id")
        data = str(callback_query.get("data") or "")
        if not chat_id or not self.is_allowed(chat_id):
            if callback_id:
                self.telegram.answer_callback_query(callback_id)
            return

        toast: str | None = None
        if data.startswith("use:") or data.startswith("useidx:"):
            kind, _, rest = data.partition(":")
            page_str, _, sel = rest.partition(":")
            page = int(page_str) if page_str.isdigit() else 1
            sid: str | None = None
            if kind == "useidx":
                idx = int(sel) if sel.isdigit() else 0
                page_sessions = self.last_session_menu.get(chat_id) or []
                if 1 <= idx <= len(page_sessions):
                    sid = page_sessions[idx - 1].id
            else:
                sid = sel or None
            if sid:
                prev = self.current_anchor(chat_id)
                self.set_anchor(chat_id, sid)
                toast = f"✓ 세션 전환: {sid[:8]}"
                if sid != prev and isinstance(message_id, int):
                    self.show_sessions(chat_id, page=page, message_id=message_id)
            else:
                toast = "세션을 찾지 못했어요. /sessions 다시 열기"
        elif data.startswith("sessions:"):
            try:
                page = int(data.split(":", 1)[1])
            except ValueError:
                page = 0
            if page >= 1 and isinstance(message_id, int):
                self.show_sessions(chat_id, page=page, message_id=message_id)
        if callback_id:
            self.telegram.answer_callback_query(callback_id, toast)

    def change_workdir(self, chat_id: str, requested: str) -> None:
        path = Path(requested).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            self.send(chat_id, f"Not a directory: {path}"); return
        self.current_workdir = str(path)
        self.store.save_bridge_state(self.bridge_snapshot())
        self.send(chat_id, f"Default workdir updated to {self.current_workdir}")

    # ---- task run ----
    def start_task(self, chat_id: str, prompt: str) -> None:
        prompt = prompt.strip()
        if not prompt:
            self.send(chat_id, "Prompt is empty."); return
        with self.task_lock:
            if self.task and not self.task.done:
                self.send(chat_id, "A task is already running. Use /status, /tail, or /stop first."); return
            anchor = self.current_anchor(chat_id)
            cmd: Command = self.provider.build_command(self.config, prompt, self.current_workdir, anchor)
            process = subprocess.Popen(
                cmd.argv, cwd=self.current_workdir,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            session_id = anchor or cmd.session_hint
            self.task = TaskState(
                prompt=prompt, chat_id=chat_id, workdir=self.current_workdir,
                started_at=time.time(), command=cmd.argv, process=process,
                session_id=session_id, session_mode=cmd.mode,
            )
            if cmd.session_hint and not anchor:
                self.set_anchor(chat_id, cmd.session_hint)
            worker = threading.Thread(target=self._watch_task, args=(self.task,), daemon=True)
            worker.start()
        short_prompt = prompt if len(prompt) <= 240 else f"{prompt[:237]}..."
        session_line = f"\nSession: {anchor}" if anchor else "\nSession: new (will auto-anchor on success)"
        self.store.save_bridge_state(self.bridge_snapshot())
        self.send(chat_id, f"Started {self.display} task in {self.current_workdir}{session_line}\n\nPrompt: {short_prompt}")

    def _watch_task(self, task: TaskState) -> None:
        try:
            assert task.process.stdout is not None
            for raw_line in task.process.stdout:
                line = raw_line.rstrip("\n")
                if line:
                    task.tail.append(line)
                self._consume_line(task, line)
            task.returncode = task.process.wait()
            final = self.provider.finalize(task)
            if final is not None:
                if final.session_id:
                    task.session_id = final.session_id
                    self.set_anchor(task.chat_id, final.session_id)
                if final.assistant_text and (
                    not task.assistant_messages
                    or task.assistant_messages[-1] != final.assistant_text
                ):
                    task.assistant_messages.append(final.assistant_text)
            task.done = True
            if not task.result_sent:
                self.send(task.chat_id, self._finish_message(task))
                task.result_sent = True
        except Exception as exc:
            task.done = True
            task.last_error = str(exc)
            self.send(task.chat_id, f"Bridge error while running {self.display}: {exc}")
        finally:
            self.store.save_bridge_state(self.bridge_snapshot())

    def _consume_line(self, task: TaskState, line: str) -> None:
        ev = self.provider.consume_line(line)
        if ev is None:
            return
        if ev.session_id:
            task.session_id = ev.session_id
            self.set_anchor(task.chat_id, ev.session_id)
        if ev.assistant_text:
            if not task.assistant_messages or task.assistant_messages[-1] != ev.assistant_text:
                task.assistant_messages.append(ev.assistant_text)
        if ev.is_result:
            task.result_received = True
            if not task.result_sent:
                self.send(task.chat_id, self._result_message(task, ev))
                task.result_sent = True

    def _finish_message(self, task: TaskState) -> str:
        header = f"{self.display} task finished with exit code {task.returncode} in {task.duration()}s"
        body = "\n\n".join(task.assistant_messages[-3:]).strip()
        tail = "\n".join(list(task.tail)[-20:]).strip()
        if not body:
            body = "(No assistant message captured. Use /tail for raw output.)"
        message = f"{header}\n\n{body}"
        if task.returncode and tail:
            message += f"\n\nRecent output:\n{tail}"
        return message

    def _result_message(self, task: TaskState, ev) -> str:
        status = "error" if ev.is_error else (ev.result_subtype or "result")
        body = "\n\n".join(task.assistant_messages[-3:]).strip()
        if not body:
            body = "(No assistant message captured. Use /tail for raw output.)"
        return f"{self.display} task {status} in {task.duration()}s\n\n{body}"

    def status_text(self, chat_id: str) -> str:
        task = self.task
        if task is None:
            return (
                f"No task has been started yet.\n"
                f"Current workdir: {self.current_workdir}\n"
                f"{self.format_anchor_text(chat_id)}"
            )
        state = "running" if not task.done else "finished"
        session_line = f"\nSession: {task.session_id}" if task.session_id else ""
        return (
            f"Task state: {state}\n"
            f"Workdir: {task.workdir}\n"
            f"Session mode: {task.session_mode}\n"
            f"Anchored session: {self.current_anchor(chat_id) or '-'}\n"
            f"Duration: {task.duration()}s\n"
            f"Exit code: {task.returncode}\n"
            f"Prompt: {task.prompt[:280]}{session_line}"
        )

    def tail_text(self) -> str:
        task = self.task
        if task is None:
            return "No task output available yet."
        tail = "\n".join(task.tail).strip()
        if not tail:
            return "No output captured yet."
        return f"Recent output from {self.display}:\n\n{tail[-3500:]}"

    def stop_task(self, chat_id: str) -> None:
        with self.task_lock:
            task = self.task
            if task is None or task.done:
                self.send(chat_id, "No running task to stop."); return
            task.process.terminate()
            try:
                task.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                task.process.kill()
                task.process.wait(timeout=5)
            task.done = True
            task.returncode = task.process.returncode
            self.store.save_bridge_state(self.bridge_snapshot())
        self.send(chat_id, f"Stopped {self.display} task. Exit code: {task.returncode}")

    def run_forever(self) -> None:
        offset = self.store.load_offset()
        self.store.save_bridge_state(self.bridge_snapshot())
        while not self.shutdown:
            try:
                updates = self.telegram.get_updates(offset)
                for update in updates:
                    offset = int(update["update_id"]) + 1
                    self.store.save_offset(offset)
                    message = update.get("message")
                    if message:
                        self.handle_message(message)
                    callback_query = update.get("callback_query")
                    if callback_query:
                        self.handle_callback_query(callback_query)
            except requests.RequestException as exc:
                time.sleep(2); print(f"telegram request error: {exc}", flush=True)
            except TelegramAPIError as exc:
                time.sleep(2); print(f"telegram api error: {exc}", flush=True)
            except Exception as exc:
                time.sleep(2); print(f"bridge loop error: {exc}", flush=True)

    def request_shutdown(self, *_args: Any) -> None:
        self.shutdown = True


def main() -> None:
    from .providers.base import get_provider
    provider_name = os.getenv("BRIDGE_PROVIDER", "").strip()
    provider = get_provider(provider_name)
    config = BridgeConfig.load(provider)
    bridge = Bridge(config, provider)
    bridge.sync_telegram_commands()
    signal.signal(signal.SIGINT, bridge.request_shutdown)
    signal.signal(signal.SIGTERM, bridge.request_shutdown)
    print(f"tgbridge started: provider={provider.name} bin={config.cli_bin}", flush=True)
    bridge.run_forever()


if __name__ == "__main__":
    main()
