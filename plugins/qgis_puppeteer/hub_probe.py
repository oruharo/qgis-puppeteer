"""Hub が生きているかを確かめる（Hub ファイル・pid・挨拶）。

「生きた Hub がいる」とは、次の 3 つがそろうこと（同じ機械の Hub）:

1. Hub ファイルがあって読める
2. ファイルの pid のプロセスが生きている（pid の無い、別の機械へ写すネットワーク用の
   ファイルでは確かめない）
3. ファイルの ``addresses`` のどれかで挨拶（``qgis_puppeteer.handshake``）が通る
   ＝相手が同じ token を知っている Hub だと、互いに確かめられた。ファイルに ``tls``
   があれば TLS でつなぎ、証明書の指紋も照合する（``qgis_puppeteer.hub_remote``）

どれかが欠ければ、そのファイルは古い（Hub が落ちた、別のプログラムがそのポートを
使っている、など）とみなして無視する。

挨拶は、標準ライブラリだけで書いた小さな同期の WebSocket クライアントで行う
（QGIS の Python には websockets が無い。プラグインが Hub を起こすか決めるときにも
使うため）。``automation_client`` として register し、Hub の hub_proof を確かめたら
すぐ切る。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import socket
import struct
import sys
import time
import uuid
from pathlib import Path

from qgis_puppeteer._version import __version__
from qgis_puppeteer.handshake import (
    ack_is_trusted,
    choose_protocol_version,
    client_proof,
    generate_nonce,
)
from qgis_puppeteer.hub_file import HubFileError, HubFileInfo, read_hub_file
from qgis_puppeteer.hub_remote import (
    client_ssl_context,
    fingerprint_mismatch_message,
    peer_matches,
    pinned_fingerprint,
)
from qgis_puppeteer.protocol import (
    SUPPORTED_PROTOCOL,
    Hello,
    ProtocolDecodeError,
    RegisterAck,
    RegisterRequest,
    Role,
    decode_message,
    encode_message,
    error_code_value,
)

DEFAULT_PROBE_TIMEOUT_SECONDS: float = 2.0
# Hub は Origin を確かめる（ADR-0001 §9.2）。ほかのクライアントと同じものを送る
_ORIGIN = "http://localhost"
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HEADER_BYTES = 16 * 1024
_MAX_MESSAGE_BYTES = 1024 * 1024

_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class HubProbeError(Exception):
    """挨拶が通らなかった（つながらない・Hub でない・token が違う・時間切れ）。"""


# ============================================================
# pid
# ============================================================


def pid_alive(pid: int) -> bool:
    """pid のプロセスが生きているか。分からないときは True（生きている側に倒す）。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 別のユーザーのプロセスとして生きている
        return True
    except OSError:
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    error_access_denied = 5
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # 無いプロセスは ERROR_INVALID_PARAMETER。権限で開けないのは生きている
        return ctypes.get_last_error() == error_access_denied
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


# ============================================================
# 挨拶
# ============================================================


def split_address(address: str) -> tuple[str, int]:
    """``"127.0.0.1:51234"`` / ``"[::1]:51234"`` を (host, port) に分ける。

    Raises:
        ValueError: ポートが無い・数でない
    """
    host, sep, port = address.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ValueError(f"Not a host:port address: {address!r}")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host, int(port)


def greet_hub(
    address: str,
    token: str,
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    tls_fingerprint: str | None = None,
) -> RegisterAck:
    """``address`` の Hub と挨拶し、通れば Hub の register_ack を返す（すぐ切る）。

    Args:
        tls_fingerprint: 渡せば TLS でつなぎ、Hub の証明書の SHA-256 がこれと一致する
            ことを挨拶の前に確かめる

    Raises:
        HubProbeError: つながらない・hello が来ない・Hub が断った・hub_proof が合わない・
            証明書の指紋が合わない
    """
    deadline = time.monotonic() + timeout
    try:
        host, port = split_address(address)
    except ValueError as e:
        raise HubProbeError(str(e)) from e
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        raise HubProbeError(f"Could not connect to {address}: {e}") from e
    try:
        if tls_fingerprint is not None:
            sock = _wrap_tls(sock, address, tls_fingerprint)
        conn = _WebSocket(sock, deadline)
        conn.handshake(host, port)
        hello = _decode(conn.recv_text(), address)
        if not isinstance(hello, Hello):
            raise HubProbeError(f"{address} did not greet with hello: not a qgis-puppeteer Hub")
        nonce = generate_nonce()
        conn.send_text(
            encode_message(
                RegisterRequest(
                    id=uuid.uuid4().hex,
                    role=Role.AUTOMATION_CLIENT,
                    protocol_version=choose_protocol_version(hello.protocol),
                    protocol=SUPPORTED_PROTOCOL,
                    package_version=__version__,
                    nonce=nonce,
                    proof=client_proof(token, hello.nonce, nonce),
                )
            )
        )
        ack = _decode(conn.recv_text(), address)
        if not isinstance(ack, RegisterAck):
            raise HubProbeError(f"{address} did not answer register with register_ack")
        if not ack_is_trusted(ack, token=token, client_nonce=nonce, hub_nonce=hello.nonce):
            raise HubProbeError(f"{address} does not know the token in the Hub file")
        if not ack.ok:
            code = error_code_value(ack.error.code) if ack.error is not None else "unknown"
            message = ack.error.message if ack.error is not None else ""
            raise HubProbeError(f"{address} refused the register ({code}): {message}")
        conn.close()
        return ack
    except (OSError, ValueError) as e:
        raise HubProbeError(f"Greeting {address} failed: {e}") from e
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def _wrap_tls(sock: socket.socket, address: str, fingerprint: str) -> socket.socket:
    """TLS でつなぎ、証明書の指紋を照合する。合わなければ切って HubProbeError。"""
    try:
        tls = client_ssl_context().wrap_socket(sock)
    except (OSError, ValueError) as e:
        sock.close()
        raise HubProbeError(f"TLS with {address} failed: {e}") from e
    if not peer_matches(tls.getpeercert(binary_form=True), fingerprint):
        tls.close()
        raise HubProbeError(fingerprint_mismatch_message(address))
    return tls


def _decode(text: str, address: str) -> object:
    try:
        return decode_message(text)
    except ProtocolDecodeError as e:
        raise HubProbeError(f"{address} sent a message this version cannot read: {e}") from e


def find_live_hub(
    path: str | os.PathLike[str],
    *,
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    address: str | None = None,
) -> HubFileInfo | None:
    """``path`` の Hub ファイルが生きた Hub を指していれば、その中身を返す。

    ファイルが無い・壊れている・pid が死んでいる・どのアドレスでも挨拶が通らない
    （TLS なら証明書の指紋が合わない）なら None（古いファイルは無視する）。

    Args:
        address: ファイルの ``addresses`` の代わりに試すアドレス（VM の NAT の先など、
            Hub から見えない宛先を使うとき）
    """
    try:
        info = read_hub_file(path)
        fingerprint = pinned_fingerprint(info, path)
    except HubFileError:
        return None
    if info.pid is not None and not pid_alive(info.pid):
        return None
    for candidate in (address,) if address is not None else info.addresses:
        try:
            greet_hub(candidate, info.token, timeout=timeout, tls_fingerprint=fingerprint)
        except HubProbeError:
            continue
        return info
    return None


def describe_hub_file(path: str | os.PathLike[str]) -> str:
    """ログやエラーに出す 1 行：Hub ファイルの場所と、読めれば Hub の版・アドレス。"""
    target = Path(path)
    try:
        info = read_hub_file(target)
    except HubFileError as e:
        return f"{target} ({e})"
    pid = "no pid (network)" if info.pid is None else f"pid {info.pid}"
    tls = ", TLS" if info.tls is not None else ""
    return (
        f"{target} (Hub qgis-puppeteer {info.package_version or '?'}, {pid}{tls}, "
        f"addresses {', '.join(info.addresses) or '-'})"
    )


# ============================================================
# 小さな同期 WebSocket クライアント（RFC 6455 のうち挨拶に要るところだけ）
# ============================================================


class _WebSocket:
    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self._sock = sock
        self._deadline = deadline
        self._buf = b""

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out")
        return remaining

    def _recv_some(self) -> bytes:
        self._sock.settimeout(self._remaining())
        chunk = self._sock.recv(65536)
        if not chunk:
            raise ConnectionError("connection closed")
        return chunk

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._buf += self._recv_some()
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _sendall(self, data: bytes) -> None:
        self._sock.settimeout(self._remaining())
        self._sock.sendall(data)

    def handshake(self, host: str, port: int) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_header = f"[{host}]" if ":" in host else host
        request = (
            "GET / HTTP/1.1\r\n"
            f"Host: {host_header}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Origin: {_ORIGIN}\r\n"
            "\r\n"
        )
        self._sendall(request.encode("ascii"))
        while b"\r\n\r\n" not in self._buf:
            if len(self._buf) > _MAX_HEADER_BYTES:
                raise ConnectionError("WebSocket handshake response too large")
            self._buf += self._recv_some()
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split()
        if len(status) < 2 or status[1] != "101":
            raise ConnectionError(f"WebSocket handshake refused: {lines[0]!r}")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()  # noqa: S324 - RFC 6455
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise ConnectionError("WebSocket handshake: bad Sec-WebSocket-Accept")

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(_OP_TEXT, text.encode("utf-8"))

    def recv_text(self) -> str:
        """テキストのメッセージを 1 つ受ける（分割されたものはつなぐ）。"""
        parts: list[bytes] = []
        total = 0
        while True:
            first, second = self._recv_exact(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", self._recv_exact(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", self._recv_exact(8))
            mask = self._recv_exact(4) if second & 0x80 else None
            if length > _MAX_MESSAGE_BYTES:
                raise ConnectionError("WebSocket message too large")
            payload = self._recv_exact(length)
            if mask is not None:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == _OP_CLOSE:
                raise ConnectionError("the Hub closed the connection")
            if opcode == _OP_PING:
                self._send_frame(_OP_PONG, payload)
                continue
            if opcode == _OP_PONG:
                continue
            if opcode not in (_OP_TEXT, _OP_CONTINUATION):
                raise ConnectionError(f"unexpected WebSocket opcode {opcode}")
            parts.append(payload)
            total += length
            if total > _MAX_MESSAGE_BYTES:
                raise ConnectionError("WebSocket message too large")
            if fin:
                return b"".join(parts).decode("utf-8")

    def close(self) -> None:
        with contextlib.suppress(OSError, TimeoutError):
            self._send_frame(_OP_CLOSE, struct.pack("!H", 1000))
