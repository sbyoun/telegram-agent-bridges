"""Codex CLI provider."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .base import Command, LineEvent, Provider, SessionInfo


def _iso_to_ms(value) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


class CodexProvider(Provider):
    name = "codex"
    display = "Codex"
    default_bin = "codex"
    env_prefix = "CODEX"

    def build_command(self, cfg, prompt, workdir, session_id) -> Command:
        if session_id:
            argv = [cfg.cli_bin, "exec", "resume", "--skip-git-repo-check", "--json"]
            if cfg.model:
                argv.extend(["--model", cfg.model])
            argv.extend(cfg.extra_args)
            argv.extend([session_id, prompt])
            return Command(argv, "resume")

        argv = [
            cfg.cli_bin, "exec", "--skip-git-repo-check", "--json",
            "--color", "never", "-C", workdir,
        ]
        if cfg.model:
            argv.extend(["--model", cfg.model])
        argv.extend(cfg.extra_args)
        argv.append(prompt)
        return Command(argv, "new")

    def consume_line(self, line: str) -> LineEvent | None:
        if not line.startswith("{"):
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        event_type = payload.get("type")
        ev = LineEvent()
        if event_type == "thread.started":
            ev.session_id = payload.get("thread_id")
            return ev
        if event_type == "item.completed":
            item = payload.get("item") or {}
            if item.get("type") == "agent_message":
                text = (item.get("text") or "").strip()
                if text:
                    ev.assistant_text = text
        return ev

    def list_sessions(self, limit: int | None = None) -> list[SessionInfo]:
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
            sessions.append(SessionInfo(
                id=session_id,
                name=str(payload.get("thread_name") or "(untitled)"),
                cwd="",
                updated_ms=_iso_to_ms(payload.get("updated_at")),
            ))
            if limit is not None and len(sessions) >= limit:
                break
        return sessions
