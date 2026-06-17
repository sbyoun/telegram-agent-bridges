"""Codex CLI provider."""
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
        # Current Codex stores sessions as rollout files:
        #   ~/.codex/sessions/YYYY/MM/DD/rollout-<localtime>-<uuid>.jsonl
        # file mtime = last activity; the legacy session_index.jsonl is stale.
        base = Path.home() / ".codex" / "sessions"
        if base.exists():
            excluded = excluded_cwds(self.env_prefix)
            files = list(base.glob("**/rollout-*.jsonl"))
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            cap = 60 if limit is None else max(limit, 8)
            sessions: list[SessionInfo] = []
            for path in files:
                if len(sessions) >= cap:
                    break
                sid = self._id_from_rollout_name(path.name)
                if not sid:
                    continue
                cwd = self._cwd_from_rollout(path)
                if is_excluded(cwd, excluded):
                    continue
                sessions.append(SessionInfo(
                    id=sid,
                    name=self._title_from_rollout(path),
                    cwd=cwd,
                    updated_ms=int(path.stat().st_mtime * 1000),
                ))
            return sessions

        # fallback: legacy index (older Codex versions)
        index_path = Path.home() / ".codex" / "session_index.jsonl"
        if not index_path.exists():
            return []
        sessions = []
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

    @staticmethod
    def _id_from_rollout_name(name: str) -> str:
        stem = name
        if stem.startswith("rollout-"):
            stem = stem[len("rollout-"):]
        if stem.endswith(".jsonl"):
            stem = stem[:-len(".jsonl")]
        parts = stem.split("-")
        if len(parts) >= 5:  # last 5 groups form the UUID (8-4-4-4-12)
            return "-".join(parts[-5:])
        return ""

    @staticmethod
    def _cwd_from_rollout(path: Path) -> str:
        """Read the session cwd from the rollout's session_meta header (line 0).

        Cheap: cwd lives in the first few events, so excluded loop-engine
        sessions are skipped before the more expensive title scan runs.
        """
        try:
            with path.open() as fh:
                for i, line in enumerate(fh):
                    if i > 8:
                        break
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    pl = ev.get("payload") or {}
                    if isinstance(pl, dict) and pl.get("cwd"):
                        return str(pl.get("cwd"))
        except OSError:
            pass
        return ""

    @staticmethod
    def _title_from_rollout(path: Path) -> str:
        """Best-effort title: first real user prompt in the rollout (cheap scan)."""
        try:
            with path.open() as fh:
                for i, line in enumerate(fh):
                    if i > 60:
                        break
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") != "response_item":
                        continue
                    pl = ev.get("payload") or {}
                    if pl.get("role") != "user":
                        continue
                    for c in (pl.get("content") or []):
                        if c.get("type") == "input_text":
                            t = (c.get("text") or "").strip()
                            if t and not t.startswith("<"):
                                return t[:40]
        except OSError:
            pass
        return "(codex)"
