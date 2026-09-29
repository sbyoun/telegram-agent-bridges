"""Claude Code CLI provider."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .base import Command, LineEvent, Provider, SessionInfo, excluded_cwds, is_excluded

# 세션 인덱스 파일 버전. 레코드 구조가 바뀌면 올려서 전체 재스캔을 유도한다.
_INDEX_VERSION = 1


def _index_path() -> Path:
    state_dir = Path(os.getenv("BRIDGE_STATE_DIR", "./state")).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "claude-sessions.json"


def _cwd_to_dir_prefix(cwd: str) -> str:
    """Claude Code는 cwd의 '/'를 '-'로 바꿔 프로젝트 디렉토리 이름을 만든다.
    ('/tmp/x' -> '-tmp-x'). 제외 cwd를 디렉토리 이름 접두사로 바꿔 두면
    파일을 열기는커녕 디렉토리 진입 자체를 건너뛸 수 있다."""
    return cwd.rstrip("/").replace("/", "-")


def _dir_excluded(dir_name: str, excluded: tuple[str, ...]) -> bool:
    for base in excluded:
        prefix = _cwd_to_dir_prefix(base)
        if dir_name == prefix or dir_name.startswith(prefix + "-"):
            return True
    return False


class ClaudeProvider(Provider):
    name = "claude"
    display = "Claude"
    default_bin = "claude"
    env_prefix = "CLAUDE"

    def __init__(self) -> None:
        self._index: dict | None = None

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
            errors = payload.get("errors") or []
            if any("No conversation found with session ID" in str(error) for error in errors):
                ev.clear_anchor = True
        return ev

    # ---- session index ----
    #
    # ~/.claude/projects/**/*.jsonl 을 매번 전량 파싱하면 수십 초가 걸린다
    # (파일 1.5만 개, 900MB). 목록에 필요한 건 세션ID·cwd·제목·최근 사용 시각뿐이므로
    # 그것만 state/claude-sessions.json 에 인덱스로 유지한다.
    #
    # - 갱신 시각은 파일 mtime (내용을 읽지 않아도 정확)
    # - 제목(custom-title / ai-title 이벤트)은 턴마다 반복 append 되므로,
    #   바뀐 파일은 지난번 읽은 오프셋부터 꼬리만 읽어도 잡힌다
    # - 제외 cwd(/tmp, loop-engine 등)는 디렉토리 이름 단계에서 건너뛴다

    def _load_index(self) -> dict:
        if self._index is not None:
            return self._index
        path = _index_path()
        try:
            data = json.loads(path.read_text())
            if data.get("version") == _INDEX_VERSION and isinstance(data.get("files"), dict):
                self._index = data
                return data
        except (OSError, json.JSONDecodeError):
            pass
        self._index = {"version": _INDEX_VERSION, "files": {}}
        return self._index

    def _save_index(self, index: dict) -> None:
        path = _index_path()
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(index, ensure_ascii=False))
            os.replace(tmp, path)
        except OSError:
            pass

    @staticmethod
    def _scan_tail(path: Path, offset: int, rec: dict) -> int:
        """offset부터 파일 끝까지 읽어 rec의 session_id/cwd/제목을 갱신한다.
        반환값은 다음에 이어 읽을 오프셋(마지막 완전한 줄 끝)."""
        with path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
        if not chunk:
            return offset
        # 쓰는 중인 파일의 잘린 마지막 줄은 다음 호출에서 다시 읽는다.
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return offset
        for raw in chunk[:cut].split(b"\n"):
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            sid = str(event.get("sessionId") or "").strip()
            if sid:
                rec["session_id"] = sid
            if not rec.get("cwd") and event.get("cwd"):
                rec["cwd"] = str(event["cwd"])
            custom = str(event.get("customTitle") or "").strip()
            if custom:
                rec["custom_title"] = custom
            ai = str(event.get("aiTitle") or event.get("aiTitleText") or "").strip()
            if ai:
                rec["ai_title"] = ai
        return offset + cut + 1

    def _refresh_index(self) -> dict:
        index = self._load_index()
        files: dict = index["files"]
        excluded = excluded_cwds(self.env_prefix)
        projects_dir = Path.home() / ".claude" / "projects"
        seen: set[str] = set()
        dirty = False

        if projects_dir.exists():
            for project_dir in projects_dir.iterdir():
                if not project_dir.is_dir() or _dir_excluded(project_dir.name, excluded):
                    continue
                for path in project_dir.glob("*.jsonl"):
                    if path.stem.startswith("agent-"):
                        continue
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    key = str(path.relative_to(projects_dir))
                    seen.add(key)
                    rec = files.get(key)
                    if rec and rec.get("size") == st.st_size and rec.get("mtime_ns") == st.st_mtime_ns:
                        continue
                    if rec is None:
                        rec = {"session_id": path.stem, "cwd": "", "custom_title": "",
                               "ai_title": "", "excluded": False, "offset": 0}
                        files[key] = rec
                    dirty = True
                    rec["size"] = st.st_size
                    rec["mtime_ns"] = st.st_mtime_ns
                    rec["updated_ms"] = int(st.st_mtime * 1000)
                    if rec.get("excluded"):
                        continue
                    offset = rec.get("offset") or 0
                    if offset > st.st_size:      # 파일이 새로 쓰였다(truncate)
                        offset = 0
                    try:
                        rec["offset"] = self._scan_tail(path, offset, rec)
                    except OSError:
                        continue
                    if is_excluded(rec.get("cwd", ""), excluded):
                        rec["excluded"] = True

        stale = [k for k in files if k not in seen]
        for k in stale:
            del files[k]
            dirty = True

        if dirty:
            self._save_index(index)
        return index

    def list_sessions(self, limit: int | None = None) -> list[SessionInfo]:
        index = self._refresh_index()
        merged: dict[str, SessionInfo] = {}
        for rec in index["files"].values():
            if rec.get("excluded"):
                continue
            custom = rec.get("custom_title") or ""
            ai = rec.get("ai_title") or ""
            if not custom and not ai:
                continue     # 제목 없는 세션은 목록에 올리지 않는다
            info = SessionInfo(
                id=rec["session_id"],
                name=custom or ai,
                cwd=rec.get("cwd", ""),
                updated_ms=int(rec.get("updated_ms") or 0),
                custom_title=bool(custom),
            )
            prev = merged.get(info.id)
            if prev is None or info.updated_ms > prev.updated_ms:
                merged[info.id] = info

        sessions = sorted(merged.values(), key=lambda s: (0 if s.custom_title else 1, -s.updated_ms))
        return sessions if limit is None else sessions[:limit]
