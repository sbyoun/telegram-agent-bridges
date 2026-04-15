#!/usr/bin/env python3
"""Minimal Telegram bridge for Codex CLI.

Features:
- Poll Telegram updates without extra frameworks
- Run Codex tasks and optionally anchor them to a persistent session
- Send final agent replies back to Telegram
- Provide task control and Codex session selection commands

Environment:
- TELEGRAM_BOT_TOKEN: Telegram bot token from BotFather
- TELEGRAM_ALLOWED_CHAT_IDS: comma-separated allowed chat ids
- CODEX_WORKDIR: default workdir for Codex tasks
- CODEX_BIN: Codex executable path, default `codex`
- CODEX_MODEL: optional model override
- CODEX_EXTRA_ARGS: optional extra args appended to `codex exec`
- TELEGRAM_POLL_TIMEOUT: polling timeout seconds, default 3
- TELEGRAM_PLAIN_TEXT_AS_RUN: if `1`, non-command text becomes `/run <text>`
- BRIDGE_STATE_DIR: directory for state files, default `./state`
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
    codex_bin: str = "codex"
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
        extra_args = shlex.split(os.getenv("CODEX_EXTRA_ARGS", "").strip())
        state_dir = Path(os.getenv("BRIDGE_STATE_DIR", "./state")).expanduser().resolve()
        state_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            bot_token=token,
            allowed_chat_ids=allowed,
            codex_bin=os.getenv("CODEX_BIN", "codex").strip() or "codex",
            workdir=os.getenv("CODEX_WORKDIR", "/home/ubuntu").strip() or "/home/ubuntu",
            model=os.getenv("CODEX_MODEL", "").strip() or None,
            extra_args=extra_args,
            poll_timeout=int(os.getenv("TELEGRAM_POLL_TIMEOUT", "3")),
            plain_text_as_run=env_flag("TELEGRAM_PLAIN_TEXT_AS_RUN", False),
            state_dir=state_dir,
        )


class TelegramClient:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.base_url = f"{API_BASE}/bot{config.bot_token}"
        self.session = requests.Session()

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
        response.raise_for_status()
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
            response.raise_for_status()

    def set_my_commands(self, commands: list[dict[str, str]], scope: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"commands": commands}
        if scope is not None:
            payload["scope"] = scope
        response = self.session.post(
            f"{self.base_url}/setMyCommands",
            json=payload,
            timeout=20,
        )
        response.raise_for_status()

    def delete_my_commands(self, scope: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {}
        if scope is not None:
            payload["scope"] = scope
        response = self.session.post(
            f"{self.base_url}/deleteMyCommands",
            json=payload,
            timeout=20,
        )
        response.raise_for_status()


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
    thread_name: str
    updated_at: str


@dataclass
class TaskState:
    prompt: str
    chat_id: str
    workdir: str
    started_at: float
    command: list[str]
    process: subprocess.Popen[str]
    thread_id: str | None = None
    session_mode: str = "new"
    assistant_messages: list[str] = field(default_factory=list)
    tail: deque[str] = field(default_factory=lambda: deque(maxlen=120))
    done: bool = False
    returncode: int | None = None
    last_error: str | None = None

    def duration(self) -> int:
        return int(time.time() - self.started_at)


class CodexBridge:
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
            {"command": "sessions", "description": "List recent Codex sessions"},
            {"command": "session", "description": "Manage the anchored session"},
            {"command": "status", "description": "Show current task state"},
            {"command": "tail", "description": "Show recent Codex output"},
            {"command": "stop", "description": "Stop the running task"},
            {"command": "pwd", "description": "Show current workdir"},
            {"command": "cd", "description": "Change default workdir"},
            {"command": "run", "description": "Run a Codex task"},
        ]

    def sync_telegram_commands(self) -> None:
        commands = self.desired_commands()
        self.telegram.set_my_commands(commands, {"type": "default"})
        self.telegram.set_my_commands(commands, {"type": "all_private_chats"})
        for chat_id in self.config.allowed_chat_ids:
            self.telegram.delete_my_commands({"type": "chat", "chat_id": int(chat_id)})

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
                "thread_id": task.thread_id,
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
            "Codex Telegram bridge commands\n\n"
            "/run <prompt> - run a Codex task\n"
            "/sessions - list recent Codex sessions\n"
            "/session current - show anchored session\n"
            "/session use <n|id> - anchor this chat to a session\n"
            "/session new - clear anchor so next /run creates a fresh session\n"
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
            "Codex Telegram bridge\n\n"
            f"Your chat id: {chat_id}\n"
            f"Allowlisted: {allowlisted}\n\n"
            "Paste this into your .env if needed:\n"
            f"TELEGRAM_ALLOWED_CHAT_IDS={chat_id}\n\n"
            "After restarting the bridge, use /help to see available commands."
        )

    def load_recent_sessions(self, limit: int = 8) -> list[SessionInfo]:
        index_path = Path.home() / ".codex" / "session_index.jsonl"
        if not index_path.exists():
            return []
        sessions: list[SessionInfo] = []
        for line in reversed(index_path.read_text().splitlines()):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = str(payload.get("id") or "").strip()
            if not session_id:
                continue
            sessions.append(
                SessionInfo(
                    id=session_id,
                    thread_name=str(payload.get("thread_name") or "(untitled)"),
                    updated_at=str(payload.get("updated_at") or ""),
                )
            )
            if len(sessions) >= limit:
                break
        return sessions

    def format_timestamp(self, raw: str) -> str:
        if not raw:
            return "-"
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return raw

    def current_anchor(self, chat_id: str) -> str | None:
        return self.anchored_sessions.get(chat_id)

    def format_anchor_text(self, chat_id: str) -> str:
        anchor = self.current_anchor(chat_id)
        if not anchor:
            return "No anchored session. Next /run will start a new Codex session."
        return f"Anchored session: {anchor}"

    def show_sessions(self, chat_id: str, limit: int = 8) -> None:
        sessions = self.load_recent_sessions(limit=limit)
        self.last_session_menu[chat_id] = sessions
        anchor = self.current_anchor(chat_id)
        if not sessions:
            self.send(chat_id, "No Codex sessions were found in ~/.codex/session_index.jsonl.")
            return
        lines = ["Recent Codex sessions\n"]
        for idx, session in enumerate(sessions, start=1):
            marker = " [anchored]" if session.id == anchor else ""
            lines.append(
                f"{idx}. {session.thread_name}{marker}\n"
                f"   id: {session.id}\n"
                f"   updated: {self.format_timestamp(session.updated_at)}"
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

    def set_anchor(self, chat_id: str, session_id: str | None) -> None:
        if session_id is None:
            self.anchored_sessions.pop(chat_id, None)
        else:
            self.anchored_sessions[chat_id] = session_id
        self.store.save_bridge_state(self.bridge_snapshot())

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
            self.send(chat_id, "Cleared anchored session. The next /run will create and anchor a new Codex session.")
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

    def use_session(self, chat_id: str, selector: str) -> None:
        session = self.resolve_session_choice(chat_id, selector)
        if session is None:
            self.send(chat_id, "Session not found. Use /sessions first, then /session use <number> or pass a full session id.")
            return
        self.set_anchor(chat_id, session.id)
        self.send(
            chat_id,
            "Anchored this chat to Codex session\n\n"
            f"{session.thread_name}\n"
            f"id: {session.id}\n"
            f"updated: {self.format_timestamp(session.updated_at)}",
        )

    def build_codex_command(self, prompt: str, workdir: str, session_id: str | None) -> tuple[list[str], str]:
        if session_id:
            command = [
                self.config.codex_bin,
                "exec",
                "resume",
                "--skip-git-repo-check",
                "--json",
            ]
            if self.config.model:
                command.extend(["--model", self.config.model])
            command.extend(self.config.extra_args)
            command.extend([session_id, prompt])
            return command, "resume"

        command = [
            self.config.codex_bin,
            "exec",
            "--skip-git-repo-check",
            "--json",
            "--color",
            "never",
            "-C",
            workdir,
        ]
        if self.config.model:
            command.extend(["--model", self.config.model])
        command.extend(self.config.extra_args)
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
            command, session_mode = self.build_codex_command(prompt, self.current_workdir, anchor)
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
                session_mode=session_mode,
                thread_id=anchor,
            )
            worker = threading.Thread(target=self._watch_task, args=(self.task,), daemon=True)
            worker.start()

        short_prompt = prompt if len(prompt) <= 240 else f"{prompt[:237]}..."
        self.store.save_bridge_state(self.bridge_snapshot())
        anchor_text = f"\nSession: {anchor}" if anchor else "\nSession: new (will auto-anchor on success)"
        self.send(chat_id, f"Started Codex task in {self.current_workdir}{anchor_text}\n\nPrompt: {short_prompt}")

    def _watch_task(self, task: TaskState) -> None:
        try:
            assert task.process.stdout is not None
            for raw_line in task.process.stdout:
                line = raw_line.rstrip("\n")
                if line:
                    task.tail.append(line)
                self._consume_codex_line(task, line)
            task.returncode = task.process.wait()
            task.done = True
            summary = self._finish_message(task)
            self.send(task.chat_id, summary)
        except Exception as exc:  # pragma: no cover - operational fallback
            task.done = True
            task.last_error = str(exc)
            self.send(task.chat_id, f"Bridge error while running Codex: {exc}")
        finally:
            self.store.save_bridge_state(self.bridge_snapshot())

    def _consume_codex_line(self, task: TaskState, line: str) -> None:
        if not line.startswith("{"):
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return

        event_type = payload.get("type")
        if event_type == "thread.started":
            task.thread_id = payload.get("thread_id")
            if task.thread_id:
                self.set_anchor(task.chat_id, task.thread_id)
            return
        if event_type == "item.completed":
            item = payload.get("item") or {}
            if item.get("type") == "agent_message":
                text = (item.get("text") or "").strip()
                if text:
                    task.assistant_messages.append(text)

    def _finish_message(self, task: TaskState) -> str:
        header = f"Codex task finished with exit code {task.returncode} in {task.duration()}s"
        body = "\n\n".join(task.assistant_messages[-3:]).strip()
        tail = "\n".join(list(task.tail)[-20:]).strip()
        if not body:
            body = "(No agent message captured. Use /tail for raw output.)"
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
        thread_line = f"\nThread: {task.thread_id}" if task.thread_id else ""
        return (
            f"Task state: {state}\n"
            f"Workdir: {task.workdir}\n"
            f"Session mode: {task.session_mode}\n"
            f"Anchored session: {self.current_anchor(chat_id) or '-'}\n"
            f"Duration: {task.duration()}s\n"
            f"Exit code: {task.returncode}\n"
            f"Prompt: {task.prompt[:280]}{thread_line}"
        )

    def tail_text(self) -> str:
        task = self.task
        if task is None:
            return "No task output available yet."
        tail = "\n".join(task.tail).strip()
        if not tail:
            return "No output captured yet."
        return f"Recent output from Codex:\n\n{tail[-3500:]}"

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
        self.send(chat_id, f"Stopped Codex task. Exit code: {task.returncode}")

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
            except Exception as exc:  # pragma: no cover - operational fallback
                time.sleep(2)
                print(f"bridge loop error: {exc}", flush=True)

    def request_shutdown(self, *_args: Any) -> None:
        self.shutdown = True


def main() -> None:
    config = BridgeConfig.load()
    bridge = CodexBridge(config)
    bridge.sync_telegram_commands()
    signal.signal(signal.SIGINT, bridge.request_shutdown)
    signal.signal(signal.SIGTERM, bridge.request_shutdown)
    bridge.run_forever()


if __name__ == "__main__":
    main()
