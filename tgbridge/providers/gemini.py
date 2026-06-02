"""Gemini CLI provider.

NOTE: best-effort adapter. The original antigravity bridge (gemini) was deleted
and was not under git, so this is reconstructed from `gemini --help`:
  -p/--prompt (headless), -y/--yolo, -o stream-json,
  -r/--resume latest|<index>  (NB: resume takes "latest" or an index, NOT a uuid),
  --session-id <uuid> (start a new session), --list-sessions.

Because `-r` cannot take a stable uuid, anchoring uses the "latest" sentinel:
after the first run we anchor to "latest" so subsequent messages continue the
most recent session — matching the original antigravity behavior.

The stream-json parsing here is tolerant/best-effort and should be verified
against a live Gemini run before relying on it; the finish message falls back to
raw tail output when no assistant text is captured.
"""
from __future__ import annotations

import json

from .base import Command, LineEvent, Provider, SessionInfo

RESUME_SENTINEL = "latest"


class GeminiProvider(Provider):
    name = "gemini"
    display = "Gemini"
    default_bin = "gemini"
    env_prefix = "GEMINI"

    def build_command(self, cfg, prompt, workdir, session_id) -> Command:
        argv = [cfg.cli_bin, "-p", prompt, "-o", "stream-json", "-y", "--skip-trust"]
        if cfg.model:
            argv.extend(["--model", cfg.model])
        argv.extend(cfg.extra_args)
        if session_id:
            argv.extend(["-r", session_id or RESUME_SENTINEL])
            return Command(argv, "resume")
        # new session: anchor to the "latest" sentinel so the next message resumes it
        return Command(argv, "new", session_hint=RESUME_SENTINEL)

    def consume_line(self, line: str) -> LineEvent | None:
        if not line.startswith("{"):
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        ev = LineEvent()
        # tolerant extraction across plausible stream-json shapes
        text = ""
        if isinstance(payload.get("response"), str):
            text = payload["response"]
        elif payload.get("type") in {"assistant", "content", "message"}:
            content = payload.get("content") or payload.get("text") or ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = [c.get("text", "") for c in content if isinstance(c, dict)]
                text = "\n".join(p for p in parts if p)
        text = (text or "").strip()
        if text:
            ev.assistant_text = text
        return ev

    def list_sessions(self, limit: int | None = None) -> list[SessionInfo]:
        # Gemini resume is index/"latest"-based, not uuid-based; listing is not
        # wired up yet. Use /session new to reset; messages resume "latest".
        return []
