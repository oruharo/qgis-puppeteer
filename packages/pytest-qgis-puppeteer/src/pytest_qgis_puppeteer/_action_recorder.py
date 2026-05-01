"""Action recorder for E2E diagnostic bundle (ADR-0002 §13 / §15).

`E2EAutomationClient.call(...)` の呼び出しを timeline として記録し、テスト失敗時に
``actions.jsonl`` として diagnostic bundle に dump する。``step()`` context manager
で手作業の論理ステップ（例: "ログイン", "feature 検索"）を hierarchically に
ネストでき、各 action に ``step_path`` を付与することで失敗解析の文脈が読みやすく
なる（ADR-0002 §15）。

## 設計判断

- **どの層で wrap するか**: ``E2EAutomationClient.call(...)`` を 1 箇所で wrap。
  high-level helper（``click_widget`` / ``set_widget_value`` / locator chain 等）は
  内部で ``call()`` 経由なので、wrap は 1 箇所で漏れなく拾える。
- **thread-local の step stack**: pytest test は通常 main thread だが、user code が
  thread を起こしても各 thread の step が混ざらないよう ``threading.local`` で持つ。
  ただし actions list 自体は全 thread の append を受ける（jsonl は時系列の単一系列）。
- **serialise 失敗の handling**: params/result に渡された値は best-effort で
  ``json.dumps(default=str)`` で文字列化する。循環参照や `bytes` のような
  serialise 不能オブジェクトは ``"<unrepresentable>"`` に置換して 1 行 1 record の
  jsonl 不変条件を保つ。
- **memory 上限**: 既定で最大 5000 actions まで（``max_actions``）。それを超えると
  古い順に捨てる（drop-oldest）。長時間 session で OOM するリスクを抑える。

## API

```python
recorder = ActionRecorder()
recorder.record(command="qgis_click_widget", params={...}, result={"ok": True},
                error=None, instance="A", duration_ms=12.3)
with recorder.step("ログイン"):
    recorder.record(...)  # step_path=["ログイン"]
recorder.dump_jsonl(Path("actions.jsonl"))
recorder.clear()  # 次テストの前に呼ぶ
```
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("pytest_qgis_puppeteer._action_recorder")

# 既定の max_actions。1 テストの想定 actions は 50〜200 程度なので、5000 あれば
# step 系 / poll 系（wait_for_*）込みでもまず溢れない。溢れた場合は drop-oldest。
DEFAULT_MAX_ACTIONS = 5000


@dataclass(frozen=True)
class ActionRecord:
    """1 つの action 記録（`actions.jsonl` の 1 行）。

    Schema version 2（ADR-0002 §13.x、Phase B5a + B5b）:

    - ``ts``: ISO-8601 timestamp（秒精度 + microsecond）
    - ``duration_ms``: call の所要時間（ms、負値はあり得ない）
    - ``command``: Worker handler 名（``qgis_*``）
    - ``params``: 呼び出しパラメータ（serialise 不能は ``<unrepresentable>``）
    - ``result``: 成功時の戻り値（同上）
    - ``error``: 失敗時の ``{"type", "message"}``、成功時は None
    - ``instance``: routing 先 instance_id、Hub 既定なら None
    - ``step_path``: ``step()`` context のネストを表す list of str
    - ``screenshot_path``: action 後の screenshot ファイルパス（B5b、未設定なら None）
    - ``schema_version``: 互換性追跡用（B5a=1 / B5b=2）

    Schema 2 は schema 1 に ``screenshot_path`` を追加しただけの非破壊変更。
    schema 1 reader は未知フィールドを無視すれば 2 を読める。
    """

    ts: str
    duration_ms: float
    command: str
    params: Any
    result: Any
    error: dict[str, str] | None
    instance: str | None
    step_path: tuple[str, ...]
    screenshot_path: str | None = None
    schema_version: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {
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


class ActionRecorder:
    """テスト中の Worker call を timeline として蓄積する recorder。

    1 つの test で 1 instance を使う想定（``conftest.py`` で test 前に
    ``clear()`` する fixture を組む）。複数 thread から ``record()`` されても
    ``actions`` list は lock で保護する。
    """

    def __init__(self, *, max_actions: int = DEFAULT_MAX_ACTIONS) -> None:
        self._max_actions = max(1, int(max_actions))
        self._actions: list[ActionRecord] = []
        self._lock = threading.Lock()
        # step stack は thread-local。pytest test は main thread で動くのが通常だが、
        # ユーザコードが thread を立てても step を継承しない方が事故が少ない
        # （別 thread の step name が混ざるよりは、明示的に step を引き渡す方が安全）。
        self._tls = threading.local()
        # B5b: per-action screenshot 保存先。None なら capture しない（既存互換）。
        # plugin 側 fixture が test 開始時に `_pending/<test_id>/` を作って set し、
        # 失敗時に bundle dir へ rename、成功時には削除する想定。
        self._screenshot_dir: Path | None = None

    # ------------------------------------------------------------
    # B5b: screenshot_dir
    # ------------------------------------------------------------

    @property
    def screenshot_dir(self) -> Path | None:
        """per-action screenshot の保存先。None なら capture 無効。"""
        return self._screenshot_dir

    def set_screenshot_dir(self, path: Path | None) -> None:
        """per-action screenshot の保存先を設定する（None で無効化）。

        path が non-None なら、各 ``record()`` の前後で `automation_client` 側が
        screenshot を撮って ``<path>/<seq>.png`` に保存する想定（実際の撮影と
        保存は client 側責務、recorder は保存先の合意点を保つだけ）。
        """
        self._screenshot_dir = path

    def next_screenshot_path(self) -> Path:
        """次の record に紐づく screenshot ファイル path を返す。

        seq 番号は **次に append される record の index**（= 現在の actions 件数）。
        client 側が record() の **前に** screenshot を撮って path を取得し、
        record() 引数の ``screenshot_path`` に渡すフローを想定。

        ``screenshot_dir`` が None の場合は呼び出しエラー。
        """
        if self._screenshot_dir is None:
            raise RuntimeError("screenshot_dir is not set")
        with self._lock:
            seq = len(self._actions)
        # 4 桁ゼロ埋め: 通常 200 actions / test なので 0000〜0199 で並ぶ。
        # max_actions=5000 を超えても 4 桁で足りる（drop-oldest なので桁あふれは無い）。
        return self._screenshot_dir / f"{seq:04d}.png"

    # ------------------------------------------------------------
    # step stack（thread-local）
    # ------------------------------------------------------------

    def _stack(self) -> list[str]:
        """thread-local の step stack を返す（必要なら lazy 初期化）。"""
        stack = getattr(self._tls, "stack", None)
        if stack is None:
            stack = []
            self._tls.stack = stack
        return stack

    def current_step_path(self) -> tuple[str, ...]:
        """現 thread の step stack を tuple で返す（現状確認用）。"""
        return tuple(self._stack())

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        """論理ステップを stack に push し、context exit で pop。

        Args:
            name: ステップ名。空文字列も許容（caller の自由）。
        """
        stack = self._stack()
        stack.append(name)
        try:
            yield
        finally:
            # 例外で抜ける際も必ず pop。push と pop の対応が崩れると path が
            # 永続的にずれるので、本 method は **stack を直接いじるが pop は
            # 強制実行**する設計（matched ペアであることを契約とする）。
            if stack and stack[-1] == name:
                stack.pop()
            else:
                # 不整合（手動で stack を弄った 等）。warn して best-effort で復旧。
                logger.warning(
                    "step stack inconsistency: expected top=%r, actual stack=%r",
                    name,
                    list(stack),
                )
                if stack:
                    stack.pop()

    # ------------------------------------------------------------
    # action 記録
    # ------------------------------------------------------------

    def record(
        self,
        *,
        command: str,
        params: Any,
        result: Any = None,
        error: BaseException | None = None,
        instance: str | None = None,
        duration_ms: float = 0.0,
        ts: str | None = None,
        screenshot_path: str | None = None,
    ) -> ActionRecord:
        """1 action を記録する。

        ``params`` / ``result`` は serialise 不能なら ``<unrepresentable>`` に置換。
        ``error`` が non-None なら ``{"type": ..., "message": ...}`` を error フィールドに
        詰め、result は None 扱い。

        ``screenshot_path`` は B5b の per-action screenshot 保存パス（client 側が
        record の **前に** ``next_screenshot_path()`` で取って撮影し、ここに渡す）。

        max_actions を超えたら oldest を 1 件捨てて新規を append。
        """
        record = ActionRecord(
            ts=ts or _now_iso(),
            duration_ms=max(0.0, float(duration_ms)),
            command=str(command),
            params=_safe_jsonify(params),
            result=_safe_jsonify(result) if error is None else None,
            error=_format_error(error) if error is not None else None,
            instance=str(instance) if instance is not None else None,
            step_path=tuple(self._stack()),
            screenshot_path=screenshot_path,
        )
        with self._lock:
            if len(self._actions) >= self._max_actions:
                # drop-oldest。N=5000 なら test 末まで保つはずだが、保険として。
                del self._actions[0]
            self._actions.append(record)
        return record

    # ------------------------------------------------------------
    # 取得 / 永続化
    # ------------------------------------------------------------

    def actions(self) -> tuple[ActionRecord, ...]:
        """記録済み action の snapshot（tuple なので外部から mutate されない）。"""
        with self._lock:
            return tuple(self._actions)

    def clear(self) -> None:
        """記録をリセット。各 test の冒頭で呼ぶ想定。

        step stack は **clear しない**。fixture の autouse で test 開始前に呼ぶ
        運用なら stack は空のはず（前 test が step 内で例外で死んでも contextmanager
        が pop する）。明示的に stack を空にしたい場合は ``reset_step_stack()`` を使う。
        """
        with self._lock:
            self._actions.clear()

    def reset_step_stack(self) -> None:
        """現 thread の step stack を強制クリア（debug / 緊急時用）。

        既存の list を **in-place で clear する**（``self._tls.stack = []`` で
        再代入すると、進行中の ``step()`` context が握っている古い参照と乖離して
        push/pop の整合性が取れなくなるため）。
        """
        stack = self._stack()
        stack.clear()

    def dump_jsonl(self, path: Path) -> None:
        """記録を JSONL（1 行 1 record）で書き出す。

        ファイル自体は parent dir を作って overwrite する。空 actions でも空 file を
        生成する（呼び出し側の「ファイルが存在する = 成功した dump」と分かりやすく）。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            actions = list(self._actions)
        with path.open("w", encoding="utf-8") as f:
            for rec in actions:
                # ensure_ascii=False で日本語 step 名等もそのまま読める。
                f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")


# ============================================================
# 内部 helper
# ============================================================


def _now_iso() -> str:
    """秒 + microsecond 精度の ISO-8601。タイムゾーン情報は付けない（local clock）。

    ADR-0002 §13 の meta.json と整合: ``YYYY-MM-DDTHH:MM:SS.ffffff``。
    """
    # time.strftime で microsecond は出ないので datetime を使う
    from datetime import datetime  # 局所 import で起動コスト回避

    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")


def _safe_jsonify(value: Any) -> Any:
    """``json.dumps`` 可能な形に best-effort で変換する。

    成功なら原値を返す（dict/list/scalar）。失敗（循環 / 不能型）なら
    ``"<unrepresentable>"`` を返す。``json.dumps(default=str)`` で多くの型
    （Path / datetime / Enum 等）はカバーされる前提。
    """
    if value is None:
        return None
    try:
        # round-trip で「json で書ける形」に変換。default=str で複雑な型を救う。
        return json.loads(json.dumps(value, default=str, ensure_ascii=False))
    except (TypeError, ValueError, RecursionError):
        return "<unrepresentable>"


def _format_error(exc: BaseException) -> dict[str, str]:
    """例外を ``{"type", "message"}`` の dict に整形する。"""
    try:
        message = str(exc)
    except Exception:  # noqa: BLE001 - __str__ が壊れた exception でも record は通す
        message = "<unrepresentable>"
    return {
        "type": type(exc).__name__,
        "message": message,
    }


def _ensure_recorder_attached(client: Any, recorder: ActionRecorder) -> None:
    """``E2EAutomationClient`` 互換オブジェクトに recorder を attach する helper。

    `client.set_recorder(recorder)` を呼ぶだけ。古い client（recorder 未対応）の
    場合は no-op に倒す（duck-typing 安全）。
    """
    setter = getattr(client, "set_recorder", None)
    if callable(setter):
        setter(recorder)


__all__ = [
    "ActionRecord",
    "ActionRecorder",
    "DEFAULT_MAX_ACTIONS",
]


# 単独実行（debug 用、ad-hoc verification）。pytest 経路では呼ばれない。
if __name__ == "__main__":  # pragma: no cover
    r = ActionRecorder()
    with r.step("demo"):
        r.record(command="demo", params={"x": 1}, result={"ok": True})
    for a in r.actions():
        print(a.to_dict())
    # smoke print
    _ = time.monotonic()
