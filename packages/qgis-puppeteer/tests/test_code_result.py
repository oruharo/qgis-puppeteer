"""``qgis_tools.code_result`` の単体テスト（Qt/QGIS 不要の純ロジック）。

(Y) 再設計の中核: 値の捕捉は明示 ``_result =`` のみ（構文依存の魔法なし）。
戻り値は JSON 直列化可能なら型保持、不可なら repr を診断フィールドへ退避。
"""

from __future__ import annotations

import math

from qgis_puppeteer.qgis_tools.code_result import jsonable_result, run_code

# ============================================================
# run_code — 明示 _result のみ
# ============================================================


class TestRunCode:
    def test_result_assignment_captured(self) -> None:
        out = run_code("_result = 1 + 1", {})
        assert out == {"result_set": True, "result": 2, "result_serializable": True}

    def test_no_result_means_result_set_false(self) -> None:
        # 副作用専用（_result 未代入）。値は捕捉しない。
        ctx: dict = {}
        out = run_code("x = 5", ctx)
        assert out == {"result_set": False, "result": None, "result_serializable": True}
        assert ctx["x"] == 5  # 副作用は context に残る

    def test_explicit_result_none_is_distinguished(self) -> None:
        # _result = None は「捕捉した上で None」。result_set=True で未代入と区別。
        out = run_code("_result = None", {})
        assert out == {"result_set": True, "result": None, "result_serializable": True}

    def test_bare_expression_is_not_captured(self) -> None:
        # 単一式を書いても _result に代入しなければ値は返らない（魔法なし）。
        out = run_code("len([1, 2, 3])", {})
        assert out["result_set"] is False
        assert out["result"] is None

    def test_falsy_values_round_trip(self) -> None:
        for code, expected in [
            ("_result = 0", 0),
            ("_result = ''", ""),
            ("_result = False", False),
            ("_result = []", []),
        ]:
            out = run_code(code, {})
            assert out["result_set"] is True
            assert out["result"] == expected
            assert out["result_serializable"] is True

    def test_container_round_trips(self) -> None:
        out = run_code("_result = [1, {'a': 2}]", {})
        assert out["result"] == [1, {"a": 2}]
        assert out["result_serializable"] is True

    def test_code_exception_propagates(self) -> None:
        # 例外は握らず伝播（呼び出し側でまとめて捕捉する）。
        try:
            run_code("raise ValueError('boom')", {})
        except ValueError as e:
            assert "boom" in str(e)
        else:  # pragma: no cover
            raise AssertionError("expected ValueError")


# ============================================================
# jsonable_result
# ============================================================


class TestJsonableResult:
    def test_serializable_passes_through(self) -> None:
        assert jsonable_result(0) == {"result": 0, "result_serializable": True}
        assert jsonable_result([1, 2]) == {"result": [1, 2], "result_serializable": True}

    def test_non_serializable_goes_to_diagnostics_not_result(self) -> None:
        class Weird:
            def __repr__(self) -> str:
                return "<Weird obj>"

        out = jsonable_result(Weird())
        assert out["result"] is None  # repr を result に入れない（実値と誤認させない）
        assert out["result_serializable"] is False
        assert out["result_repr"] == "<Weird obj>"
        assert out["result_type"] == "Weird"

    def test_nested_non_serializable_whole_struct_flagged(self) -> None:
        class Weird:
            pass

        out = jsonable_result([1, Weird()])
        assert out["result_serializable"] is False
        assert out["result_type"] == "list"
        assert out["result"] is None

    def test_nan_and_inf_are_non_serializable(self) -> None:
        # allow_nan=False なので NaN/Inf は非直列化扱い（厳格 JSON 消費者を守る）。
        for v in (math.nan, math.inf, -math.inf):
            out = jsonable_result(v)
            assert out["result_serializable"] is False
            assert out["result_type"] == "float"

    def test_safe_repr_survives_broken_repr(self) -> None:
        class BadRepr:
            def __repr__(self) -> str:
                raise RuntimeError("no repr for you")

        out = jsonable_result(BadRepr())
        assert out["result_serializable"] is False
        assert out["result_type"] == "BadRepr"
        assert "unreprable BadRepr" in out["result_repr"]
