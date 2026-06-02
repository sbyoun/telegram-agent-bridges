"""Provider abstraction for the unified Telegram bridge.

Everything that differs between Claude / Codex / Gemini lives behind this
interface. The shared core (tgbridge.core) handles all Telegram I/O, command
dispatch, state, pagination and task monitoring identically for every provider.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionInfo:
    """Uniform session descriptor across providers."""
    id: str
    name: str = ""
    cwd: str = ""
    updated_ms: int = 0


@dataclass
class Command:
    """A built CLI invocation.

    argv         : process argument list
    mode         : "new" | "resume" (for status display)
    session_hint : if the provider already knows the session id at build time
                   (e.g. Gemini --session-id), the core anchors it immediately.
                   For Claude/Codex this is None; the id is captured from output.
    """
    argv: list[str]
    mode: str
    session_hint: str | None = None


@dataclass
class LineEvent:
    """Parsed result of one stdout line from the CLI."""
    session_id: str | None = None       # capture/anchor this session id
    assistant_text: str | None = None   # assistant/agent text to surface
    is_result: bool = False             # a terminal "result" event (send now)
    result_subtype: str | None = None
    is_error: bool = False


class Provider:
    name = "base"
    display = "Agent"
    default_bin = "true"
    env_prefix = "AGENT"

    def build_command(self, cfg, prompt: str, workdir: str, session_id: str | None) -> Command:
        raise NotImplementedError

    def consume_line(self, line: str) -> "LineEvent | None":
        """Parse one stdout line. Pure: no side effects. Return None to ignore."""
        return None

    def list_sessions(self, limit: int | None = None) -> list[SessionInfo]:
        return []


def get_provider(name: str) -> Provider:
    name = (name or "").strip().lower()
    if name == "claude":
        from .claude import ClaudeProvider
        return ClaudeProvider()
    if name == "codex":
        from .codex import CodexProvider
        return CodexProvider()
    if name == "gemini":
        from .gemini import GeminiProvider
        return GeminiProvider()
    raise SystemExit(f"Unknown BRIDGE_PROVIDER: {name!r} (expected claude|codex|gemini)")
