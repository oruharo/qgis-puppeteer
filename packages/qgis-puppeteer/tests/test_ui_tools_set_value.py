"""値の設定の見えない失敗（#23）。

入れる前に断るもの（範囲外の番号・形の違う値・候補に無い文字）と、入れたあとに
読み直して見つけるもの（Qt が黙って丸めた・切り詰めた・無視した）を、実際の Qt で
確かめる。``qgis.PyQt`` が import できる環境（QGIS の Python、
``scripts/qt_tests.py``）でだけ走る。無ければ全件 skip。
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("qgis.PyQt.QtWidgets")
QtCore = pytest.importorskip("qgis.PyQt.QtCore")
QtGui = pytest.importorskip("qgis.PyQt.QtGui")

from qgis_puppeteer.qgis_tools import exception_recorder  # noqa: E402
from qgis_puppeteer.qgis_tools.ui_tools import set_widget_value  # noqa: E402

QApplication = QtWidgets.QApplication


@pytest.fixture(scope="module")
def app() -> Any:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def host(app: Any) -> Iterator[Any]:
    window = QtWidgets.QWidget()
    window.setObjectName("host")
    window.setLayout(QtWidgets.QVBoxLayout())
    window.show()
    exception_recorder.clear()
    try:
        yield window
    finally:
        window.close()
        window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        app.processEvents()
        exception_recorder.clear()


def _add(host: Any, widget: Any, name: str = "w") -> Any:
    widget.setObjectName(name)
    host.layout().addWidget(widget)
    QApplication.processEvents()
    return widget


def _set(value: Any, name: str = "w", **kwargs: Any) -> dict:
    return set_widget_value({"object_name": name, "root_object_name": "host"}, value, **kwargs)


# ------------------------------------------------------------
# 入れる前に断る
# ------------------------------------------------------------


def test_a_list_item_is_chosen_by_its_text(host: Any) -> None:
    """findItems に整数の 0 を渡していて、文字では必ず失敗していた。"""
    widget = _add(host, QtWidgets.QListWidget())
    widget.addItems(["alpha", "beta"])

    result = _set("beta")

    assert result["success"] is True
    assert result["value_after"] == 1
    assert widget.currentItem().text() == "beta"
    missing = _set("bet")  # 完全一致だけ
    assert missing["error"] == "list_item_not_found"
    assert missing["available"] == ["alpha", "beta"]


@pytest.mark.parametrize("kind", ["combo", "list", "tab", "view"])
def test_an_index_out_of_range_is_refused(host: Any, kind: str) -> None:
    if kind == "combo":
        widget = QtWidgets.QComboBox()
        widget.addItems(["a", "b"])
    elif kind == "list":
        widget = QtWidgets.QListWidget()
        widget.addItems(["a", "b"])
    elif kind == "tab":
        widget = QtWidgets.QTabWidget()
        widget.addTab(QtWidgets.QWidget(), "a")
        widget.addTab(QtWidgets.QWidget(), "b")
    else:
        widget = QtWidgets.QListView()
        widget.setModel(QtCore.QStringListModel(["a", "b"]))
    _add(host, widget)

    for value in (2, -1):
        result = _set(value)
        assert result["success"] is False
        assert result["error"] == "index_out_of_range"
        assert result["count"] == 2
    assert _set(1)["success"] is True


@pytest.mark.parametrize(
    ("make", "value", "after"),
    [
        (QtWidgets.QDateEdit, "2026-10-20", "2026-10-20"),
        (QtWidgets.QTimeEdit, "12:34:56", "12:34:56"),
        (QtWidgets.QDateTimeEdit, "2026-10-20T12:34:56", "2026-10-20T12:34:56"),
    ],
)
def test_date_and_time_editors_take_their_own_form(
    host: Any, make: Any, value: str, after: str
) -> None:
    """QDateEdit / QTimeEdit は QDateTimeEdit の下位クラスで、日時の形で読んでいた。"""
    _add(host, make())

    result = _set(value)

    assert result["success"] is True
    assert result["value_after"] == after


@pytest.mark.parametrize(
    ("make", "value"),
    [
        (QtWidgets.QDateEdit, "2026/10/20"),
        (QtWidgets.QTimeEdit, "noon"),
        (QtWidgets.QDateTimeEdit, "2026-10-20"),
    ],
)
def test_a_date_or_time_in_another_form_is_refused(host: Any, make: Any, value: str) -> None:
    widget = _add(host, make())
    before = widget.dateTime()

    result = _set(value)

    assert result["error"] == "invalid_value"
    assert "expected_form" in result
    assert widget.dateTime() == before


@pytest.mark.parametrize(("value", "checked"), [(True, True), ("false", False), ("TRUE", True)])
def test_a_check_box_takes_a_bool_or_true_false(host: Any, value: Any, checked: bool) -> None:
    widget = _add(host, QtWidgets.QCheckBox("x"))
    widget.setChecked(not checked)

    result = _set(value)

    assert result["success"] is True
    assert widget.isChecked() is checked


@pytest.mark.parametrize("value", ["no", "0", 1, None])
def test_a_check_box_refuses_anything_else(host: Any, value: Any) -> None:
    """bool("false") は True なので、これまでは "false" でチェックが付いた。"""
    widget = _add(host, QtWidgets.QCheckBox("x"))

    result = _set(value)

    assert result["error"] == "invalid_value"
    assert widget.isChecked() is False


@pytest.mark.parametrize("value", [3.7, "abc", True])
def test_a_spin_box_refuses_what_it_would_silently_change(host: Any, value: Any) -> None:
    _add(host, QtWidgets.QSpinBox())
    assert _set(value)["error"] == "invalid_value"


# ------------------------------------------------------------
# 入れたあとに読み直して見つける
# ------------------------------------------------------------


def test_a_clamped_number_is_not_applied(host: Any) -> None:
    widget = _add(host, QtWidgets.QSpinBox())
    widget.setRange(0, 99)

    result = _set(150)

    assert result["success"] is False
    assert result["error"] == "value_not_applied"
    assert result["requested"] == 150
    assert result["value_after"] == 99
    assert result["prior_value"] == 0


def test_verify_false_returns_the_value_after_without_failing(host: Any) -> None:
    widget = _add(host, QtWidgets.QSpinBox())
    widget.setRange(0, 99)

    result = _set(150, verify=False)

    assert result["success"] is True
    assert result["value_after"] == 99


def test_a_truncated_text_is_not_applied(host: Any) -> None:
    widget = _add(host, QtWidgets.QLineEdit())
    widget.setMaxLength(3)

    result = _set("abcdef")

    assert result["error"] == "value_not_applied"
    assert result["value_after"] == "abc"


def test_a_validator_is_reported_not_enforced(host: Any) -> None:
    """setText は validator を通さない。受け付けられる形かを添える。"""
    widget = _add(host, QtWidgets.QLineEdit())
    widget.setValidator(QtGui.QIntValidator(0, 9, widget))

    assert _set("x")["acceptable_input"] is False
    assert _set("5")["acceptable_input"] is True


def test_a_date_out_of_range_is_not_applied(host: Any) -> None:
    widget = _add(host, QtWidgets.QDateEdit())
    widget.setDateRange(QtCore.QDate(2020, 1, 1), QtCore.QDate(2020, 12, 31))

    result = _set("2026-10-20")

    assert result["error"] == "value_not_applied"
    assert result["value_after"] == "2020-12-31"


def test_an_exclusive_radio_button_cannot_be_unchecked(host: Any) -> None:
    first = _add(host, QtWidgets.QRadioButton("a"), "a")
    _add(host, QtWidgets.QRadioButton("b"), "b")
    first.setChecked(True)

    result = _set(False, name="a")

    assert result["error"] == "value_not_applied"
    assert result["value_after"] is True


def test_a_subclass_is_not_compared(host: Any) -> None:
    """下位クラスは値を自分で直すことがあるので比べない（value_after は返す）。"""

    class HostLineEdit(QtWidgets.QLineEdit):
        pass

    widget = _add(host, HostLineEdit())
    widget.setMaxLength(3)

    result = _set("abcdef")

    assert result["success"] is True
    assert result["value_after"] == "abc"


def test_a_password_field_does_not_leak_through_a_failure(host: Any) -> None:
    widget = _add(host, QtWidgets.QLineEdit())
    widget.setEchoMode(QtWidgets.QLineEdit.Password)
    widget.setMaxLength(3)

    result = _set("secret")

    assert result["error"] == "value_not_applied"
    assert result["masked"] is True
    assert result["value_after"] is None
    assert result["expected"] is None
    assert "requested" not in result
    assert "secret" not in repr(result) and "sec" not in repr(result)


# ------------------------------------------------------------
# wait=False（#21）
# ------------------------------------------------------------


def test_wait_false_returns_what_it_can_check_before(host: Any) -> None:
    widget = _add(host, QtWidgets.QComboBox())
    widget.addItems(["a", "b"])

    result = _set(5, wait=False)

    assert result["error"] == "index_out_of_range"
    assert "queued" not in result


def test_wait_false_records_what_it_finds_after(host: Any, app: Any) -> None:
    widget = _add(host, QtWidgets.QSpinBox())
    widget.setRange(0, 99)

    result = _set(150, wait=False)

    assert result["queued"] is True
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not exception_recorder.get_recent():
        app.processEvents()
        time.sleep(0.01)
    records = exception_recorder.get_recent()
    assert len(records) == 1
    assert "value_not_applied" in records[0]["message"]
