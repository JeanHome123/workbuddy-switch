"""后台常驻：把面板注入客户端，并给面板提供本地接口。

一个进程干两件事：
  1. 起一个只监听 127.0.0.1 的小 HTTP 服务（档案列表 / 切换 / 新建 / 删除）；
  2. 等客户端启动后，通过 CDP 把 inject/panel.js 注进渲染进程。

没有客户端在跑时它只是定时轮询，CPU 接近 0；不依赖 node，也不开窗口。
"""

from __future__ import annotations

import atexit
import faulthandler
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from . import agent, cdp
from .core import Store


class Injector:
    def __init__(self, store: Store, api: str, token: str) -> None:
        self.store = store
        self.api = api
        self.token = token
        self.client: cdp.CDP | None = None
        self.last_check = 0.0
        self.injected = False
        self.avatar_captured: set[str] = set()
        self.panel_source = (Path(__file__).resolve().parent.parent / "inject" / "panel.js").read_text(
            encoding="utf-8"
        )
        # 用内容指纹判断页面里的是不是最新脚本，避免依赖版本号解析
        self.signature = hashlib.sha1(self.panel_source.encode("utf-8")).hexdigest()[:12]

    # ------------------------------------------------------------------ 数据

    def payload(self) -> str:
        state = self.store.state(include_sizes=False)
        profiles = [
            {
                "id": item["id"],
                "name": item["name"],
                "color": item["color"],
                "running": item["running"],
                "isDefault": item["isDefault"],
                "account": item["account"],
                "hasAvatar": item["hasAvatar"],
                "avatarVersion": item["avatarVersion"],
            }
            for item in state["profiles"]
        ]
        config = json.dumps({"api": self.api, "token": self.token})
        sig = json.dumps(self.signature)
        data = json.dumps(profiles, ensure_ascii=False)
        return (
            "(function(){window.__WBS_CFG__=" + config + ";window.__WBS_SIG__=" + sig + ";try{\n"
            + self.panel_source
            + "\n}catch(e){}try{window.__WBS_PANEL__&&window.__WBS_PANEL__.setProfiles("
            + data
            + ");}catch(e){}return true;})()"
        )

    # ---------------------------------------------------------------- 连接

    def _pick_target(self) -> cdp.Target | None:
        port = self.store.debug_port()
        targets = cdp.CDP.targets(port)
        skip = (
            "devtools://",
            "edge://",
            "chrome://",
            "about:",
            "chrome-extension://",
            "windowAppId=settings",
            "windowKind=settings",
            "windowKind=utility",
            "splash.html",
        )
        pages = [t for t in targets if not any(s in t.url for s in skip)]
        if not pages:
            return None
        # The main workbench always carries wbSeedToken/accountSnapshot.  A
        # settings or utility window may sort first, so never blindly pick [0].
        preferred = [
            t for t in pages if "wbseedtoken=" in t.url.lower() or "accountsnapshot=" in t.url.lower()
        ]
        for page in preferred:
            if "workbuddy" in page.url.lower() or "buddy" in page.url.lower():
                return page
        for page in pages:
            if "workbuddy" in page.url.lower() or "buddy" in page.url.lower():
                return page
        return preferred[0] if preferred else pages[0]

    def connect(self) -> bool:
        if self.client:
            return True
        try:
            target = self._pick_target()
            if not target:
                return False
            client = cdp.CDP(self.store.debug_port())
            client.connect(target)
            client.call("Runtime.enable")
            self.client = client
            # Page URLs can contain account snapshots and seed tokens.
            self.store.logger.info("已连上客户端渲染进程")
            return True
        except Exception as exc:
            self.store.logger.debug("连接渲染进程失败: %s", exc)
            self.disconnect()
            return False

    def disconnect(self) -> None:
        if self.client:
            self.client.close()
        self.client = None
        self.last_check = 0.0

    def inject(self) -> bool:
        if not self.client:
            return False
        try:
            self.client.evaluate(self.payload(), timeout=10)
            self.injected = True
            self.store.logger.info("已注入账号面板入口 sig=%s", self.signature)
            return True
        except Exception as exc:
            self.store.logger.warning("注入失败: %s", exc)
            self.disconnect()
            return False

    def page_state(self) -> dict[str, Any]:
        """One CDP round-trip for all injection health fields."""
        if not self.client:
            return {}
        value = self.client.evaluate(
            "(function(){var p=window.__WBS_PANEL__||{};var c=window.__WBS_CFG__||{};"
            "return {sig:window.__WBS_SIG__||'',panelSig:p.sig||'',version:p.version||0,"
            "api:c.api||'',token:c.token||''};})()",
            timeout=4,
        )
        return value if isinstance(value, dict) else {}

    def capture_avatar(self, running: dict[str, Any]) -> None:
        """Cache the real avatar shown by the client for the running profile."""
        profile_id = str(running.get("profileId") or "")
        if not profile_id or profile_id in self.avatar_captured or not self.client:
            return
        try:
            src = self.client.evaluate(
                "(function(){var r=document.querySelector('.user-menu-trigger');"
                "if(!r)return '';var i=r.querySelector('img.wb-avatar__img');"
                "return i&&i.src?i.src:'';})()",
                timeout=4,
            )
            if not isinstance(src, str) or not src:
                return
            try:
                profile = self.store.get(profile_id)
            except KeyError:
                return
            account = self.store.read_account(profile) or {}
            if self.store.save_avatar_from_src(profile_id, src, account.get("uid", ""), "live"):
                self.avatar_captured.add(profile_id)
                self.store.logger.info("已缓存账号头像 profile=%s", profile_id)
        except Exception as exc:
            self.store.logger.debug("读取账号头像失败: %s", exc)

    # ---------------------------------------------------------------- 主循环

    def tick(self) -> None:
        running = self.store.running_app()
        if not running:
            self.disconnect()
            self.injected = False
            return
        if not self.connect():
            return
        self.capture_avatar(running)
        now = time.time()
        if now - self.last_check < 4 and self.injected:
            return
        self.last_check = now
        try:
            page = self.page_state()
        except Exception as exc:
            self.store.logger.debug("读取注入状态失败: %s", exc)
            self.disconnect()
            return
        healthy = (
            page.get("sig") == self.signature
            and page.get("panelSig") == self.signature
            and page.get("version") == 21
            and page.get("api") == self.api
            and page.get("token") == self.token
        )
        if not healthy:
            # 脚本内容、面板版本或后台端口变了，摘掉旧的重新注入
            try:
                self.client.evaluate(
                    "(() => { const b = document.getElementById('wbs-entry-btn');"
                    " if (b) b.remove(); document.querySelectorAll('.wbs-layer').forEach(n => n.remove());"
                    " window.__WBS_PANEL__ = null; return 1; })()",
                    timeout=3,
                )
            except Exception as exc:
                self.store.logger.debug("清理旧面板失败: %s", exc)
                self.disconnect()
                return
            self.inject()
            return
        # 入口按钮只在账号菜单展开时才存在，所以用面板对象本身判断是否还活着
        if not page.get("panelSig"):
            self.inject()


def watch(store: Store, quiet: bool = False) -> None:
    if not store.acquire_singleton():
        if not quiet:
            print("切号器后台已在运行")
        return
    crash_log = open(store.logs_dir / "watch-crash.log", "a", buffering=1, encoding="utf-8")
    faulthandler.enable(file=crash_log, all_threads=True)
    store.logger.info("后台进程启动 pid=%s", __import__("os").getpid())
    atexit.register(lambda: store.logger.info("后台进程退出（atexit）"))

    httpd, info = agent.create_server(store)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    thread.start()
    store.logger.info("后台启动 pid=%s api=%s", info["pid"], info["port"])
    if not quiet:
        print(f"切号器后台已启动（pid {info['pid']}，接口 127.0.0.1:{info['port']}）")

    injector = Injector(store, f"http://127.0.0.1:{info['port']}", info["token"])
    beats = 0
    try:
        while True:
            try:
                injector.tick()
            except Exception as exc:  # 单次异常不影响常驻
                store.logger.warning("注入循环异常: %s", exc, exc_info=True)
                injector.disconnect()
            beats += 1
            if beats % 40 == 0:
                store.logger.info("心跳 pid=%s 已运行 %ss", info["pid"], beats * 1.5)
            time.sleep(1.5)
    except BaseException as exc:  # 记录一切退出原因
        store.logger.error("后台异常退出: %r", exc, exc_info=True)
        raise
    finally:
        store.logger.info("后台收尾")
        injector.disconnect()
        httpd.shutdown()
        store.release_singleton()
        crash_log.close()
