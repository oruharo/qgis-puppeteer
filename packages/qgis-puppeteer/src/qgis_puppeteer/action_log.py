"""操作記録 ``actions.jsonl`` の 1 行の形（ADR-0002 §13 / #8）。

pytest の診断バンドル（`pytest_qgis_puppeteer._action_recorder`）と MCP gateway の
操作記録（`gateways.recording`）が同じ形で書く。同じ道具で読めるように、型と
直列化はここに 1 つだけ置く（pytest-qgis-puppeteer は qgis-puppeteer に依存する
ので、この向きなら両方から使える）。

## 1 行の形（schema_version 2）

- ``ts``: ISO-8601（マイクロ秒まで、ローカル時刻）
- ``duration_ms``: 呼び出しの所要時間
- ``command``: 呼んだもの。pytest では Worker のコマンド名、MCP ではツール名
  （大半は同じ ``qgis_*``）
- ``params``: 引数（直列化できない値は ``"<unrepresentable>"``）
- ``result``: 成功時の戻り値（MCP では長いものを切り詰める）
- ``error``: 失敗時の ``{"type", "message"}``、成功時は None
- ``instance``: 送り先の instance（既定なら None）
- ``step_path``: 論理ステップの入れ子（list of str）
- ``screenshot_path``: 操作後のスクリーンショット（pytest の B5b、無ければ None）
- ``source``: ``"mcp"`` など記録した側。pytest の記録には付かない（旧形式と同じ）

``source`` は任意の追加フィールドなので、schema_version 2 のまま。未知の
フィールドを無視する reader はそのまま読める。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

SCHEMA_VERSION = 2


@dataclass(frozen=True)
class ActionRecord:
    """操作記録の 1 行（``actions.jsonl``）。"""

    ts: str
    duration_ms: float
    command: str
    params: Any
    result: Any
    error: dict[str, str] | None
    instance: str | None
    step_path: tuple[str, ...]
    screenshot_path: str | None = None
    schema_version: int = SCHEMA_VERSION
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "ts": self.ts,
            "duration_ms": self.duration_ms,
            "command": self.command,
            "params": self.params,
            "result": self.result,
            "error": self.error,
            "instance": self.instance,
            "step_path": list(self.step_path),
            "screenshot_path": self.screenshot_path,
        }
        if self.source is not None:
            d["source"] = self.source
        return d

    def to_json_line(self) -> str:
        """JSONL の 1 行（改行付き）。日本語のステップ名などはそのまま書く。"""
        return json.dumps(self.to_dict(), ensure_ascii=False) + "\n"


def now_iso() -> str:
    """マイクロ秒精度の ISO-8601（タイムゾーン無し、ローカル時刻）。"""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")


def safe_jsonify(value: Any) -> Any:
    """``json.dumps`` できる形に best-effort で変換する。

    ``default=str`` で Path / datetime / Enum 等は文字列になる。循環参照などで
    変換できなければ ``"<unrepresentable>"``。
    """
    if value is None:
        return None
    try:
        return json.loads(json.dumps(value, default=str, ensure_ascii=False))
    except (TypeError, ValueError, RecursionError):
        return "<unrepresentable>"


SECRET_PLACEHOLDER = "***"


def redact_secret_value(params: Any, result: Any) -> Any:
    """パスワード欄に入れた ``value`` を、記録に残す前に伏せる（#10）。

    相手がパスワード欄かは、値を入れる前には分からない。Worker は相手が
    パスワード欄なら結果に ``masked: true``（または widget の要約に
    ``masked: true``）を付けるので、それを見て ``params["value"]`` を
    `SECRET_PLACEHOLDER` に置き換える。``params`` は書き換えずに新しい dict を返す。
    """
    if not isinstance(params, dict) or "value" not in params or not _reports_masked(result):
        return params
    return {**params, "value": SECRET_PLACEHOLDER}


def _reports_masked(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("masked") is True:
        return True
    widget = result.get("widget")
    if isinstance(widget, dict) and widget.get("masked") is True:
        return True
    # MCP は失敗を tool error の本文 {"error": {"code", "message", "details"}} で返す
    # （#23）。パスワード欄の失敗では masked がその details の下にある
    error = result.get("error")
    return isinstance(error, dict) and _reports_masked(error.get("details"))


def format_error(exc: BaseException) -> dict[str, str]:
    """例外を ``{"type", "message"}`` にする。"""
    try:
        message = str(exc)
    except Exception:  # noqa: BLE001 - __str__ が壊れた例外でも記録は通す
        message = "<unrepresentable>"
    return {"type": type(exc).__name__, "message": message}
