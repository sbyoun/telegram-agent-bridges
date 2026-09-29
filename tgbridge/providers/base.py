"""Provider abstraction for the unified Telegram bridge.

Everything that differs between Claude / Codex / Gemini lives behind this
interface. The shared core (tgbridge.core) handles all Telegram I/O, command
dispatch, state, pagination and task monitoring identically for every provider.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# cwd prefixes whose sessions are hidden from /sessions even with no env set.
# loop-engine spawns hundreds of headless agent loops there, and batch callers
# (yousinsa agy calls etc.) run under /tmp; both bury the interactive sessions
# and are never resumed from Telegram.
DEFAULT_EXCLUDED_CWDS = ("/home/ubuntu/loop-engine", "/tmp")


def excluded_cwds(env_prefix: str) -> tuple[str, ...]:
    """cwd prefixes to hide from a provider's session list.

    Configurable via ``<PREFIX>_EXCLUDE_CWDS`` (comma-separated absolute paths).
    When the env var is unset, the loop-engine default applies; set it to an
    empty string to disable filtering entirely.
    """
    raw = os.getenv(f"{env_prefix}_EXCLUDE_CWDS")
    if raw is None:
        return DEFAULT_EXCLUDED_CWDS
    return tuple(p.strip().rstrip("/") for p in raw.split(",") if p.strip())


def is_excluded(cwd: str, excluded: tuple[str, ...]) -> bool:
    cwd = (cwd or "").rstrip("/")
    if not cwd:
        return False
    return any(cwd == base or cwd.startswith(base + "/") for base in excluded)


@dataclass
class SessionInfo:
    """Uniform session descriptor across providers."""
    id: str
    name: str = ""
    cwd: str = ""
    updated_ms: int = 0
    # 제목(customTitle/aiTitle)이 없는 세션이 대다수라, 목록에서 구분되도록
    # 첫 사용자 프롬프트를 폴백 라벨로 쓴다. 없으면 빈 문자열.
    first_prompt: str = ""
    # 사용자가 직접 붙인 제목(/rename)인지. 자동 제목(aiTitle)보다 앞에 정렬한다.
    custom_title: bool = False


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
    clear_anchor: bool = False          # provider rejected the anchored session


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

    def finalize(self, task) -> "LineEvent | None":
        """Called once after stdout closes, before the finish message is sent.

        Lets a provider that buffers plain-text output (no terminal "result"
        event) flush the whole response and/or capture a session id at the end.
        Default: no-op. The core applies the returned event's session_id and
        assistant_text just like a consumed line.
        """
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
    if name in {"antigravity", "agy"}:
        from .antigravity import AntigravityProvider
        return AntigravityProvider()
    raise SystemExit(
        f"Unknown BRIDGE_PROVIDER: {name!r} (expected claude|codex|gemini|antigravity)"
    )
