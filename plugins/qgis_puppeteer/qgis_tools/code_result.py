"""``execute_python`` の結果値ハンドリング（Qt/QGIS 非依存・単体テスト可能）。

``python_executor`` はモジュール先頭で QGIS を import するため単体テストできない。
そこでコード実行と「戻り値を JSON 安全な形へ整える」純ロジックをここへ切り出し、
QGIS 無し環境で網羅テストできるようにする。

## 戻り値契約（ADR フォローアップ / multi-agent レビューで確定）

テストヘルパなので **silent-wrong（黙って違う値を返す）を最優先で排除**する。

- **値の捕捉は明示 ``_result =`` のみ**（``exec`` 単一モード。``eval`` 切替や
  「最終式の値」自動取得のような構文依存の魔法は持たない）。コードが ``_result``
  を代入すればその値、しなければ ``result_set=False``（副作用専用呼び出し）。
- 戻り値が **JSON 直列化可能**（``allow_nan=False``）なら型を保ったまま返す。
  不可なら ``result`` には入れず、``result_serializable=False`` +
  ``result_repr`` + ``result_type`` を診断用に付けて呼び出し側へ知らせる
  （ラッパ側で loud に raise する）。
"""

from __future__ import annotations

import json
from typing import Any

_FILENAME = "<qgis_puppeteer_exec>"


def run_code(code: str, context: dict[str, Any]) -> dict[str, Any]:
    """``code`` を ``exec`` し、``_result`` の捕捉状態と result フィールド群を返す。

    例外は呼び出し側（``_execute_code_internal``）でまとめて捕捉する想定なので、
    ここでは握らない（コード内例外はそのまま伝播させる）。

    Returns:
        以下を含む dict:
            - ``result_set``: コードが ``_result`` を代入したか。
            - ``result``: 直列化可能な値（不可 / 未代入なら ``None``）。
            - ``result_serializable``: ``result`` が実値か（False=repr 退避）。
            - ``result_repr`` / ``result_type``: 非直列化時のみ付与（診断用）。
    """
    exec(compile(code, _FILENAME, "exec"), context)  # noqa: S102 - 許可済みコード実行
    if "_result" not in context:
        # 値を捕捉していない（副作用専用）。result_set=False で明示する。
        return {"result_set": False, "result": None, "result_serializable": True}
    fields: dict[str, Any] = {"result_set": True}
    fields.update(jsonable_result(context["_result"]))
    return fields


def jsonable_result(value: Any) -> dict[str, Any]:
    """戻り値を JSON 安全な result フィールド群へ整える。

    Returns:
        - 直列化可能: ``{"result": value, "result_serializable": True}``
        - 不可: ``{"result": None, "result_serializable": False,
          "result_repr": <repr>, "result_type": <型名>}``
          （``result`` には repr 文字列を入れない＝実値と誤認させない）
    """
    if _is_json_serializable(value):
        return {"result": value, "result_serializable": True}
    return {
        "result": None,
        "result_serializable": False,
        "result_repr": _safe_repr(value),
        "result_type": type(value).__name__,
    }


def _is_json_serializable(value: Any) -> bool:
    """``json.dumps`` で（NaN/Inf も拒否して）直列化できるか。再帰的に判定。"""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def _safe_repr(value: Any) -> str:
    """``repr`` が例外を投げる壊れた QGIS オブジェクトでも落ちない repr。"""
    try:
        return repr(value)
    except Exception as exc:  # noqa: BLE001 - 診断用、repr 失敗でも握る
        return f"<unreprable {type(value).__name__}: {exc!r}>"
