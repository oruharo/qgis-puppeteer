"""Hub ファイル：動いている Hub への接続情報を、本人だけが読めるファイルに置く。

Hub は待ち受けを始めたらこのファイルを書き、止まるときに消す。Hub につなぐ側
（QGIS のプラグイン、MCP の gateway、pytest、``AutomationClient``）はここから
token を読み、挨拶（``qgis_puppeteer.handshake``）で「同じ token を知っている」
ことを Hub と確かめ合う。同じ機械のほかの OS ユーザーはこのファイルを読めない
ので、その人の QGIS を操作できない。

置き場所（``resolve_hub_file_path``）:

1. 呼び出し側が明示したパス（Hub の ``--hub-file``、``AutomationClient(hub_file=...)``）
2. 環境変数 ``QPUPPETEER_HUB_FILE``
3. 既定（``default_hub_file_path``）

Hub・Worker・クライアントの全員が同じ規則で決めるので、プラグインが起こした
Hub と、その QGIS の Worker は同じファイルを見る。

標準ライブラリだけで書く（websockets の無い QGIS の Python からも import する）。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qgis_puppeteer.protocol import ProtocolRange, parse_protocol_range

ENV_HUB_FILE = "QPUPPETEER_HUB_FILE"
# ファイルの書式の版。読めない書式なら HubFileError にする
FORMAT = 1
_DIR_NAME = "qgis_puppeteer"
_FILE_NAME = "hub.json"

# Windows では、読んでいる人がいると os.replace が PermissionError になる。
# 読むのは一瞬なので、少し待ってやり直す
_REPLACE_ATTEMPTS = 20
_REPLACE_RETRY_SECONDS = 0.05


class HubFileError(OSError):
    """Hub ファイルが無い・読めない・壊れている・書式が違う。

    ``OSError`` の子なので、「Hub につながらない」と同じに扱える。
    """

    def __init__(self, message: str, *, path: Path) -> None:
        super().__init__(message)
        self.path = path


@dataclass(frozen=True)
class HubFileInfo:
    """Hub ファイルの中身。"""

    # "127.0.0.1:9876" の形。つなぐ側は順に試す
    addresses: tuple[str, ...]
    # 挨拶の proof の鍵。通信には載せない
    token: str
    # TLS の情報（ネットワークから受け付ける Hub だけ）。今は常に None
    tls: dict[str, Any] | None = None
    pid: int | None = None
    package_version: str | None = None
    protocol: ProtocolRange | None = None
    # ISO 8601（UTC）
    started_at: str | None = None
    # この版が知らない項目（新しい版の Hub が足したもの）。書き戻すときに残す
    extra: dict[str, Any] = field(default_factory=dict, compare=False)


def default_hub_file_path() -> Path:
    """既定の Hub ファイルのパス。

    - Windows：``%LOCALAPPDATA%\\qgis_puppeteer\\hub.json``
      （既定の ACL で、本人・SYSTEM・Administrators だけが読める）
    - POSIX：``$XDG_RUNTIME_DIR/qgis_puppeteer/hub.json``。無ければ
      ``~/.local/state/qgis_puppeteer/hub.json``
    """
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
    else:
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        base = Path(runtime) if runtime else Path.home() / ".local" / "state"
    return base / _DIR_NAME / _FILE_NAME


def resolve_hub_file_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """使う Hub ファイルのパス：明示 → ``QPUPPETEER_HUB_FILE`` → 既定。"""
    if explicit is not None and str(explicit) != "":
        return Path(explicit)
    env = os.environ.get(ENV_HUB_FILE)
    if env:
        return Path(env)
    return default_hub_file_path()


def now_iso() -> str:
    """``started_at`` に入れる今の時刻（UTC、秒まで）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_hub_file(path: str | os.PathLike[str], info: HubFileInfo) -> None:
    """Hub ファイルを書く。本人だけが読めるように作り、置き換えは一度に行う。

    一時ファイル（``mkstemp`` は 0600 で作る）に書いて fsync し、``os.replace``
    で置き換える。読む側が書きかけのファイルを見ることは無い。フォルダが
    無ければ 0700 で作る（既にあるフォルダの権限は変えない）。
    """
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, Any] = dict(info.extra)
    payload.update(
        {
            "format": FORMAT,
            "addresses": list(info.addresses),
            "token": info.token,
            "tls": info.tls,
            "pid": info.pid,
            "package_version": info.package_version,
            "protocol": (
                None
                if info.protocol is None
                else {"min": info.protocol.min, "max": info.protocol.max}
            ),
            "started_at": info.started_at,
        }
    )
    fd, tmp_name = tempfile.mkstemp(prefix=".hub-", suffix=".tmp", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _replace_with_retry(src: Path, dst: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_RETRY_SECONDS)


def read_hub_file(path: str | os.PathLike[str]) -> HubFileInfo:
    """Hub ファイルを読む。

    Raises:
        HubFileError: 無い・読めない・JSON が壊れている・書式の版が違う・token が無い
    """
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise HubFileError(f"No Hub file at {target} (is the Hub running?)", path=target) from e
    except OSError as e:
        raise HubFileError(f"Could not read the Hub file {target}: {e}", path=target) from e
    try:
        raw = json.loads(text)
    except ValueError as e:
        raise HubFileError(f"The Hub file {target} is not valid JSON: {e}", path=target) from e
    if not isinstance(raw, dict):
        raise HubFileError(f"The Hub file {target} is not a JSON object", path=target)
    fmt = raw.get("format")
    if fmt != FORMAT:
        raise HubFileError(
            f"The Hub file {target} has format {fmt!r}; this version reads format {FORMAT}. "
            "Use the same qgis-puppeteer version for the Hub and its clients.",
            path=target,
        )
    token = raw.get("token")
    if not isinstance(token, str) or not token:
        raise HubFileError(f"The Hub file {target} has no token", path=target)
    raw_addresses = raw.get("addresses")
    addresses = (
        tuple(a for a in raw_addresses if isinstance(a, str))
        if isinstance(raw_addresses, list)
        else ()
    )
    known = {
        "format",
        "addresses",
        "token",
        "tls",
        "pid",
        "package_version",
        "protocol",
        "started_at",
    }
    pid = raw.get("pid")
    tls = raw.get("tls")
    package_version = raw.get("package_version")
    started_at = raw.get("started_at")
    return HubFileInfo(
        addresses=addresses,
        token=token,
        tls=tls if isinstance(tls, dict) else None,
        pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        package_version=package_version if isinstance(package_version, str) else None,
        protocol=parse_protocol_range(raw.get("protocol")),
        started_at=started_at if isinstance(started_at, str) else None,
        extra={k: v for k, v in raw.items() if k not in known},
    )


def remove_hub_file(path: str | os.PathLike[str], *, token: str) -> bool:
    """Hub ファイルを消す。中の token が ``token`` のときだけ（自分の書いたものだけ）。

    止まる Hub が、あとから起きた別の Hub のファイルを消さないため。
    消したら True。無い・読めない・別の Hub のもの・消せなかったなら False。
    """
    target = Path(path)
    try:
        info = read_hub_file(target)
    except HubFileError:
        return False
    if info.token != token:
        return False
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True
