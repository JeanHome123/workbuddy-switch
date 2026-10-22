"""无界面控制端（agent）。

客户端里的注入面板通过它读档案列表、发起切换、新建/删除档案。
只监听 127.0.0.1，token 由切号器生成，写在 agent.json 里。
没有窗口、没有前端页面，进程常驻但几乎不占资源。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .core import Store, SwitchError

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class Handler(BaseHTTPRequestHandler):
    server_version = "WBSAgent/1.0"
    store: Store
    token: str

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # Request lines may include the API token in the query string.
        self.store.logger.debug("agent method=%s", self.command)

    # ------------------------------------------------------------------ 输出

    def _cors(self) -> None:
        origin = self.headers.get("Origin") or "*"
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "X-WBS-Token, Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        try:
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # The page intentionally aborts this request when a switch
                # closes the client; it is not a server-side failure.
                pass

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "error": message})

    def _image(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=60")
        self._cors()
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 200_000:
            raise ApiError("请求体过大", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            raise ApiError("请求体不是合法 JSON") from exc
        return data if isinstance(data, dict) else {}

    def _check_token(self, query: dict[str, list[str]]) -> None:
        provided = self.headers.get("X-WBS-Token") or (query.get("t", [""])[0])
        if not provided or not secrets.compare_digest(provided, self.token):
            raise ApiError("未授权", HTTPStatus.FORBIDDEN)

    # ------------------------------------------------------------------ 路由

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/state":
                self._check_token(query)
                include_sizes = query.get("sizes", ["0"])[0] != "0"
                return self._json(200, {"ok": True, **self.store.state(include_sizes=include_sizes)})
            if parsed.path == "/api/switch-op":
                self._check_token(query)
                op_id = (query.get("id", [""])[0] or "").strip()
                if not op_id:
                    # 不带 id：返回最近一次切换记录，供切换后的新档案页面
                    # 恢复结果（新档案的 localStorage 读不到旧档案的 opId）。
                    return self._json(200, {"ok": True, "operation": self.store.last_operation()})
                operation = self.store.get_operation(op_id)
                if operation is None:
                    raise ApiError("切换操作不存在", HTTPStatus.NOT_FOUND)
                return self._json(200, {"ok": True, "operation": operation})
            match = re.fullmatch(r"/api/avatar/([A-Za-z0-9_-]+)", parsed.path)
            if match:
                self._check_token(query)
                profile_id = match.group(1)
                self.store.get(profile_id)
                path = self.store.avatar_path(profile_id)
                if not path.is_file():
                    raise ApiError("头像不存在", HTTPStatus.NOT_FOUND)
                meta = self.store.avatar_meta(profile_id)
                return self._image(path.read_bytes(), meta.get("contentType") or "image/png")
            if parsed.path == "/api/ping":
                return self._json(200, {"ok": True, "version": 1, "tool": "wbs-agent"})
            raise ApiError("接口不存在", HTTPStatus.NOT_FOUND)
        except ApiError as exc:
            self._error(exc.status, exc.message)
        except Exception as exc:  # pragma: no cover
            self.store.logger.exception("GET %s 失败", parsed.path)
            self._error(500, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            self._check_token(query)
            payload = self._read_body()
            return self._json(200, {"ok": True, **(self._route(parsed.path, payload, query) or {})})
        except ApiError as exc:
            self._error(exc.status, exc.message)
        except SwitchError as exc:
            self._error(400, str(exc))
        except KeyError as exc:
            self._error(404, str(exc).strip("'\""))
        except Exception as exc:  # pragma: no cover
            self.store.logger.exception("POST %s 失败", parsed.path)
            self._error(500, str(exc))

    def _route(self, path: str, payload: dict[str, Any], query: dict[str, list[str]]) -> dict[str, Any] | None:
        if path == "/api/switch":
            profile_id = str(payload.get("id") or (query.get("id", [""])[0]) or "")
            if not profile_id:
                raise ApiError("缺少档案 id")
            # 先校验档案存在，404 能立刻回到面板，而不是等后台线程失败。
            self.store.get(profile_id)
            op_id = str(payload.get("opId") or (query.get("opId", [""])[0]) or "").strip()
            force = bool(payload.get("force")) or (
                query.get("force", ["0"])[0].lower() in ("1", "true", "yes")
            )
            if self.store.is_switch_busy():
                # 已有切换在跑：直接拒绝，避免前端收到“已受理”却查不到记录。
                raise ApiError("已有切换正在进行，请稍候", HTTPStatus.CONFLICT)
            operation, created = self.store.begin_operation(profile_id, op_id, force)
            if not created:
                # 同一操作重复提交：返回已有记录，不再执行。
                return {
                    "accepted": True,
                    "opId": operation["id"],
                    "duplicate": True,
                    "operation": operation,
                }

            def _run() -> None:
                try:
                    self.store.switch(
                        profile_id,
                        op_id=operation["id"],
                        force=force,
                        operation=operation,
                    )
                except SwitchError as exc:
                    self.store.logger.warning("切换失败 op=%s: %s", operation["id"], exc)
                except Exception:
                    self.store.logger.exception("切换异常 op=%s", operation["id"])

            # 切换会关闭客户端，同步等待响应的页面一定会被中断；
            # 操作记录已经同步落盘，这里回执 ID，结果由 /api/switch-op 查询。
            threading.Thread(
                target=_run, daemon=True, name=f"wbs-switch-{operation['id']}"
            ).start()
            return {"accepted": True, "opId": operation["id"]}
        if path == "/api/profiles":
            name = str(payload.get("name") or (query.get("name", [""])[0]) or "").strip()
            if not name:
                raise ApiError("请填写名称")
            profile = self.store.create_profile(name[:24], str(payload.get("color") or "#7C5CFC"))
            return {"profile": profile}
        match = re.fullmatch(r"/api/profiles/([A-Za-z0-9_-]+)/delete", path)
        if match:
            purge = str(payload.get("purge") or (query.get("purge", ["0"])[0])).lower() in ("1", "true", "yes")
            return self.store.delete_profile(match.group(1), purge=purge)
        raise ApiError("接口不存在", HTTPStatus.NOT_FOUND)


def create_server(store: Store, port: int = 0) -> tuple[ThreadingHTTPServer, dict[str, Any]]:
    """建好监听但不起线程，由调用方决定怎么跑。

    优先占用固定端口，这样后台重启后页面里缓存的接口地址依然有效；
    端口被占时退回随机端口。
    """
    token = store.agent_token()
    handler = type("BoundHandler", (Handler,), {"store": store, "token": token})
    preferred = port or store.agent_port()
    httpd: ThreadingHTTPServer | None = None
    for candidate in (preferred, 0):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), handler)
            break
        except OSError:
            continue
    if httpd is None:
        raise OSError("无法监听本地端口")
    httpd.daemon_threads = True
    info = {
        "port": httpd.server_address[1],
        "token": token,
        "pid": os.getpid(),
        "startedAt": int(time.time() * 1000),
    }
    store.agent_path().write_text(json.dumps(info), encoding="utf-8")
    return httpd, info


def serve(store: Store, port: int = 0, quiet: bool = False) -> None:
    httpd, info = create_server(store, port)
    if not quiet:
        print(f"控制端已启动：127.0.0.1:{info['port']}（pid {info['pid']}）")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        try:
            store.agent_path().unlink(missing_ok=True)
        except OSError:
            pass
