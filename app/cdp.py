"""极简 WebSocket 客户端 + Chrome DevTools Protocol 封装。

只用标准库（socket + base64 + json），用于连本机 Chromium 调试端口。
不引入 node，也不引入任何第三方包，进程内存开销可以忽略。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.request
from dataclasses import dataclass
from typing import Any


class WSError(RuntimeError):
    pass


class WebSocket:
    def __init__(self, host: str, port: int, path: str, timeout: float = 6.0) -> None:
        self.sock = socket.create_connection((host, port), timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("握手被关闭")
            raw += chunk
        head, self._buffer = raw.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise WSError("握手失败")
        self._closed = False

    # ---------------------------------------------------------------- 帧处理

    def _read_exact(self, size: int) -> bytes:
        data = self._buffer
        while len(data) < size:
            chunk = self.sock.recv(max(4096, size - len(data)))
            if not chunk:
                raise WSError("连接已断开")
            data += chunk
        self._buffer = data[size:]
        return data[:size]

    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        length = len(payload)
        mask_bit = 0x80
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header += struct.pack(">H", length)
        else:
            header.append(mask_bit | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self, deadline: float) -> str | None:
        """读到一条文本消息；超时返回 None。"""
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            self.sock.settimeout(max(0.05, remaining))
            try:
                first = self._read_exact(2)
            except (socket.timeout, TimeoutError):
                return None
            except WSError:
                raise
            opcode = first[0] & 0x0F
            masked = bool(first[1] & 0x80)
            length = first[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                self._closed = True
                raise WSError("对端关闭")
            if opcode in (0x9, 0xA):
                continue
            return payload.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
        self._closed = True


@dataclass
class Target:
    url: str
    ws_url: str


class CDP:
    def __init__(self, port: int) -> None:
        self.port = port
        self.ws: WebSocket | None = None
        self._next_id = 0

    # ---------------------------------------------------------------- 连接

    @staticmethod
    def targets(port: int, timeout: float = 2.0) -> list[Target]:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/json/list")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return [
            Target(url=item.get("url", ""), ws_url=item.get("webSocketDebuggerUrl", ""))
            for item in data
            if item.get("type") == "page" and item.get("webSocketDebuggerUrl")
        ]

    def connect(self, target: Target) -> None:
        path = target.ws_url.split(f":{self.port}", 1)[-1]
        if not path.startswith("/"):
            path = "/" + path
        self.ws = WebSocket("127.0.0.1", self.port, path)

    def close(self) -> None:
        if self.ws:
            self.ws.close()
            self.ws = None

    # ---------------------------------------------------------------- 调用

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 6.0) -> dict[str, Any]:
        if not self.ws:
            raise WSError("未连接")
        self._next_id += 1
        message_id = self._next_id
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while True:
            text = self.ws.recv(deadline)
            if text is None:
                raise WSError(f"等待 {method} 超时")
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue
            if message.get("id") == message_id:
                if "error" in message:
                    raise WSError(message["error"].get("message", "CDP 错误"))
                return message.get("result", {})

    def evaluate(self, expression: str, timeout: float = 8.0) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": False,
                "returnByValue": True,
                "allowUnsafeEvalBlockedByCSP": True,
            },
            timeout=timeout,
        )
        return (result.get("result") or {}).get("value")
