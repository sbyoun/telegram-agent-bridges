#!/usr/bin/env python3
"""Telegram bridge for Claude Code CLI.

Features:
- Poll Telegram without extra frameworks
- Run Claude tasks in print mode
- Anchor a Telegram chat to a Claude session id
- Show recent local Claude sessions and select one
- Support plain text messages as task input
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

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LEN = 4000


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
    claude_bin: str = "claude"
    workdir: str = "/home/ubuntu"
    model: str | None = None
    extra_args: list[str] = field(default_factory=list)
    poll_timeout: int = 3
    plain_text_as_run: bool = False
    state_dir: Path = Path("./state")

    @classmethod
    def load(cls) -> "BridgeConfig":
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
        state_dir = Path(os.getenv("BRIDGE_STATE_DIR", "./state")).expanduser().resolve()
        state_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            bot_token=token,
            allowed_chat_ids=allowed,
            claude_bin=os.getenv("CLAUDE_BIN", "claude").strip() or "claude",
            workdir=os.getenv("CLAUDE_WORKDIR", "/home/ubuntu").strip() or "/home/ubuntu",
            model=os.getenv("CLAUDE_MODEL", "").strip() or None,
            extra_args=shlex.split(os.getenv("CLAUDE_EXTRA_ARGS", "").strip()),
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
            "allowed_updates": ["message"],
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

    def send_message(self, chat_id: str, text: str) -> None:
        for chunk in chunk_text(text):
            response = self.session.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
                timeout=20,
            )
            self._raise_for_status("sendMessage", response)

    def set_my_commands(self, commands: list[dict[str, str]], scope: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"commands": commands}
        if scope is not None:
            payload["scope"] = scope
        response = self.session.post(
            f"{self.base_url}/setMyCommands",
            json=payload,
            timeout=20,
        )
        self._raise_for_status("setMyCommands", response)

    def delete_my_commands(self, scope: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {}
        if scope is not None:
            payload["scope"] = scope
        response = self.session.post(
            f"{self.base_url}/deleteMyCommands",
            json=payload,
            timeout=20,
        )
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
        self.offset_file.parent.mkdir(parents=True, exist_ok=True)
        self.offset_file.write_text(json.dumps({"offset": offset}, indent=2))

    def save_bridge_state(self, data: dict[str, Any]) -> None:
        self.bridge_file.parent.mkdir(parents=True, exist_ok=True)
        self.bridge_file.write_text(json.dumps(data, indent=2, ensure_ascii=True))

    def load_bridge_state(self) -> dict[str, Any]:
        if not self.bridge_file.exists():
            return {}
        try:
            return json.loads(self.bridge_file.read_text())
        except json.JSONDecodeError:
            return {}


@dataclass
class SessionInfo:
    id: str
    name: str
    cwd: str
    started_at: int
    updated_at: int


@dataclass
class TaskState:
    prompt: str
    chat_id: str
    workdir: str
    started_at: float
    command: list[str]
    process: subprocess.Popen[str]
    session_id: str | None = None
    session_mode: str = "new"
    assistant_messages: list[str] = field(default_factory=list)
    tail: deque[str] = field(default_factory=lambda: deque(maxlen=120))
    done: bool = False
    returncode: int | None = None
    last_error: str | None = None

    def duration(self) -> int:
        return int(time.time() - self.started_at)


class ClaudeBridge:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.telegram = TelegramClient(config)
        self.store = StateStore(config.state_dir)
        self.current_workdir = config.workdir
        self.task_lock = threading.Lock()
        self.task: TaskState | None = None
        self.shutdown = False
        state = self.store.load_bridge_state()
        self.anchored_sessions: dict[str, str] = dict(state.get("anchored_sessions") or {})
        self.last_session_menu: dict[str, list[SessionInfo]] = {}

    def desired_commands(self) -> list[dict[str, str]]:
        return [
            {"command": "start", "description": "Show chat id and setup status"},
            {"command": "help", "description": "Show available commands"},
            {"command": "sessions", "description": "List recent Claude sessions"},
            {"command": "session", "description": "Manage the anchored session"},
            {"command": "status", "description": "Show current task state"},
            {"command": "tail", "description": "Show recent Claude output"},
            {"command": "stop", "description": "Stop the running task"},
            {"command": "pwd", "description": "Show current workdir"},
            {"command": "cd", "description": "Change default workdir"},
            {"command": "run", "description": "Run a Claude task"},
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
            "current_workdir": self.current_workdir,
            "anchored_sessions": self.anchored_sessions,
            "task": None
            if task is None
            else {
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
        return (
            "Claude Telegram bridge commands\n\n"
            "/sessions - list recent Claude sessions\n"
            "/session current - show anchored session\n"
            "/session use <n|id> - anchor this chat to a session\n"
            "/session new - clear anchor so next message creates a fresh session\n"
            "/run <prompt> - run a Claude task\n"
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
            "Claude Telegram bridge\n\n"
            f"Your chat id: {chat_id}\n"
            f"Allowlisted: {allowlisted}\n\n"
            "Paste this into your .env if needed:\n"
            f"TELEGRAM_ALLOWED_CHAT_IDS={chat_id}\n\n"
            "After restarting the bridge, use /help to see available commands."
        )

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
            return "No anchored session. Next message will start a new Claude session."
        return f"Anchored session: {anchor}"

    def format_timestamp(self, raw_ms: int) -> str:
        if not raw_ms:
            return "-"
        return datetime.fromtimestamp(raw_ms / 1000).strftime("%Y-%m-%d %H:%M")

    def iso_to_ms(self, value: str | None) -> int:
        if not value:
            return 0
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return 0

    def load_recent_sessions(self, limit: int = 8) -> list[SessionInfo]:
        merged: dict[str, SessionInfo] = {}

        sessions_dir = Path.home() / ".claude" / "sessions"
        if sessions_dir.exists():
            for path in sessions_dir.glob("*.json"):
                try:
                    payload = json.loads(path.read_text())
                except json.JSONDecodeError:
                    continue
                session_id = str(payload.get("sessionId") or "").strip()
                if not session_id:
                    continue
                started_at = int(payload.get("startedAt") or 0)
                merged[session_id] = SessionInfo(
                    id=session_id,
                    name=str(payload.get("name") or path.stem),
                    cwd=str(payload.get("cwd") or ""),
                    started_at=started_at,
                    updated_at=started_at,
                )

        projects_dir = Path.home() / ".claude" / "projects"
        if projects_dir.exists():
            for path in projects_dir.glob("**/*.jsonl"):
                if "/subagents/" in str(path):
                    continue
                session_id = path.stem
                if not session_id or session_id.startswith("agent-"):
                    continue
                name = path.stem
                cwd = ""
                started_at = 0
                updated_at = int(path.stat().st_mtime * 1000)
                try:
                    with path.open() as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                event = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            event_session_id = str(event.get("sessionId") or session_id).strip()
                            if event_session_id:
                                session_id = event_session_id
                            if not cwd and event.get("cwd"):
                                cwd = str(event.get("cwd"))
                            ts_ms = self.iso_to_ms(event.get("timestamp"))
                            if ts_ms:
                                if not started_at:
                                    started_at = ts_ms
                                updated_at = max(updated_at, ts_ms)
                            custom_title = str(event.get("customTitle") or "").strip()
                            if custom_title:
                                name = custom_title
                            else:
                                ai_title = str(event.get("aiTitle") or event.get("aiTitleText") or "").strip()
                                if ai_title:
                                    name = ai_title
                except OSError:
                    continue

                existing = merged.get(session_id)
                if existing:
                    merged[session_id] = SessionInfo(
                        id=session_id,
                        name=name if name != path.stem else existing.name,
                        cwd=cwd or existing.cwd,
                        started_at=existing.started_at or started_at,
                        updated_at=max(existing.updated_at, updated_at),
                    )
                else:
                    merged[session_id] = SessionInfo(
                        id=session_id,
                        name=name,
                        cwd=cwd,
                        started_at=started_at,
                        updated_at=updated_at,
                    )

        sessions = list(merged.values())
        sessions.sort(key=lambda item: item.updated_at or item.started_at, reverse=True)
        return sessions[:limit]

    def show_sessions(self, chat_id: str, limit: int = 8) -> None:
        sessions = self.load_recent_sessions(limit=limit)
        self.last_session_menu[chat_id] = sessions
        anchor = self.current_anchor(chat_id)
        if not sessions:
            self.send(chat_id, "No Claude sessions were found in ~/.claude/sessions.")
            return
        lines = ["Recent Claude sessions\n"]
        for idx, session in enumerate(sessions, start=1):
            marker = " [anchored]" if session.id == anchor else ""
            label = session.name if session.name and session.name != session.id else "(none)"
            lines.append(
                f"{idx}. {label}{marker}\n"
                f"   id: {session.id}\n"
                f"   cwd: {session.cwd or '-'}\n"
                f"   updated: {self.format_timestamp(session.updated_at or session.started_at)}"
            )
        lines.append("\nUse /session use <number> or /session use <session_id>")
        self.send(chat_id, "\n".join(lines))

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
        return None

    def use_session(self, chat_id: str, selector: str) -> None:
        session = self.resolve_session_choice(chat_id, selector)
        if session is None:
            self.send(chat_id, "Session not found. Use /sessions first, then /session use <number> or pass a full session id.")
            return
        self.set_anchor(chat_id, session.id)
        self.send(
            chat_id,
            "Anchored this chat to Claude session\n\n"
            f"{session.name}\n"
            f"id: {session.id}\n"
            f"cwd: {session.cwd or '-'}\n"
            f"updated: {self.format_timestamp(session.updated_at or session.started_at)}",
        )

    def handle_message(self, message: dict[str, Any]) -> None:
        chat_id = str(message["chat"]["id"])
        text = (message.get("text") or "").strip()
        if not text:
            return

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
            self.send(chat_id, self.help_text())
            return
        if text == "/sessions":
            self.show_sessions(chat_id)
            return
        if text == "/session":
            self.send(chat_id, "Usage: /session current | /session use <n|id> | /session new")
            return
        if text == "/session current":
            self.send(chat_id, self.format_anchor_text(chat_id))
            return
        if text == "/session new":
            self.set_anchor(chat_id, None)
            self.send(chat_id, "Cleared anchored session. The next message will create and anchor a new Claude session.")
            return
        if text.startswith("/session use "):
            self.use_session(chat_id, text[len("/session use "):].strip())
            return
        if text == "/status":
            self.send(chat_id, self.status_text(chat_id))
            return
        if text == "/tail":
            self.send(chat_id, self.tail_text())
            return
        if text == "/stop":
            self.stop_task(chat_id)
            return
        if text == "/pwd":
            self.send(chat_id, f"Current workdir: {self.current_workdir}")
            return
        if text.startswith("/cd "):
            self.change_workdir(chat_id, text[4:].strip())
            return
        if text.startswith("/run "):
            self.start_task(chat_id, text[5:].strip())
            return
        if text == "/run":
            self.send(chat_id, "Usage: /run <prompt>")
            return

        self.send(chat_id, "Unknown command. Use /help.")

    def change_workdir(self, chat_id: str, requested: str) -> None:
        path = Path(requested).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            self.send(chat_id, f"Not a directory: {path}")
            return
        self.current_workdir = str(path)
        self.store.save_bridge_state(self.bridge_snapshot())
        self.send(chat_id, f"Default workdir updated to {self.current_workdir}")

    def build_claude_command(self, prompt: str, session_id: str | None) -> tuple[list[str], str]:
        command = [
            self.config.claude_bin,
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
        ]
        if self.config.model:
            command.extend(["--model", self.config.model])
        command.extend(self.config.extra_args)
        if session_id:
            command.extend(["-r", session_id])
            command.append(prompt)
            return command, "resume"
        command.extend(["-n", "telegram-claude"])
        command.append(prompt)
        return command, "new"

    def start_task(self, chat_id: str, prompt: str) -> None:
        prompt = prompt.strip()
        if not prompt:
            self.send(chat_id, "Prompt is empty.")
            return

        with self.task_lock:
            if self.task and not self.task.done:
                self.send(chat_id, "A task is already running. Use /status, /tail, or /stop first.")
                return
            anchor = self.current_anchor(chat_id)
            command, session_mode = self.build_claude_command(prompt, anchor)
            process = subprocess.Popen(
                command,
                cwd=self.current_workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self.task = TaskState(
                prompt=prompt,
                chat_id=chat_id,
                workdir=self.current_workdir,
                started_at=time.time(),
                command=command,
                process=process,
                session_id=anchor,
                session_mode=session_mode,
            )
            worker = threading.Thread(target=self._watch_task, args=(self.task,), daemon=True)
            worker.start()

        short_prompt = prompt if len(prompt) <= 240 else f"{prompt[:237]}..."
        session_line = f"\nSession: {anchor}" if anchor else "\nSession: new (will auto-anchor on success)"
        self.store.save_bridge_state(self.bridge_snapshot())
        self.send(chat_id, f"Started Claude task in {self.current_workdir}{session_line}\n\nPrompt: {short_prompt}")

    def _watch_task(self, task: TaskState) -> None:
        try:
            assert task.process.stdout is not None
            for raw_line in task.process.stdout:
                line = raw_line.rstrip("\n")
                if line:
                    task.tail.append(line)
                self._consume_claude_line(task, line)
            task.returncode = task.process.wait()
            task.done = True
            self.send(task.chat_id, self._finish_message(task))
        except Exception as exc:
            task.done = True
            task.last_error = str(exc)
            self.send(task.chat_id, f"Bridge error while running Claude: {exc}")
        finally:
            self.store.save_bridge_state(self.bridge_snapshot())

    def _consume_claude_line(self, task: TaskState, line: str) -> None:
        if not line.startswith("{"):
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        event_type = payload.get("type")
        session_id = payload.get("session_id")
        if session_id:
            task.session_id = session_id
            self.set_anchor(task.chat_id, session_id)
        if event_type == "assistant":
            message = payload.get("message") or {}
            content = message.get("content") or []
            text_parts = [part.get("text", "") for part in content if part.get("type") == "text"]
            text = "\n".join(part for part in text_parts if part).strip()
            if text:
                task.assistant_messages.append(text)
        elif event_type == "result":
            result_text = (payload.get("result") or "").strip()
            if result_text and (not task.assistant_messages or task.assistant_messages[-1] != result_text):
                task.assistant_messages.append(result_text)

    def _finish_message(self, task: TaskState) -> str:
        header = f"Claude task finished with exit code {task.returncode} in {task.duration()}s"
        body = "\n\n".join(task.assistant_messages[-3:]).strip()
        tail = "\n".join(list(task.tail)[-20:]).strip()
        if not body:
            body = "(No assistant message captured. Use /tail for raw output.)"
        message = f"{header}\n\n{body}"
        if task.returncode and tail:
            message += f"\n\nRecent output:\n{tail}"
        return message

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
        return f"Recent output from Claude:\n\n{tail[-3500:]}"

    def stop_task(self, chat_id: str) -> None:
        with self.task_lock:
            task = self.task
            if task is None or task.done:
                self.send(chat_id, "No running task to stop.")
                return
            task.process.terminate()
            try:
                task.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                task.process.kill()
                task.process.wait(timeout=5)
            task.done = True
            task.returncode = task.process.returncode
            self.store.save_bridge_state(self.bridge_snapshot())
        self.send(chat_id, f"Stopped Claude task. Exit code: {task.returncode}")

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
            except requests.RequestException as exc:
                time.sleep(2)
                print(f"telegram request error: {exc}", flush=True)
            except TelegramAPIError as exc:
                time.sleep(2)
                print(f"telegram api error: {exc}", flush=True)
            except Exception as exc:
                time.sleep(2)
                print(f"bridge loop error: {exc}", flush=True)

    def request_shutdown(self, *_args: Any) -> None:
        self.shutdown = True


def main() -> None:
    config = BridgeConfig.load()
    bridge = ClaudeBridge(config)
    bridge.sync_telegram_commands()
    signal.signal(signal.SIGINT, bridge.request_shutdown)
    signal.signal(signal.SIGTERM, bridge.request_shutdown)
    bridge.run_forever()


if __name__ == "__main__":
    main()
