"""Claude Code CLI provider."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .base import Command, LineEvent, Provider, SessionInfo, excluded_cwds, is_excluded


def _iso_to_ms(value) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


class ClaudeProvider(Provider):
    name = "claude"
    display = "Claude"
    default_bin = "claude"
    env_prefix = "CLAUDE"

    def build_command(self, cfg, prompt, workdir, session_id) -> Command:
        argv = [
            cfg.cli_bin,
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
        ]
        if cfg.model:
            argv.extend(["--model", cfg.model])
        argv.extend(cfg.extra_args)
        if session_id:
            argv.extend(["-r", session_id, prompt])
            return Command(argv, "resume")
        argv.extend(["-n", "telegram-claude", prompt])
        return Command(argv, "new")

    def consume_line(self, line: str) -> LineEvent | None:
        if not line.startswith("{"):
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        ev = LineEvent()
        session_id = payload.get("session_id")
        if session_id:
            ev.session_id = session_id
        event_type = payload.get("type")
        if event_type == "assistant":
            message = payload.get("message") or {}
            content = message.get("content") or []
            parts = [p.get("text", "") for p in content if p.get("type") == "text"]
            text = "\n".join(p for p in parts if p).strip()
            if text:
                ev.assistant_text = text
        elif event_type == "result":
            ev.is_result = True
            ev.result_subtype = str(payload.get("subtype") or "result")
            ev.is_error = bool(payload.get("is_error"))
            result_text = (payload.get("result") or "").strip()
            if result_text:
                ev.assistant_text = result_text
        return ev

    def list_sessions(self, limit: int | None = None) -> list[SessionInfo]:
        merged: dict[str, SessionInfo] = {}
        excluded = excluded_cwds(self.env_prefix)

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
                if is_excluded(str(payload.get("cwd") or ""), excluded):
                    continue
                started_at = int(payload.get("startedAt") or 0)
                merged[session_id] = SessionInfo(
                    id=session_id,
                    name=str(payload.get("name") or path.stem),
                    cwd=str(payload.get("cwd") or ""),
                    updated_ms=started_at,
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
                custom_title = ""
                ai_title = ""
                cwd = ""
                started_at = 0
                updated_at = int(path.stat().st_mtime * 1000)
                skip = False
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
                                # Bail out early on automated-loop dirs so we
                                # don't fully parse hundreds of jsonl files.
                                if is_excluded(cwd, excluded):
                                    skip = True
                                    break
                            ts_ms = _iso_to_ms(event.get("timestamp"))
                            if ts_ms:
                                if not started_at:
                                    started_at = ts_ms
                                updated_at = max(updated_at, ts_ms)
                            ct = str(event.get("customTitle") or "").strip()
                            if ct:
                                custom_title = ct
                            else:
                                at = str(event.get("aiTitle") or event.get("aiTitleText") or "").strip()
                                if at:
                                    ai_title = at
                except OSError:
                    continue

                if skip:
                    continue

                # A user's manual rename (customTitle) always wins over the
                # AI-generated title, regardless of which event appears last:
                # ai-title events are re-emitted on every session load and would
                # otherwise clobber the rename.
                name = custom_title or ai_title or name

                existing = merged.get(session_id)
                if existing:
                    merged[session_id] = SessionInfo(
                        id=session_id,
                        name=name if name != path.stem else existing.name,
                        cwd=cwd or existing.cwd,
                        updated_ms=max(existing.updated_ms, updated_at, started_at),
                    )
                else:
                    merged[session_id] = SessionInfo(
                        id=session_id,
                        name=name,
                        cwd=cwd,
                        updated_ms=updated_at or started_at,
                    )

        sessions = sorted(merged.values(), key=lambda s: s.updated_ms, reverse=True)
        return sessions if limit is None else sessions[:limit]
