"""Antigravity (agy) CLI provider.

Google's Antigravity CLI (`agy`) replaces the deprecated Gemini CLI, which is
retired on 2026-06-18. Unlike the claude/codex/gemini providers, `agy --print`
emits **plain text** — there is no stream-json output format and no terminal
"result" event. Two consequences shape this adapter:

  1. Output is buffered across all stdout lines and flushed **once** via
     finalize() so multi-line answers are surfaced whole (the core's per-line
     assistant_messages[-3:] model would otherwise truncate them).
  2. The conversation id is not printed, and `--print` runs do NOT append to
     history.jsonl (only interactive runs do) — so the id is captured post-run
     from the newest conversations/<uuid>.db file by mtime, which is the db agy
     just wrote. The next message resumes with `--conversation <id>`, falling
     back to `--continue` (most-recent conversation) when no id is captured —
     matching the original antigravity bridge's "continue latest" behavior.

Flags (from `agy --help`):
  --print <prompt>            run one prompt non-interactively (value flag)
  --conversation <id>         resume a previous conversation by id
  --continue / -c             continue the most recent conversation
  --dangerously-skip-permissions  auto-approve tool permissions (autonomous bot)

The model is configured in agy's own settings.json (no --model flag exists), so
ANTIGRAVITY_MODEL is intentionally ignored here.
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import Command, LineEvent, Provider, SessionInfo

RESUME_SENTINEL = "latest"
AGY_HOME = Path.home() / ".gemini" / "antigravity-cli"
HISTORY = AGY_HOME / "history.jsonl"
CONVERSATIONS = AGY_HOME / "conversations"


class AntigravityProvider(Provider):
    name = "antigravity"
    display = "Antigravity"
    default_bin = "agy"
    env_prefix = "ANTIGRAVITY"

    def __init__(self) -> None:
        # buffers the current run's stdout; reset per build_command. Only one
        # task runs at a time (core holds task_lock), so instance state is safe.
        self._buf: list[str] = []

    def build_command(self, cfg, prompt, workdir, session_id) -> Command:
        self._buf = []
        argv = [cfg.cli_bin]
        if session_id and session_id != RESUME_SENTINEL:
            argv.extend(["--conversation", session_id])
        elif session_id == RESUME_SENTINEL:
            argv.append("--continue")
        # autonomous bridge: never block on an interactive permission prompt
        argv.append("--dangerously-skip-permissions")
        argv.extend(cfg.extra_args)
        # --print is a value flag and must take the prompt as its value, last.
        argv.extend(["--print", prompt])
        if session_id:
            return Command(argv, "resume")
        # new session: anchor to the sentinel so the next message still continues
        # via --continue even if finalize() fails to capture the real id.
        return Command(argv, "new", session_hint=RESUME_SENTINEL)

    def consume_line(self, line: str) -> LineEvent | None:
        # plain-text output: accumulate, surface once in finalize().
        if line:
            self._buf.append(line)
        return None

    def finalize(self, task) -> LineEvent | None:
        ev = LineEvent()
        text = "\n".join(self._buf).strip()
        if text:
            ev.assistant_text = text
        cid = self._latest_conversation_id()
        if cid:
            ev.session_id = cid
        return ev if (ev.assistant_text or ev.session_id) else None

    def _latest_conversation_id(self) -> str | None:
        # --print runs persist a conversations/<uuid>.db but do NOT touch
        # history.jsonl, so the newest db by mtime is the run we just finished.
        try:
            dbs = list(CONVERSATIONS.glob("*.db"))
        except OSError:
            return None
        if not dbs:
            return None
        newest = max(dbs, key=lambda p: p.stat().st_mtime)
        return newest.stem or None

    def list_sessions(self, limit: int | None = None) -> list[SessionInfo]:
        # conversations/<uuid>.db (mtime = last activity) is the source of truth;
        # enrich titles from history.jsonl where an interactive prompt exists.
        try:
            dbs = list(CONVERSATIONS.glob("*.db"))
        except OSError:
            return []
        titles = self._history_titles()
        sessions = [
            SessionInfo(
                id=p.stem,
                name=titles.get(p.stem, "(agy)"),
                cwd="",
                updated_ms=int(p.stat().st_mtime * 1000),
            )
            for p in dbs
        ]
        sessions.sort(key=lambda s: s.updated_ms, reverse=True)
        return sessions if limit is None else sessions[:limit]

    @staticmethod
    def _history_titles() -> dict[str, str]:
        titles: dict[str, str] = {}
        try:
            with HISTORY.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cid = str(e.get("conversationId") or "").strip()
                    disp = str(e.get("display") or "").strip()
                    if cid and disp:
                        titles[cid] = disp[:40]  # last prompt wins
        except OSError:
            pass
        return titles
