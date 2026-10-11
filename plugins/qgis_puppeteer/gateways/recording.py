"""MCP gateway の操作記録（#8）。

AI エージェントが MCP 経由で QGIS を操作して手順を探り、安定した手順を pytest の
E2E テストに書き起こす、という使い方のための元資料を残す。各ツール呼び出しを
pytest の診断バンドルの ``actions.jsonl`` と同じ形（`qgis_puppeteer.action_log`）
で JSONL に追記する。テストコードの自動生成（Codegen）はしない（ADR-0002 §16）。

## 使い方

- エージェントが ``qgis_record_start(name)`` → 操作 → ``qgis_record_step(name)`` で
  区切る → ``qgis_record_stop()``。開始・終了の結果に保存先のパスが入る
- 環境変数 ``QPUPPETEER_MCP_RECORD=1`` なら gateway の起動時から記録する
  （名前は ``session``）

保存先は ``QPUPPETEER_MCP_RECORD_DIR``（既定 ``<TEMP>/qgis_puppeteer/recordings``）
の下の ``<日時>_<name>.jsonl``。**パスはエージェントに選ばせない。** 任意の
パスを受け付けると、エージェントに「どこにでも書ける」道具を渡すことになる。

## 記録の中身

- ``command`` はツール名、``source`` は ``"mcp"``
- ``params`` は ``None`` を除いた引数（``qgis_execute_python`` のコードも入る）
- ``result`` は本文の JSON。長いもの（UI スナップショット等）は先頭だけ残して
  ``{"truncated": true, "chars": N, "head": "..."}`` にする。画像は中身を書かず
  ``{"image": "image/png", "bytes": N}``
- tool error は ``error`` に ``{"type": "tool_error", "message": 本文全体}``
- ``instance`` は明示の ``instance`` 引数、無ければ ``qgis_use_instance`` で選んだもの
- ``qgis_record_*`` 自身は記録しない

1 行ずつ追記して flush するので、gateway が落ちてもそこまでの記録は残る。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from qgis_puppeteer.action_log import ActionRecord, now_iso, safe_jsonify

logger = logging.getLogger(__name__)

ENV_RECORD = "QPUPPETEER_MCP_RECORD"
ENV_RECORD_DIR = "QPUPPETEER_MCP_RECORD_DIR"
SOURCE = "mcp"
# 結果をこれ以上の文字数なら切り詰める（エラーは切らない）
MAX_RESULT_CHARS = 2000
_NAME_MAX = 64


def default_record_dir() -> Path:
    override = os.environ.get(ENV_RECORD_DIR)
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "qgis_puppeteer" / "recordings"


def safe_name(name: str) -> str:
    """ファイル名に使える形にする（英数・``-``・``_`` 以外は ``_``）。"""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip())[:_NAME_MAX].strip("_")
    return cleaned or "recording"


class McpRecorder:
    """1 本ずつ記録する。開始中でなければ ``record()`` は何もしない。"""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory
        self._lock = threading.Lock()
        self._file: TextIO | None = None
        self._path: Path | None = None
        self._count = 0
        self._step: tuple[str, ...] = ()

    @property
    def directory(self) -> Path:
        return self._directory if self._directory is not None else default_record_dir()

    @property
    def active(self) -> bool:
        return self._file is not None

    def start(self, name: str) -> dict[str, Any]:
        """新しい記録を始める。記録中なら、それを閉じてから始める。"""
        with self._lock:
            previous = self._close_locked()
            directory = self.directory
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = directory / f"{stamp}_{safe_name(name)}.jsonl"
            n = 1
            while path.exists():  # 同じ秒に同じ名前で始めた場合
                n += 1
                path = directory / f"{stamp}_{safe_name(name)}_{n}.jsonl"
            self._file = path.open("a", encoding="utf-8")
            self._path = path
            self._count = 0
            self._step = ()
        logger.info("MCP recording started: %s", path)
        result: dict[str, Any] = {"recording": True, "path": str(path)}
        if previous is not None:
            result["previous"] = previous
        return result

    def step(self, name: str) -> dict[str, Any]:
        """以降の記録の ``step_path`` を ``[name]`` にする（空文字で解除）。"""
        with self._lock:
            self._step = (name,) if name else ()
            return {"recording": self._file is not None, "step": name or None}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            closed = self._close_locked()
        if closed is None:
            return {"recording": False}
        logger.info("MCP recording stopped: %s (%d actions)", closed["path"], closed["actions"])
        return {"recording": False, **closed}

    def record(
        self,
        *,
        command: str,
        params: dict[str, Any],
        result: Any,
        is_error: bool,
        instance: str | None,
        duration_ms: float,
    ) -> None:
        if self._file is None:
            return
        rendered = summarize_result(result)
        rec = ActionRecord(
            ts=now_iso(),
            duration_ms=max(0.0, round(duration_ms, 3)),
            command=command,
            params=safe_jsonify({k: v for k, v in params.items() if v is not None}),
            result=None if is_error else rendered,
            error={"type": "tool_error", "message": _text_of(result)} if is_error else None,
            instance=instance,
            step_path=self._step,
            source=SOURCE,
        )
        line = rec.to_json_line()
        with self._lock:
            if self._file is None:
                return
            try:
                self._file.write(line)
                self._file.flush()
                self._count += 1
            except OSError:
                logger.exception("Could not write the MCP recording to %s", self._path)

    def _close_locked(self) -> dict[str, Any] | None:
        if self._file is None:
            return None
        try:
            self._file.close()
        except OSError:
            logger.exception("Could not close the MCP recording %s", self._path)
        closed = {"path": str(self._path), "actions": self._count}
        self._file = None
        self._path = None
        self._count = 0
        self._step = ()
        return closed


def _text_of(result: Any) -> str:
    """ツール結果の本文テキスト（TextContent をつないだもの）。"""
    if isinstance(result, str):
        return result
    parts = []
    for content in getattr(result, "content", None) or ():
        text = getattr(content, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def summarize_result(result: Any) -> Any:
    """記録に残す結果。JSON はそのまま、長いものは切り詰め、画像は大きさだけ。"""
    contents = getattr(result, "content", None)
    images = []
    if contents is not None:
        for content in contents:
            if getattr(content, "type", None) == "image":
                data = getattr(content, "data", "") or ""
                images.append(
                    {
                        "image": getattr(content, "mime_type", None),
                        "bytes": len(data) * 3 // 4,  # base64 → バイト数の目安
                    }
                )
    text = _text_of(result)
    if len(text) > MAX_RESULT_CHARS:
        body: Any = {"truncated": True, "chars": len(text), "head": text[:MAX_RESULT_CHARS]}
    else:
        try:
            body = json.loads(text)
        except ValueError:
            body = text
    if images:
        return {"body": body, "images": images}
    return body
