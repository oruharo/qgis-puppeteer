"""`qgis_puppeteer.AutomationClient` の E2E 用同期ラッパ。

pytest fixture は sync で書くのが素直なので、非同期 API を薄く同期化する。
内部で単一の asyncio event loop を保持し、各メソッドで `run_until_complete`
を呼ぶ形（session 全期間で 1 ループ再利用なので connect/call/close の文脈が
切れない）。

## 提供 API

- `connect()` / `close()` / `list_instances()` / `wait_for_worker()` / `call()`
- 便利メソッド（ADR-0002 §2）: `execute_python` / `snapshot_ui` /
  `click_widget` / `set_widget_value` / `screenshot` / canvas extent /
  選択フィーチャ / `wait_for_*` の同期ラッパ。Worker 側のコマンド名は
  `qgis_puppeteer.gateways.mcp` の `@mcp.tool()` 群と 1:1 対応する。

`wait_for_*` は Worker 側に専用 handler が無いため、`snapshot_ui(
include_main_window=True)` を `poll_interval_s` 間隔で繰り返し呼んで条件成立
を判定する。timeout 時は `TimeoutError` を上げる（ADR-0002 §11 の Locator
推奨 250ms に揃える）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from qgis_puppeteer.client import (
    AutomationClient,
    ConfirmationRequiredError,
    InstanceInfo,
    NonSerializableResultError,
    WorkerCodeError,
)
from qgis_puppeteer.protocol import Role
from qgis_puppeteer.selector_match import (
    record_matches_selector,
    resolve_with_index,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pytest_qgis_puppeteer._action_recorder import ActionRecorder

logger = logging.getLogger("pytest_qgis_puppeteer.automation_client")


class ModalBlockedError(RuntimeError):
    """プロジェクト読み込みがモーダルに遮られて進まない時に上がる例外。

    例えばログインダイアログ等のモーダルが開いていると、ユーザが応答する
    まで QGIS 側のプロジェクト読み込みが止まる。`wait_for_project_loaded`
    がその間 `list_layers` を空で受け続けて timeout する代わりに、本例外で
    早期に「モーダル block である」ことを呼び出し側に通知する。

    `modal` 属性に `snapshot_ui` から抜き出したモーダル辞書を持つので、
    テスト側で原因のダイアログを特定して diagnostics に残せる。
    """

    def __init__(self, message: str, *, modal: dict[str, Any]) -> None:
        super().__init__(message)
        self.modal = modal


@dataclass(frozen=True)
class ExecResult:
    """``execute_python_detailed`` の戻り値（handler 応答の typed ビュー）。

    値モード ``execute_python`` と違い **コード失敗でも raise しない**。成否や
    stdout / 非直列化状態を自分で検査したいとき用。``raw`` に元 dict を保持する。
    """

    success: bool
    result_set: bool
    value: Any
    result_serializable: bool
    stdout: str
    stderr: str
    error: str | None = None
    traceback: str | None = None
    result_type: str | None = None
    result_repr: str | None = None
    requires_confirmation: bool = False
    permission_level: str | None = None
    risk_level: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Any) -> ExecResult:
        """handler の結果 dict（または非 dict）から ExecResult を構築する。"""
        d = payload if isinstance(payload, dict) else {}
        return cls(
            success=bool(d.get("success", False)),
            result_set=bool(d.get("result_set", False)),
            value=d.get("result"),
            result_serializable=bool(d.get("result_serializable", True)),
            stdout=str(d.get("stdout", "")),
            stderr=str(d.get("stderr", "")),
            error=d.get("error"),
            traceback=d.get("traceback"),
            result_type=d.get("result_type"),
            result_repr=d.get("result_repr"),
            requires_confirmation=bool(d.get("requires_confirmation", False)),
            permission_level=d.get("permission_level"),
            risk_level=d.get("risk_level"),
            raw=dict(d),
        )


def _raise_for_execute_result(result: Any) -> None:
    """``execute_python`` 結果 dict を検査し、コード失敗なら例外へ変換する。

    - ``requires_confirmation`` → :class:`ConfirmationRequiredError`
    - その他の ``success=False`` → :class:`WorkerCodeError`

    非直列化（``result_serializable=False``）の判定は値モード側で別途行う
    （detailed モードでは raise しないため、ここには含めない）。
    ``success`` が True、または dict でない戻り値は no-op。
    """
    if not isinstance(result, dict) or result.get("success", True):
        return
    if result.get("requires_confirmation"):
        raise ConfirmationRequiredError(result.get("risk_level"))
    raise WorkerCodeError(
        result.get("error") or result.get("message"),
        result.get("traceback"),
        stdout=result.get("stdout"),
        stderr=result.get("stderr"),
    )


class E2EAutomationClient:
    """AutomationClient の pytest 向け同期ラッパ。

    内部で `asyncio.new_event_loop()` を 1 つ持ち、接続・close・call の全てを
    同じループ上で完結させる。fixture の session scope と相性が良い（接続を
    切らずに複数テスト間で再利用できる）。
    """

    def __init__(self, url: str, *, origin: str = "http://localhost") -> None:
        self._url = url
        self._origin = origin
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: AutomationClient | None = None
        # use_instance() で sticky に設定するデフォルト instance。``call(...)`` の
        # `instance=None` 時にこちらが使われる。multi-instance テスト
        # （`spawn_qgis()` 併用 / `@pytest.mark.fresh_qgis` 等）で routing 制御に使う。
        self._default_instance: str | None = None
        # ADR-0002 §13 / B5a: 各 ``call(...)`` を timeline に記録する recorder。
        # session fixture が test 開始前に attach、failure 時に jsonl で dump する。
        # None なら no-op（既存テスト互換、後付け attach）。
        self._recorder: ActionRecorder | None = None
        # B5b: per-action screenshot capture 中フラグ。True の間 call() は recorder
        # への記録を **skip** する（screenshot 取得自体は qgis_screenshot を内部で
        # 呼ぶため、recorder に二重記録すると jsonl が膨らむ + 再帰条件が複雑になる）。
        self._capture_in_progress = False

    # ------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------

    def connect(self) -> None:
        """Hub に WebSocket 接続する。二重呼び出しは no-op。"""
        if self._client is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._client = AutomationClient(
            url=self._url,
            origin=self._origin,
            role=Role.AUTOMATION_CLIENT,
        )
        self._loop.run_until_complete(self._client.connect())
        logger.info("E2EAutomationClient connected to %s", self._url)

    def close(self) -> None:
        """Hub への接続と event loop を閉じる。二重 close は安全。"""
        if self._client is not None and self._loop is not None:
            try:
                self._loop.run_until_complete(self._client.close())
            except Exception:  # noqa: BLE001 - best-effort on teardown
                logger.warning("close() raised during teardown", exc_info=True)
            finally:
                self._loop.close()
                self._client = None
                self._loop = None

    # ------------------------------------------------------------
    # 基本 API
    # ------------------------------------------------------------

    def list_instances(self) -> list[InstanceInfo]:
        """Hub が管理している Worker 一覧を取得する。"""
        client, loop = self._require_connected()
        return loop.run_until_complete(client.list_instances())

    def call(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        *,
        instance: str | None = None,
    ) -> Any:
        """Worker コマンドを 1 つ呼ぶ同期ラッパ。

        戻り値は Worker 側 handler の result をそのまま返す（dict/list/str/None）。
        `RequestError` / `TimeoutError` 等の例外は素通しするので、pytest の
        `assert`・`pytest.raises` でそのまま拾える。

        ``instance`` 解決の優先順位:

        1. 明示的に渡された ``instance`` 引数（per-call override）
        2. ``use_instance(...)`` で設定した sticky な default
        3. ``None``（Hub の default routing に委ねる）
        """
        client, loop = self._require_connected()
        effective_instance = instance if instance is not None else self._default_instance
        recorder = self._recorder
        actual_params = params or {}
        # recorder が無い、または per-action screenshot 取得中の再帰呼び出しなら
        # 記録なしの素通しパス（既存 sync API と完全互換）。
        if recorder is None or self._capture_in_progress:
            return loop.run_until_complete(
                client.call(command, actual_params, instance=effective_instance)
            )

        # B5a: recorder attached の場合は所要時間と error を timeline に記録する。
        # `loop.run_until_complete` の周りを wrap するだけ（call 自体の semantics は不変）。
        # B5b: action 完了後に per-action screenshot を撮って record の screenshot_path に
        # 紐付ける（``recorder.screenshot_dir`` が設定されている場合のみ）。
        start = time.monotonic()
        try:
            result = loop.run_until_complete(
                client.call(command, actual_params, instance=effective_instance)
            )
        except BaseException as exc:
            duration_ms = (time.monotonic() - start) * 1000.0
            ss_path = self._capture_action_screenshot_safe(recorder, command=command)
            recorder.record(
                command=command,
                params=actual_params,
                error=exc,
                instance=effective_instance,
                duration_ms=duration_ms,
                screenshot_path=ss_path,
            )
            raise
        else:
            duration_ms = (time.monotonic() - start) * 1000.0
            ss_path = self._capture_action_screenshot_safe(recorder, command=command)
            recorder.record(
                command=command,
                params=actual_params,
                result=result,
                instance=effective_instance,
                duration_ms=duration_ms,
                screenshot_path=ss_path,
            )
            return result

    def _capture_action_screenshot_safe(
        self, recorder: ActionRecorder, *, command: str
    ) -> str | None:
        """B5b: action 完了直後に screenshot を撮り、bundle 相対 path を返す。

        ``recorder.screenshot_dir`` が None の場合は no-op（None 返却）。
        ``command == "qgis_screenshot"`` の場合も skip する（user が明示 screenshot を
        撮ったので per-action capture は不要、ファイルが二重に出るだけ）。
        失敗は best-effort で握り潰し、warn ログだけ出す（test 本体の失敗を上書きしない）。

        返却 path は ``"screenshots/<seq>.png"`` の **bundle dir 相対 path**。
        recorder.screenshot_dir は test 中は ``_pending/`` 配下を指すが、失敗時に
        plugin 側が ``_pending/<test>`` を ``<bundle>/screenshots`` に rename するため、
        相対 path で記録しておけば成功 / 失敗どちらでも jsonl の link が壊れない。
        """
        if recorder.screenshot_dir is None:
            return None
        if command == "qgis_screenshot":
            # ユーザ自身の screenshot 呼び出しは per-action capture をスキップ
            # （重複保存になる）。record() 側は通常通り記録される。
            return None
        try:
            out_path = recorder.next_screenshot_path()
        except RuntimeError:
            # screenshot_dir が race で None に戻った等。silent skip。
            return None
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # 再帰防止フラグを立てて screenshot を撮る。flag が立っている間 call() は
        # recorder.record() を skip する。
        self._capture_in_progress = True
        try:
            self.screenshot(output_path=str(out_path))
        except Exception:  # noqa: BLE001 - capture failure must not mask test outcome
            logger.warning(
                "per-action screenshot failed: command=%s path=%s",
                command,
                out_path,
                exc_info=True,
            )
            return None
        finally:
            self._capture_in_progress = False
        # bundle dir 相対 path として記録（rename 後も整合する）
        return f"screenshots/{out_path.name}"

    def use_instance(self, instance_id: str | None) -> None:
        """以降の ``call(...)`` で使う default instance を設定する。

        multi-instance テスト（``spawn_qgis()`` で追加 Worker を立てる、
        ``@pytest.mark.fresh_qgis`` でテスト粒度の Worker 切替 等）で、
        個々の call に ``instance=`` を渡さなくても routing が固定される。

        ``None`` を渡すと sticky 設定をクリアし、Hub の default routing に戻る。

        Args:
            instance_id: target Worker の identifier、または ``None``。

        Examples:
            >>> with spawn_qgis(...) as worker:
            ...     client.use_instance(worker.instance_id)
            ...     client.execute_python("...")  # → fresh worker に行く
            >>> client.use_instance(None)  # 元に戻す
        """
        self._default_instance = instance_id

    def get_default_instance(self) -> str | None:
        """現在の sticky default instance を返す（``use_instance`` の getter）。

        テストで一時的に default を退避・復元するときに使う。
        """
        return self._default_instance

    # ------------------------------------------------------------
    # ADR-0002 §13 / §15: action recorder + step
    # ------------------------------------------------------------

    def set_recorder(self, recorder: ActionRecorder | None) -> None:
        """各 ``call(...)`` の timeline を蓄積する recorder を attach / detach する。

        recorder が attach された状態で ``call(...)`` を呼ぶと、所要時間 / params /
        result（または error）が ``ActionRecord`` として recorder に蓄えられる。
        テスト失敗時に ``recorder.dump_jsonl(...)`` で diagnostic bundle に書き出す
        ことを想定（plugin 側 fixture が制御）。

        ``None`` を渡すと記録を停止する。これは ``call(...)`` の semantics を変えない
        no-op パスに切り替えるだけで、既存の sync API は完全互換。

        Args:
            recorder: 既存 ``ActionRecorder`` または ``None``。
        """
        self._recorder = recorder

    def get_recorder(self) -> ActionRecorder | None:
        """attach 済み recorder を返す（無い場合は None）。テストや diag dump で使う。"""
        return self._recorder

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        """論理ステップを recorder の step stack に push し、ネスト構造を timeline に残す。

        recorder が attach されていない（None）場合は **真の no-op**。step 自体は
        失敗しないので、test を書く側は recorder の有無を意識せず常に step を使える。

        Args:
            name: ステップ名（例: "ログイン"、"feature 検索"）。

        Examples:
            >>> with qgis.step("ログイン"):
            ...     qgis.locator({"object_name": "username"}).fill("alice")
            ...     qgis.locator({"object_name": "login_btn"}).click()
        """
        recorder = self._recorder
        if recorder is None:
            yield
            return
        with recorder.step(name):
            yield

    # ------------------------------------------------------------
    # 便利メソッド（基本セット）
    # ------------------------------------------------------------

    def list_layers(self, *, instance: str | None = None) -> Any:
        """`qgis_list_layers` を呼ぶ。戻り値は Worker 側 handler 形式に依存。"""
        return self.call("qgis_list_layers", {}, instance=instance)

    def wait_for_worker(
        self,
        *,
        timeout_s: float = 30.0,
        poll_interval_s: float = 0.25,
    ) -> str:
        """Worker が 1 つ以上 Hub に register するまで待つ。

        QGIS subprocess を起動した直後は Worker が register するまで数秒〜
        十数秒かかるので、`list_instances` を poll する。既定 30 秒タイムアウト。

        Returns:
            先頭 Worker の `instance_id`（単一 Worker 想定）。
        """
        deadline = time.monotonic() + timeout_s
        last_count = -1
        while time.monotonic() < deadline:
            instances = self.list_instances()
            if instances:
                logger.info(
                    "Worker ready: instance_id=%s (label=%s)",
                    instances[0].instance_id,
                    instances[0].label,
                )
                return instances[0].instance_id
            if last_count != 0:
                logger.debug("Waiting for Worker to register...")
                last_count = 0
            time.sleep(poll_interval_s)
        raise TimeoutError(
            f"No QGIS Worker registered to Hub within {timeout_s}s (url={self._url})"
        )

    # ------------------------------------------------------------
    # 便利メソッド（Worker コマンド薄ラッパ）
    # ------------------------------------------------------------

    def execute_python(self, code: str, *, instance: str | None = None) -> Any:
        """`qgis_execute_python` を呼び、コードが捕捉した **値** を返す。

        値を返したいときは Worker 側コードで **``_result`` に代入** する（明示
        規約。式の自動評価のような構文依存の魔法は持たない）::

            n = qgis.execute_python("_result = len(QgsProject.instance().mapLayers())")
            assert n == 0

        ``_result`` を代入しなければ ``None``（＝副作用専用呼び出し）。値は JSON
        直列化可能なら型を保って返る（int / str / list / dict / bool / None）。

        **fail-fast**（テストが silent に誤らないよう、失敗は必ず例外にする）:

        - コード内で例外 → :class:`WorkerCodeError`（真因の traceback が載る）
        - ``_result`` が JSON 直列化不可（QGIS layer 等）→
          :class:`NonSerializableResultError`（Worker 側で素データへ変換せよ）
        - confirm ゲート（信頼モード未設定）→ :class:`ConfirmationRequiredError`

        stdout / stderr や成否そのものを検査したい場合は
        :meth:`execute_python_detailed` を使う（そちらはコード失敗で raise しない）。

        Args:
            code: 実行する Python コード。値は ``_result`` に代入する。
            instance: 対象 instance selector（未指定は sticky / Hub default）。

        Returns:
            ``_result`` の値（JSON 直列化可能な型）。未代入なら ``None``。

        Raises:
            WorkerCodeError: コード実行が例外で失敗。
            NonSerializableResultError: ``_result`` が JSON 直列化不可。
            ConfirmationRequiredError: confirm が必要（信頼モード未設定）。
        """
        result = self.call("qgis_execute_python", {"code": code}, instance=instance)
        _raise_for_execute_result(result)
        if isinstance(result, dict) and result.get("result_serializable") is False:
            raise NonSerializableResultError(result.get("result_type"), result.get("result_repr"))
        return result.get("result") if isinstance(result, dict) else result

    def execute_python_detailed(self, code: str, *, instance: str | None = None) -> ExecResult:
        """`qgis_execute_python` を呼び、:class:`ExecResult` を返す（inspection 用）。

        :meth:`execute_python` と違い **コード失敗でも raise しない**。``.success`` /
        ``.value`` / ``.result_set`` / ``.stdout`` / ``.stderr`` / ``.traceback`` /
        ``.result_serializable`` / ``.requires_confirmation`` を自分で検査する。

        stdout が欲しい、成否を分岐したい、非直列化を許容して repr を見たい、等の
        ケース向け。接続断などプロトコル層の例外（``RequestError`` 等）はそのまま
        伝播する（コードの実行結果とは別レイヤのため）。
        """
        result = self.call("qgis_execute_python", {"code": code}, instance=instance)
        return ExecResult.from_payload(result)

    def snapshot_ui(
        self,
        *,
        max_depth: int = 8,
        include_invisible: bool = False,
        include_main_window: bool = False,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """`qgis_snapshot_ui` を呼ぶ。戻り値は `ui_tools.snapshot_ui` のスキーマ
        （`active_modal` / `visible_dialogs` / `active_window` / `main_window`）。
        """
        return self.call(
            "qgis_snapshot_ui",
            {
                "max_depth": max_depth,
                "include_invisible": include_invisible,
                "include_main_window": include_main_window,
            },
            instance=instance,
        )

    def click_widget(
        self, selector: dict[str, Any], *, instance: str | None = None
    ) -> dict[str, Any]:
        """`qgis_click_widget` を呼ぶ。selector 仕様は `ui_tools._find_widget`。"""
        return self.call("qgis_click_widget", {"selector": selector}, instance=instance)

    def set_widget_value(
        self,
        selector: dict[str, Any],
        value: Any,
        *,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """`qgis_set_widget_value` を呼ぶ。"""
        return self.call(
            "qgis_set_widget_value",
            {"selector": selector, "value": value},
            instance=instance,
        )

    def screenshot(
        self,
        *,
        output_path: str | None = None,
        width: int | None = None,
        height: int | None = None,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """`qgis_screenshot` を呼ぶ。`None` の引数は省略して送る。"""
        params: dict[str, Any] = {}
        if output_path is not None:
            params["output_path"] = output_path
        if width is not None:
            params["width"] = width
        if height is not None:
            params["height"] = height
        return self.call("qgis_screenshot", params, instance=instance)

    def get_canvas_extent(self, *, instance: str | None = None) -> dict[str, Any]:
        """`qgis_get_canvas_extent` を呼ぶ。"""
        return self.call("qgis_get_canvas_extent", {}, instance=instance)

    def set_canvas_extent(
        self,
        xmin: float,
        ymin: float,
        xmax: float,
        ymax: float,
        *,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """`qgis_set_canvas_extent` を呼ぶ。"""
        return self.call(
            "qgis_set_canvas_extent",
            {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax},
            instance=instance,
        )

    def get_selected_features(
        self,
        layer_name: str,
        *,
        limit: int = 100,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """`qgis_get_selected_features` を呼ぶ。"""
        return self.call(
            "qgis_get_selected_features",
            {"layer_name": layer_name, "limit": limit},
            instance=instance,
        )

    # ------------------------------------------------------------
    # test.* namespace（ADR-0002 Roadmap "Worker test handler 追加"）
    # 利用するには Worker 側で QPUPPETEER_ALLOW_TEST_HANDLERS=1 が立っている必要がある。
    # ------------------------------------------------------------

    def signal_spy_start(
        self,
        selector: dict[str, Any],
        signal_name: str,
        *,
        max_emissions: int | None = None,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """``test.signal_spy_start``: widget の signal に spy を attach する。

        Returns:
            ``{spy_id, matched, diagnostics}``。``matched=False`` のときは widget
            解決に失敗（``diagnostics`` に reason / candidates）、``spy_id=-1``。
        """
        params: dict[str, Any] = {"selector": selector, "signal": signal_name}
        if max_emissions is not None:
            params["max_emissions"] = int(max_emissions)
        return self.call("test.signal_spy_start", params, instance=instance)

    def signal_spy_get_emissions(
        self,
        spy_id: int,
        *,
        since_index: int = 0,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """``test.signal_spy_get_emissions``: 蓄積された emissions を取得。

        ``since_index`` で前回照会以降の差分のみを取れる（Returns ``next_index``）。
        """
        return self.call(
            "test.signal_spy_get_emissions",
            {"spy_id": int(spy_id), "since_index": int(since_index)},
            instance=instance,
        )

    def signal_spy_count(
        self,
        spy_id: int,
        *,
        instance: str | None = None,
    ) -> int:
        """``test.signal_spy_count``: emissions 件数だけを軽量に取得する。"""
        result = self.call(
            "test.signal_spy_count",
            {"spy_id": int(spy_id)},
            instance=instance,
        )
        return int(result.get("count", 0)) if isinstance(result, dict) else 0

    def signal_spy_stop(
        self,
        spy_id: int,
        *,
        instance: str | None = None,
    ) -> bool:
        """``test.signal_spy_stop``: spy を解除して registry から外す。

        Returns:
            ``True`` if removed, ``False`` if spy_id was unknown.
        """
        result = self.call(
            "test.signal_spy_stop",
            {"spy_id": int(spy_id)},
            instance=instance,
        )
        return bool(result.get("ok", False)) if isinstance(result, dict) else False

    def wait_for_signal(
        self,
        selector: dict[str, Any],
        signal_name: str,
        *,
        timeout_ms: int = 5000,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """``test.wait_for_signal``: signal が emit するまで blocking 待機。

        Returns:
            ``{fired, args, timed_out, matched, diagnostics?}``。``matched=False``
            なら widget 解決失敗、``timed_out=True`` なら timeout、``fired=True``
            なら captured ``args`` が JSON safe なリストで入る。
        """
        return self.call(
            "test.wait_for_signal",
            {
                "selector": selector,
                "signal": signal_name,
                "timeout_ms": int(timeout_ms),
            },
            instance=instance,
        )

    def get_recent_exceptions(
        self,
        *,
        limit: int | None = None,
        instance: str | None = None,
    ) -> list[dict[str, Any]]:
        """Worker 側 ``sys.excepthook`` リングバッファに溜まった未捕捉例外を取る。

        ADR-0002 §17 / Roadmap "Qt 未捕捉例外 → fail 連動" のクライアント側 API。
        plugin の auto-fail 機構（既定 ON）が利用するが、test 内から手動で照会
        しても良い。

        Returns:
            list of dict （古い順）。各レコードは ``ts`` / ``type`` / ``message``
            / ``traceback`` / ``thread``。
        """
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = int(limit)
        result = self.call("qgis_get_recent_exceptions", params, instance=instance)
        excs = result.get("exceptions") if isinstance(result, dict) else None
        return list(excs) if isinstance(excs, list) else []

    def clear_recent_exceptions(self, *, instance: str | None = None) -> None:
        """Worker 側 例外バッファをクリアする（test setup でリセット用）。"""
        self.call("qgis_clear_recent_exceptions", {}, instance=instance)

    def select_features(
        self,
        layer_name: str,
        expression: str,
        *,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """`qgis_select_features` を呼ぶ。"""
        return self.call(
            "qgis_select_features",
            {"layer_name": layer_name, "expression": expression},
            instance=instance,
        )

    def get_layer_info(self, layer_name: str, *, instance: str | None = None) -> dict[str, Any]:
        """`qgis_get_layer_info` を呼ぶ。"""
        return self.call("qgis_get_layer_info", {"layer_name": layer_name}, instance=instance)

    def check_actionability(
        self, selector: dict[str, Any], *, instance: str | None = None
    ) -> dict[str, Any]:
        """`qgis_check_actionability` を呼ぶ。

        ADR-0002 §8.3 の auto-wait 用 1-shot 判定。返り値は
        `{exists, visible, enabled, not_covered, actionable, widget, diagnostics}`。
        通常はこれを直接呼ばず `Locator` 経由で auto-wait する。
        """
        return self.call("qgis_check_actionability", {"selector": selector}, instance=instance)

    def locator(
        self,
        selector: dict[str, Any],
        *,
        instance: str | None = None,
        default_timeout_s: float = 5.0,
        stable: bool = False,
    ) -> Any:
        """`Locator` を生成する。`selector` 仕様は `ui_tools._find_widget` と同じ。

        ``stable=True`` で auto-wait に geometry stability check を有効化する
        （ADR-0002 §8.3、アニメ中操作の吸収）。既定 False（普通 widget は最初から
        stable で、stable 待ちは追加 1 RTT を使う opt-in）。

        circular import 回避のため遅延 import している。
        """
        from pytest_qgis_puppeteer.locator import Locator

        return Locator(
            self,
            selector,
            instance=instance,
            default_timeout_s=default_timeout_s,
            stable=stable,
        )

    # ------------------------------------------------------------
    # 便利メソッド（poll ベースの待機系）
    # ------------------------------------------------------------

    def wait_for_modal(
        self,
        title_contains: str,
        *,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.25,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """`active_modal` の title に `title_contains` が部分一致するまで poll。

        snapshot は `include_main_window=True` で取る（main window 上の非モーダル
        ダイアログも `visible_dialogs` 経由で拾えるよう保険をかける）。

        Returns:
            条件を満たした時の `active_modal` 辞書（snapshot から抜き出したもの）。

        Raises:
            TimeoutError: `timeout_s` 以内にマッチする modal が現れなかった。
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            snap = self.snapshot_ui(include_main_window=True, instance=instance)
            modal = snap.get("active_modal")
            if modal is not None and title_contains in (modal.get("title") or ""):
                return modal
            time.sleep(poll_interval_s)
        raise TimeoutError(
            f"No modal with title containing {title_contains!r} appeared within {timeout_s}s"
        )

    def wait_for_modal_closed(
        self,
        *,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.25,
        instance: str | None = None,
    ) -> None:
        """`active_modal` が消えるまで poll。

        Raises:
            TimeoutError: `timeout_s` 以内に modal が閉じなかった。
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            snap = self.snapshot_ui(include_main_window=True, instance=instance)
            if snap.get("active_modal") is None:
                return
            time.sleep(poll_interval_s)
        raise TimeoutError(f"Active modal did not close within {timeout_s}s")

    def wait_for_widget(
        self,
        selector: dict[str, Any],
        *,
        visible: bool = True,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.25,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """selector に一致するウィジェットが指定の可視状態になるまで poll。

        実装は `snapshot_ui(include_main_window=True)` を取って tree を
        再帰的に探索する（Worker 側に「selector で 1 つ describe する」
        専用 handler が無いため）。selector は `class` / `object_name` /
        `text` / `title` を AND 条件で見る（`ui_tools._find_widget` の subset）。

        Args:
            visible: True なら可視で見つかるまで待つ。False なら非表示
                または非存在になるまで待つ。

        Returns:
            条件成立時に見つけたウィジェット辞書（snapshot 上の node）。
            `visible=False` で見えなくなった場合は空 dict を返す。

        Raises:
            TimeoutError: 条件成立前に `timeout_s` を超えた。
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            snap = self.snapshot_ui(include_main_window=True, instance=instance)
            found = _find_in_snapshot(snap, selector)
            if visible:
                if found is not None and found.get("visible", True):
                    return found
            else:
                if found is None or not found.get("visible", True):
                    return found or {}
            time.sleep(poll_interval_s)
        raise TimeoutError(
            f"Widget matching {selector!r} did not reach visible={visible} within {timeout_s}s"
        )

    def wait_for_project_loaded(
        self,
        *,
        timeout_s: float = 60.0,
        poll_interval_s: float = 0.5,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """プロジェクトが読み込まれて layer が取得可能になるまで待つ。

        QGIS を `--project <path>` で起動した場合、qgis_puppet の register
        完了（= `wait_for_worker` が返ったタイミング）は QGIS メインウィンドウ
        表示直後で、プロジェクト読み込みは async で後から走る。そのため
        register 直後に `qgis_list_layers` を呼ぶと空で返ることがある（smoke
        test で `count=0` が観測された root cause）。本メソッドは layer
        数が 1 以上になるまで poll する。

        モーダルが開いている場合は読み込みが止まっているので、
        `ModalBlockedError` を raise して早期に break する（ログインダイアログ
        などでユーザ応答待ちになっているケースが典型）。

        Args:
            timeout_s: 待機上限（既定 60 秒。大プロジェクト + 複数 PostGIS
                layer の読み込みは数十秒かかることがあるため長め）。
            poll_interval_s: poll 間隔（既定 0.5 秒。snapshot と list_layers
                の 2 連打になるので短すぎると Worker が詰まる）。
            instance: 対象 Worker。単一 Worker の場合は省略可。

        Returns:
            成功時は `qgis_list_layers` の結果辞書（`{layers: [...], count: N}`）。

        Raises:
            ModalBlockedError: 読み込み途中でモーダルが検出された。
            TimeoutError: `timeout_s` 内に layer > 0 にならなかった。
        """
        deadline = time.monotonic() + timeout_s
        last_count = 0
        while time.monotonic() < deadline:
            # モーダルがあれば QGIS 側はそこで止まっている可能性大。読み込み
            # 再開の見込みがないので専用例外で早期に break する。
            snap = self.snapshot_ui(include_main_window=True, instance=instance)
            modal = snap.get("active_modal")
            if isinstance(modal, dict):
                raise ModalBlockedError(
                    f"Project load blocked by modal: "
                    f"title={modal.get('title')!r} "
                    f"class={modal.get('class')!r}",
                    modal=modal,
                )
            result = self.list_layers(instance=instance)
            layers = result.get("layers", []) if isinstance(result, dict) else []
            if layers:
                logger.info("Project loaded: %d layers available", len(layers))
                return result if isinstance(result, dict) else {"layers": layers}
            last_count = len(layers)
            time.sleep(poll_interval_s)
        raise TimeoutError(
            f"Project did not finish loading within {timeout_s}s "
            f"(last list_layers count={last_count})"
        )

    # ------------------------------------------------------------
    # internal
    # ------------------------------------------------------------

    def _require_connected(
        self,
    ) -> tuple[AutomationClient, asyncio.AbstractEventLoop]:
        if self._client is None or self._loop is None:
            raise RuntimeError("E2EAutomationClient.connect() has not been called yet")
        return self._client, self._loop


# ============================================================
# snapshot tree 探索（wait_for_widget の補助）
# ============================================================


def _snapshot_roots(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """snapshot 辞書から探索対象の root node 群を抽出する。

    `active_modal` を最優先（モーダルがあればフォーカスはそこに集中している）、
    続いて `visible_dialogs`（モードレスダイアログ）、最後に `main_window`
    （`include_main_window=True` のときのみ存在）。
    """
    roots: list[dict[str, Any]] = []
    modal = snapshot.get("active_modal")
    if isinstance(modal, dict):
        roots.append(modal)
    for d in snapshot.get("visible_dialogs") or ():
        if isinstance(d, dict):
            roots.append(d)
    main = snapshot.get("main_window")
    if isinstance(main, dict):
        roots.append(main)
    return roots


def _node_matches(node: dict[str, Any], selector: dict[str, Any]) -> bool:
    """selector の各キーが node のフィールドと完全一致するかを判定。

    OSS 共通の `qgis_puppeteer.selector_match.record_matches_selector` に委譲。
    snapshot node はそのまま record として扱える（`class` / `object_name` /
    `text` / `title` / `label` / `placeholder` 等のキーが入っている）。
    """
    return record_matches_selector(node, selector)


def _find_in_snapshot(snapshot: dict[str, Any], selector: dict[str, Any]) -> dict[str, Any] | None:
    """snapshot tree を DFS で歩き、selector の解決結果を返す。

    selector 仕様（match キー / `index` / strict mode）は live tree 側
    `_find_widget` と完全に揃える（ADR-0002 §8.1 / Roadmap "Selector resolver の
    統合"）。共通 helper `resolve_with_index` に委譲。

    Strict mode: 複数マッチ + `index` 未指定 → None を返す（snapshot 経路は
    `wait_for_widget` / `Locator.snapshot()` から呼ばれ、両者とも単純 None
    判定をするため、ambiguous は「見つからなかった」として扱う。`Locator` の
    操作系（click / fill）は `check_actionability` 経由で別途 early-fail する）。

    Locator chain（``_scope_chain`` キー）: ネスト selector を順に解決し、
    最深 node の subtree 内で leaf selector を適用する（live tree 側
    `_find_widget` と semantics 一致）。
    """
    chain = selector.get("_scope_chain") or []
    if chain:
        leaf = {k: v for k, v in selector.items() if k != "_scope_chain"}
        # 各 step を順に解決し、結果 node を次の探索 root にする
        roots: list[dict[str, Any]] = list(_snapshot_roots(snapshot))
        parent_node: dict[str, Any] | None = None
        for step in chain:
            search_roots = roots if parent_node is None else [parent_node]
            parent_node = _find_first_in_subtree(search_roots, step)
            if parent_node is None:
                return None
        assert parent_node is not None
        return _find_first_in_subtree([parent_node], leaf)

    return _find_first_in_subtree(list(_snapshot_roots(snapshot)), selector)


def _find_first_in_subtree(
    roots: list[dict[str, Any]], selector: dict[str, Any]
) -> dict[str, Any] | None:
    """roots の subtree を DFS して selector を解決する内部 helper。

    `_find_in_snapshot` の chain step / leaf 共通実装。selector に
    ``_scope_chain`` が含まれている場合は何もしない（呼び出し側で除去想定）。
    """
    matches: list[dict[str, Any]] = []
    # 探索は DFS stack。stack の `pop()` で natural な兄弟順序を保つため、
    # 子要素は逆順で push する（push: [c, b, a] → pop: a → b → c）。
    # `index=N` 指定時のユーザー期待（snapshot に出ている順）に合わせる。
    stack: list[dict[str, Any]] = list(reversed(roots))
    while stack:
        node = stack.pop()
        if _node_matches(node, selector):
            matches.append(node)
        children = node.get("children") or ()
        for child in reversed(children):
            if isinstance(child, dict) and not child.get("_truncated"):
                stack.append(child)
    chosen, _diag = resolve_with_index(matches, selector)
    return chosen
