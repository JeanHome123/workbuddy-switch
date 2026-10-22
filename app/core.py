"""WorkBuddy 切号器核心逻辑。

设计要点
--------
1. 客户端主进程用 ``WORKBUDDY_CONFIG_DIR`` / ``WORKBUDDY_USER_DATA_DIR`` 决定
   配置目录与 Chromium 用户数据目录（见 resources/app.asar 内
   main/workbuddy-paths.js）。因此「一个档案 = 一套独立目录」即可获得完全
   隔离的登录态，无需接触任何明文凭据。
2. 客户端存在全局单实例约束，切换必须「关旧 → 换登录态 → 开新」。
3. 客户端还会把当前登录态导出到
   ``%LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\workbuddy-desktop.info``
   （供 CLI / 扩展复用）。该路径不随档案变化，所以在切换时按档案做快照搬运。
   快照内容本身是客户端加密后的信封，本工具从不解析或落盘明文令牌。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Iterable

from . import win32

APP_EXE_NAME = "WorkBuddy.exe"
DEFAULT_PROFILE_ID = "default"
DEFAULT_CONFIG_DIR = Path.home() / ".workbuddy"
SHARED_AUTH_DIR = (
    Path.home() / "AppData" / "Local" / "CodeBuddyExtension" / "Data" / "Public" / "auth"
)
TOOL_VERSION = "1.0.1"
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

Progress = Callable[[str, str, dict[str, Any] | None], None]


class SwitchError(RuntimeError):
    """切换过程中的可预期失败。"""


def now_ms() -> int:
    return int(time.time() * 1000)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _human_bytes(size: int) -> str:
    step = 1024.0
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < step or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TB"


OP_ID_RE = re.compile(r"[A-Za-z0-9-]{8,64}")


@contextlib.contextmanager
def cross_process_lock(path: Path, blocking: bool = False):
    """跨进程文件锁：Windows 用 msvcrt 字节区间锁，其他平台退回 flock。

    yield 一个 bool 表示是否拿到锁；进程退出（包括被强杀）时操作系统会
    自动释放，不需要陈锁清理。同进程的不同句柄在 Windows 上同样互斥。
    """
    acquired = False
    handle = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(
                    handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1
                )
            else:  # pragma: no cover - 仅非 Windows 开发环境
                import fcntl

                fcntl.flock(
                    handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                )
            acquired = True
        except OSError:
            acquired = False
        yield acquired
    finally:
        if acquired and handle is not None:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        if handle is not None:
            handle.close()


def _sanitize_op_id(op_id: str) -> str:
    candidate = (op_id or "").strip()
    return candidate if OP_ID_RE.fullmatch(candidate) else ""


@dataclass
class LaunchSpec:
    exe: str
    env: dict[str, str]
    cwd: str


class Store:
    def __init__(self, root: str | os.PathLike | None = None) -> None:
        base = root or os.environ.get("WBS_DATA_ROOT")
        if not base:
            local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            base = str(Path(local) / "WorkBuddySwitch")
        self.root = Path(base)
        self.settings_path = self.root / "settings.json"
        self.registry_path = self.root / "profiles.json"
        self.runtime_path = self.root / "runtime.json"
        self.sizes_path = self.root / "sizes.json"
        self.logs_dir = self.root / "logs"
        self.profiles_dir = self.root / "profiles"
        self.quarantine_dir = self.root / "quarantine"
        self.operations_dir = self.root / "operations"
        self.switch_lock_path = self.root / "switch.lock"
        self.registry_lock_path = self.root / "registry.lock"
        self.settings: dict[str, Any] = {}
        self._registry: dict[str, Any] = {"version": 1, "profiles": []}
        self._registry_mtime_ns = 0
        self._registry_lock = threading.RLock()
        self._sizes: dict[str, Any] = {}
        self._logger: logging.Logger | None = None
        self._asar_capability: bool | None = None
        self._asar_key: tuple[Any, ...] | None = None
        self._switch_lock = threading.Lock()
        self._avatar_attempts: dict[str, float] = {}
        self.ensure()

    # ------------------------------------------------------------------ 基础

    def ensure(self) -> None:
        for directory in (self.root, self.profiles_dir, self.logs_dir, self.operations_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.settings = _read_json(self.settings_path, {}) or {}
        defaults = {
            "version": 1,
            "appExe": "",
            "dpapi": True,
            "shareDirs": [],
            "graceSeconds": 12,
            "injectPanel": True,
            "debugPort": 9339,
            "agentPort": 9340,
            "authBasename": "workbuddy-desktop.info",
        }
        changed = False
        for key, value in defaults.items():
            if key not in self.settings:
                self.settings[key] = value
                changed = True
        if changed:
            _write_json(self.settings_path, self.settings)

        self._registry = _read_json(self.registry_path, None) or {"version": 1, "profiles": []}
        self._registry_mtime_ns = self._file_mtime_ns(self.registry_path)
        self._sizes = _read_json(self.sizes_path, {}) or {}
        if self._default_profile_needed():
            try:
                with self._registry_transaction():
                    if self._default_profile_needed():
                        self._insert_default_profile()
            except SwitchError:
                # 另一个进程正在写注册表：本次先跳过登记，下次启动会补上。
                self.logger.warning("注册表被占用，跳过默认档案登记")

    def _default_profile_needed(self) -> bool:
        profiles = self._registry.setdefault("profiles", [])
        if any(p.get("id") == DEFAULT_PROFILE_ID for p in profiles):
            return False
        return DEFAULT_CONFIG_DIR.exists()

    def _insert_default_profile(self) -> None:
        self._registry.setdefault("profiles", []).insert(
            0,
            {
                "id": DEFAULT_PROFILE_ID,
                "name": "默认档案",
                "kind": "default",
                "color": "#2B6CF6",
                "note": "已存在的安装（原样使用，不做任何搬迁）",
                "createdAt": now_ms(),
                "lastUsedAt": 0,
                "shareDirs": [],
            },
        )

    @contextlib.contextmanager
    def _registry_transaction(self):
        """注册表读—改—写整段持锁。

        只锁写入不够：两个实例先后读到同一份快照，各自追加后写回，
        后写的会覆盖先写的，导致账号从列表里消失。这里在锁内重新读取
        磁盘内容，改完再写；拿不到锁直接失败，绝不无锁写入。
        """
        with self._registry_lock:
            with cross_process_lock(self.registry_lock_path, blocking=True) as locked:
                if not locked:
                    raise SwitchError("注册表正被另一个进程占用，请稍后重试")
                data = _read_json(self.registry_path, None)
                if isinstance(data, dict) and isinstance(data.get("profiles"), list):
                    self._registry = data
                self._registry_mtime_ns = self._file_mtime_ns(self.registry_path)
                yield
                _write_json(self.registry_path, self._registry)
                self._registry_mtime_ns = self._file_mtime_ns(self.registry_path)

    def _profile_ref(self, profile_id: str) -> dict[str, Any]:
        for profile in self._registry.get("profiles", []):
            if profile.get("id") == profile_id:
                return profile
        raise KeyError(f"档案不存在: {profile_id}")

    def _save_settings(self) -> None:
        _write_json(self.settings_path, self.settings)

    @property
    def logger(self) -> logging.Logger:
        if self._logger is None:
            logger = logging.getLogger("wbs")
            logger.setLevel(logging.INFO)
            if not logger.handlers:
                handler = RotatingFileHandler(
                    self.logs_dir / "switcher.log",
                    maxBytes=1_000_000,
                    backupCount=3,
                    encoding="utf-8",
                )
                handler.setFormatter(
                    logging.Formatter("%(asctime)s %(levelname)s %(message)s")
                )
                logger.addHandler(handler)
            self._logger = logger
        return self._logger

    # ------------------------------------------------------------- 档案模型

    @staticmethod
    def _file_mtime_ns(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    def _reload_registry_if_needed(self) -> None:
        """Pick up profile changes made by the CLI or another process.

        The background agent is long-lived, while ``wbs.py new/rm`` may run in
        a different process.  Without this refresh the injected panel would
        keep showing the profile list captured when the agent started.
        """
        mtime = self._file_mtime_ns(self.registry_path)
        if not mtime or mtime == self._registry_mtime_ns:
            return
        with self._registry_lock:
            mtime = self._file_mtime_ns(self.registry_path)
            if not mtime or mtime == self._registry_mtime_ns:
                return
            data = _read_json(self.registry_path, None)
            if isinstance(data, dict) and isinstance(data.get("profiles"), list):
                self._registry = data
                self._registry_mtime_ns = mtime

    @property
    def profiles(self) -> list[dict[str, Any]]:
        self._reload_registry_if_needed()
        return self._registry.get("profiles", [])

    def get(self, profile_id: str) -> dict[str, Any]:
        for profile in self.profiles:
            if profile.get("id") == profile_id:
                return profile
        raise KeyError(f"档案不存在: {profile_id}")

    def find(self, keyword: str) -> dict[str, Any]:
        keyword = (keyword or "").strip()
        if not keyword:
            raise KeyError("请提供档案 id 或名称")
        for profile in self.profiles:
            if profile.get("id") == keyword:
                return profile
        matches = [p for p in self.profiles if keyword in (p.get("name") or "")]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise KeyError(f"未找到档案: {keyword}")
        names = "、".join(p.get("name", "") for p in matches)
        raise KeyError(f"名称不唯一，请用 id 指定：{names}")

    def config_dir(self, profile: dict[str, Any]) -> Path:
        if profile.get("kind") == "default":
            return Path(profile.get("configDir") or DEFAULT_CONFIG_DIR)
        return self.profiles_dir / profile["id"] / "config"

    def user_data_dir(self, profile: dict[str, Any]) -> Path:
        return self.config_dir(profile) / "app"

    def profile_root(self, profile_id: str) -> Path:
        return self.profiles_dir / profile_id

    def auth_store(self, profile_id: str) -> Path:
        return self.profiles_dir / profile_id / "auth"

    def create_profile(self, name: str, color: str = "#7C5CFC") -> dict[str, Any]:
        name = (name or "").strip() or "新档案"
        profile_id = "p" + uuid.uuid4().hex[:8]
        with self._registry_transaction():
            profile = {
                "id": profile_id,
                "name": name,
                "kind": "isolated",
                "color": color,
                "note": "",
                "createdAt": now_ms(),
                "lastUsedAt": 0,
                "shareDirs": list(self.settings.get("shareDirs") or []),
            }
            self._registry.setdefault("profiles", []).append(profile)
        self.config_dir(profile).mkdir(parents=True, exist_ok=True)
        self._link_shared_dirs(profile)
        self.logger.info("创建档案 %s (%s)", name, profile_id)
        return profile

    def update_profile(self, profile_id: str, **fields: Any) -> dict[str, Any]:
        with self._registry_transaction():
            profile = self._profile_ref(profile_id)
            for key in ("name", "color", "note", "shareDirs"):
                if key in fields and fields[key] is not None:
                    profile[key] = fields[key]
        return profile

    def delete_profile(self, profile_id: str, purge: bool = False) -> dict[str, Any]:
        self.get(profile_id)
        running = self.running_app()
        if running and running.get("profileId") == profile_id:
            raise SwitchError("该档案正在运行，请先切换到其他档案")
        with self._registry_transaction():
            profile = self._profile_ref(profile_id)
            if profile.get("kind") == "default":
                raise SwitchError("默认档案对应现有安装，不能删除")
            self._registry["profiles"] = [
                p for p in self._registry.get("profiles", []) if p.get("id") != profile_id
            ]
        removed = False
        if purge:
            target = self.profile_root(profile_id)
            if target.exists() and self._is_inside_root(target):
                shutil.rmtree(target, ignore_errors=True)
                removed = True
        self._sizes.pop(profile_id, None)
        _write_json(self.sizes_path, self._sizes)
        self.logger.info("删除档案 %s purge=%s", profile_id, purge)
        return {"deleted": True, "purged": removed}

    def _is_inside_root(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root.resolve())
            return True
        except ValueError:
            return False

    def link_shared_dirs(self, profile_id: str) -> dict[str, Any]:
        """为已有档案补建共享目录联接（binaries 等）。需要开启共享设置。"""
        self.get(profile_id)
        dirs = list(self.settings.get("shareDirs") or [])
        if not dirs:
            raise SwitchError("未开启共享目录，请先在设置里选择要共享的目录")
        with self._registry_transaction():
            profile = self._profile_ref(profile_id)
            if profile.get("kind") == "default":
                raise SwitchError("默认档案不需要共享目录")
            profile["shareDirs"] = dirs
        self._link_shared_dirs(profile)
        linked = []
        for name in dirs:
            dest = self.config_dir(profile) / name
            if dest.exists():
                linked.append({"name": name, "path": str(dest)})
        return {"linked": linked, "requested": dirs}

    def _link_shared_dirs(self, profile: dict[str, Any]) -> None:
        """把体积大且与账号无关的运行时缓存用目录联接共享，避免重复下载。"""
        config = self.config_dir(profile)
        for name in profile.get("shareDirs") or []:
            source = DEFAULT_CONFIG_DIR / name
            dest = config / name
            if dest.exists() or not source.is_dir():
                continue
            try:
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(dest), str(source)],
                    capture_output=True,
                    creationflags=CREATE_NO_WINDOW,
                    timeout=20,
                )
                self.logger.info("共享目录 %s -> %s", dest, source)
            except Exception as exc:  # 共享失败不影响主流程
                self.logger.warning("创建目录联接失败 %s: %s", name, exc)

    # --------------------------------------------------------- 账号信息读取

    def read_account(self, profile: dict[str, Any]) -> dict[str, Any] | None:
        config = self.config_dir(profile)
        snapshot = config / "storage" / "skeleton" / "account-snapshot.json"
        data = _read_json(snapshot, None)
        if isinstance(data, dict):
            primary = data.get("primary") or {}
            if primary.get("uid"):
                return {
                    "uid": primary.get("uid", ""),
                    "nickname": primary.get("nickname", ""),
                    "type": primary.get("type", ""),
                    "edition": primary.get("editionType", ""),
                    "pro": bool(primary.get("isPro")),
                    "avatarUrl": primary.get("avatarUrl", ""),
                    "savedAt": primary.get("savedAt") or 0,
                }
        return None

    # ------------------------------------------------------------ 账号头像

    def avatar_path(self, profile_id: str) -> Path:
        return self.profile_root(profile_id) / "avatar.bin"

    def avatar_meta_path(self, profile_id: str) -> Path:
        return self.profile_root(profile_id) / "avatar.json"

    def avatar_meta(self, profile_id: str) -> dict[str, Any]:
        data = _read_json(self.avatar_meta_path(profile_id), None)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _image_type(raw: bytes, content_type: str = "") -> str:
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if raw.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if raw.startswith(b"GIF8"):
            return "image/gif"
        if len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            return "image/webp"
        return ""

    def _store_avatar(
        self,
        profile_id: str,
        raw: bytes,
        uid: str,
        content_type: str = "",
        source: str = "",
    ) -> bool:
        if not raw or len(raw) > 4 * 1024 * 1024:
            return False
        image_type = self._image_type(raw, content_type)
        if not image_type:
            return False
        digest = hashlib.sha256(raw).hexdigest()
        previous = self.avatar_meta(profile_id)
        path = self.avatar_path(profile_id)
        if path.is_file() and previous.get("hash") == digest and previous.get("uid") == uid:
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, path)
        _write_json(
            self.avatar_meta_path(profile_id),
            {
                "version": 1,
                "uid": uid,
                "contentType": image_type,
                "hash": digest,
                "savedAt": now_ms(),
                "source": source,
            },
        )
        self.logger.info("保存账号头像 profile=%s bytes=%s", profile_id, len(raw))
        return True

    def _download_avatar(self, url: str) -> tuple[bytes, str]:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            content_type = response.headers.get_content_type() or ""
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("头像文件过大")
        return raw, content_type

    def save_avatar_from_src(self, profile_id: str, src: str, uid: str = "", source: str = "live") -> bool:
        """Cache a real avatar from a data: URI or an http(s) URL."""
        src = (src or "").strip()
        if not src:
            return False
        try:
            if src.startswith("data:image/"):
                header, payload = src.split(",", 1)
                content_type = header[5:].split(";", 1)[0].lower()
                if ";base64" in header.lower():
                    raw = base64.b64decode(payload, validate=True)
                else:
                    from urllib.parse import unquote_to_bytes

                    raw = unquote_to_bytes(payload)
            elif src.startswith(("http://", "https://")):
                raw, content_type = self._download_avatar(src)
            else:
                return False
            return self._store_avatar(profile_id, raw, uid, content_type, source)
        except Exception as exc:
            self.logger.debug("保存账号头像失败 profile=%s: %s", profile_id, exc)
            return False

    def ensure_avatar(self, profile: dict[str, Any], account: dict[str, Any] | None) -> bool:
        """Ensure a cached avatar matches the account currently recorded."""
        if not account or not account.get("uid"):
            return False
        meta = self.avatar_meta(profile["id"])
        path = self.avatar_path(profile["id"])
        if path.is_file() and meta.get("uid") == account["uid"] and meta.get("hash"):
            return True
        url = account.get("avatarUrl") or ""
        if not url:
            return False
        key = f"{profile['id']}:{account['uid']}"
        now = time.time()
        if now - self._avatar_attempts.get(key, 0) < 300:
            return False
        self._avatar_attempts[key] = now
        try:
            raw, content_type = self._download_avatar(url)
            return self._store_avatar(profile["id"], raw, account["uid"], content_type, "snapshot")
        except Exception as exc:
            self.logger.debug("下载账号头像失败 profile=%s: %s", profile["id"], exc)
            return False

    def avatar_info(self, profile: dict[str, Any], account: dict[str, Any] | None) -> dict[str, Any]:
        if not self.ensure_avatar(profile, account):
            return {"exists": False, "version": ""}
        meta = self.avatar_meta(profile["id"])
        return {
            "exists": bool(meta.get("hash") and self.avatar_path(profile["id"]).is_file()),
            "version": (meta.get("hash") or "")[:16],
            "contentType": meta.get("contentType") or "image/png",
        }

    def dir_size(self, profile: dict[str, Any], ttl_ms: int = 300_000) -> int:
        profile_id = profile["id"]
        cached = self._sizes.get(profile_id) or {}
        if cached and now_ms() - cached.get("at", 0) < ttl_ms:
            return int(cached.get("bytes", 0))
        target = self.profile_root(profile_id) if profile.get("kind") != "default" else self.config_dir(profile)
        size = self._walk_size(target)
        self._sizes[profile_id] = {"bytes": size, "at": now_ms()}
        _write_json(self.sizes_path, self._sizes)
        return size

    @staticmethod
    def _walk_size(path: Path, max_entries: int = 300_000) -> int:
        if not path.exists():
            return 0
        total = 0
        seen = 0
        stack = [str(path)]
        while stack and seen < max_entries:
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        seen += 1
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            else:
                                total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
                        if seen >= max_entries:
                            break
            except OSError:
                continue
        return total

    # ------------------------------------------------------- 客户端可执行文件

    @staticmethod
    def _exe_exists(path: str | None) -> bool:
        """路径存在性检查要容错：无权限/被占用时视为不存在，而不是抛异常。"""
        if not path:
            return False
        try:
            return Path(path).is_file()
        except OSError:
            return False

    def find_app_exe(self) -> str:
        configured = (self.settings.get("appExe") or "").strip()
        if configured and self._exe_exists(configured):
            return configured
        running = self.running_app()
        if running:
            path = win32.process_image_path(running["pid"])
            if path and self._exe_exists(path):
                return path
        for proc in win32.list_processes():
            if proc.exe.lower() == APP_EXE_NAME.lower():
                path = win32.process_image_path(proc.pid)
                if path and self._exe_exists(path):
                    return path
        candidates: list[str] = []
        for env_key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env_key)
            if not base:
                continue
            candidates.append(str(Path(base) / "WorkBuddy" / APP_EXE_NAME))
            candidates.append(str(Path(base) / "Programs" / "WorkBuddy" / APP_EXE_NAME))
        candidates += [
            "C:/Program Files/WorkBuddy/WorkBuddy.exe",
        ]
        for candidate in candidates:
            if candidate and self._exe_exists(candidate):
                return candidate
        registry_path = self._find_from_registry()
        if registry_path:
            return registry_path
        return ""

    @staticmethod
    def _find_from_registry() -> str:
        try:
            import winreg
        except Exception:
            return ""
        roots = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, key_path in roots:
            try:
                with winreg.OpenKey(hive, key_path) as root_key:
                    for index in range(winreg.QueryInfoKey(root_key)[0]):
                        try:
                            sub_name = winreg.EnumKey(root_key, index)
                            with winreg.OpenKey(root_key, sub_name) as sub:
                                display = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                                if "workbuddy" not in display.lower():
                                    continue
                                location = ""
                                try:
                                    location = str(winreg.QueryValueEx(sub, "InstallLocation")[0])
                                except OSError:
                                    pass
                                if location:
                                    exe = Path(location) / APP_EXE_NAME
                                    if exe.is_file():
                                        return str(exe)
                                icon = ""
                                try:
                                    icon = str(winreg.QueryValueEx(sub, "DisplayIcon")[0])
                                except OSError:
                                    pass
                                if icon:
                                    exe = Path(icon.split(",")[0].strip('"'))
                                    if exe.is_file():
                                        return str(exe)
                        except OSError:
                            continue
            except OSError:
                continue
        return ""

    def app_version(self) -> str:
        for base in (DEFAULT_CONFIG_DIR, self.root):
            data = _read_json(base / "last-launch.json", None)
            if isinstance(data, dict) and data.get("version"):
                return str(data["version"])
        return ""

    # ----------------------------------------------------------- 运行态检测

    def _wb_processes(self) -> list[win32.Proc]:
        return [p for p in win32.list_processes() if p.exe.lower() == APP_EXE_NAME.lower()]

    def _main_process(self) -> win32.Proc | None:
        procs = self._wb_processes()
        if not procs:
            return None
        ids = {p.pid for p in procs}
        candidates = [p for p in procs if p.ppid not in ids]
        if not candidates:
            candidates = procs
        if len(candidates) == 1:
            return candidates[0]
        children: dict[int, int] = {}
        for proc in procs:
            children[proc.ppid] = children.get(proc.ppid, 0) + 1
        return max(candidates, key=lambda p: children.get(p.pid, 0))

    def running_app(self) -> dict[str, Any] | None:
        main = self._main_process()
        if not main:
            return None
        runtime = _read_json(self.runtime_path, {}) or {}
        profile_id = "default"
        manual = True
        if runtime.get("pid") == main.pid and runtime.get("profileId"):
            profile_id = runtime["profileId"]
            manual = False
        elif runtime.get("profileId") and runtime.get("pid") and win32.is_alive(int(runtime["pid"])):
            profile_id = runtime["profileId"]
            manual = False
        else:
            # The user may have closed the client and opened it again from the
            # normal shortcut.  In that case runtime.json is stale, but the
            # last selected profile was persisted in the user environment.
            profile_id = self.active_profile_from_env()
        try:
            profile = self.get(profile_id)
            name = profile.get("name", profile_id)
        except KeyError:
            profile_id, name = "default", "默认档案"
        return {
            "pid": main.pid,
            "profileId": profile_id,
            "profileName": name,
            "manual": manual,
            "exe": win32.process_image_path(main.pid),
        }

    def _descendants(self, root_pid: int) -> list[int]:
        procs = self._wb_processes()
        result: list[int] = []
        frontier = [root_pid]
        while frontier:
            current = frontier.pop()
            for proc in procs:
                if proc.ppid == current and proc.pid not in result:
                    result.append(proc.pid)
                    frontier.append(proc.pid)
        return result

    def close_app(self, grace_seconds: int | None = None, force: bool = False) -> dict[str, Any]:
        """请求客户端退出。

        默认（force=False）只在宽限时间内等待正常退出。超时后先看还有没有
        可见窗口：
        - 窗口都已关闭、进程还留在托盘：用户视角已经退出，继续结束残留进程；
        - 仍有可见窗口：客户端可能停在任务界面，返回 ``close-timeout``，
          由用户主动选择强制切换，不擅自强杀。
        """
        main = self._main_process()
        if not main:
            return {"closed": False, "reason": "not-running", "forced": False}
        grace = int(grace_seconds if grace_seconds is not None else self.settings.get("graceSeconds", 12))
        windows = win32.close_windows(main.pid)
        self.logger.info("请求关闭客户端 pid=%s windows=%s force=%s", main.pid, windows, force)
        deadline = time.time() + max(0, grace)
        started = time.time()
        tray_only = False
        while time.time() < deadline:
            if not win32.is_alive(main.pid):
                return {"closed": True, "forced": False, "pid": main.pid}
            # WM_CLOSE 后窗口缩到托盘时，不必等满整个宽限期：窗口已经没了，
            # 用户视角就是“已关闭”，可以直接结束残留进程继续切换。
            if not force and time.time() - started > 2.0:
                if win32.count_visible_windows(main.pid) == 0:
                    tray_only = True
                    self.logger.info("客户端窗口已关闭（托盘残留），提前结束等待 pid=%s", main.pid)
                    break
            time.sleep(0.3)
        if not force and not tray_only:
            visible = win32.count_visible_windows(main.pid)
            if visible > 0:
                self.logger.warning(
                    "客户端未在 %ss 内退出且仍有 %s 个可见窗口，按用户设置不强杀 pid=%s",
                    grace,
                    visible,
                    main.pid,
                )
                return {
                    "closed": False,
                    "forced": False,
                    "pid": main.pid,
                    "reason": "close-timeout",
                    "message": "客户端未在宽限时间内退出（还有可见窗口，可能有任务在运行）",
                }
            # 窗口都关了，只剩托盘里的进程：继续结束它，否则切换会卡在
            # “窗口消失但没开新窗口”的状态，用户也没有地方选强制切换。
            tray_only = True
            self.logger.warning(
                "客户端窗口已全部关闭但进程仍在托盘，继续结束残留进程 pid=%s", main.pid
            )
        forced_pids = []
        for pid in [main.pid] + self._descendants(main.pid):
            if win32.is_alive(pid) and win32.terminate(pid):
                forced_pids.append(pid)
        deadline = time.time() + 8
        while time.time() < deadline and win32.is_alive(main.pid):
            time.sleep(0.2)
        still = win32.is_alive(main.pid)
        self.logger.warning("强制结束客户端 pids=%s still_alive=%s", forced_pids, still)
        message = ""
        if not forced_pids:
            message = "客户端未响应关闭请求，且无法结束进程"
        elif tray_only:
            message = "客户端已关闭窗口但仍在托盘运行，已结束后台进程以完成切换"
        else:
            message = "客户端未响应关闭请求，已强制结束"
        return {
            "closed": not still,
            "forced": True,
            "pid": main.pid,
            "killed": forced_pids,
            "message": message,
        }

    # --------------------------------------------------------- 登录态快照

    def _auth_basename(self) -> str:
        return (self.settings.get("authBasename") or "workbuddy-desktop.info").strip()

    def live_auth_files(self) -> list[Path]:
        if not SHARED_AUTH_DIR.is_dir():
            return []
        base = self._auth_basename()
        files = [p for p in SHARED_AUTH_DIR.iterdir() if p.is_file() and p.name.startswith(base)]
        if files:
            return sorted(files)
        infos = [p for p in SHARED_AUTH_DIR.glob("*.info") if p.is_file()]
        if len(infos) == 1:
            single = infos[0]
            sidecars = [p for p in SHARED_AUTH_DIR.iterdir() if p.is_file() and p.name.startswith(single.name)]
            return sorted(set(sidecars) | {single})
        return []

    def save_auth(self, profile_id: str) -> dict[str, Any]:
        """Snapshot the client's encrypted auth export for one profile.

        The previous snapshot is only replaced after the new one is complete.
        If the live file is absent, keep a known-good snapshot instead of
        overwriting it with an empty manifest.
        """
        store = self.auth_store(profile_id)
        files = self.live_auth_files()
        previous = self.read_manifest(profile_id)
        if not files and previous and not previous.get("empty") and previous.get("files"):
            self.logger.warning(
                "当前没有可归档的登录态文件，保留 profile=%s 的旧快照（savedAt=%s）",
                profile_id,
                previous.get("savedAt", 0),
            )
            return {**previous, "preserved": True}

        tmp = store.with_name(store.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, Any] = {"version": 1, "savedAt": now_ms(), "empty": not files, "files": []}
        use_dpapi = bool(self.settings.get("dpapi", True)) and win32.dpapi_available()
        for source in files:
            raw = source.read_bytes()
            entry: dict[str, Any] = {"name": source.name, "size": len(raw), "dpapi": False}
            if use_dpapi:
                stored_name = source.name + ".dpapi"
                (tmp / stored_name).write_bytes(win32.dpapi_protect(raw))
                entry.update({"stored": stored_name, "dpapi": True})
            else:
                (tmp / source.name).write_bytes(raw)
                entry["stored"] = source.name
            manifest["files"].append(entry)
        if files:
            for profile in self.profiles:
                if profile["id"] == profile_id:
                    account = self.read_account(profile)
                    if account:
                        manifest["account"] = {
                            "uid": account["uid"],
                            "nickname": account["nickname"],
                        }
        _write_json(tmp / "manifest.json", manifest)

        old = store.with_name(store.name + ".old")
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        moved_old = False
        try:
            if store.exists():
                os.replace(store, old)
                moved_old = True
            os.replace(tmp, store)
        except Exception:
            if moved_old and not store.exists() and old.exists():
                os.replace(old, store)
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        shutil.rmtree(old, ignore_errors=True)
        self.logger.info(
            "保存登录态 profile=%s files=%s empty=%s", profile_id, len(files), manifest["empty"]
        )
        return manifest

    def read_manifest(self, profile_id: str) -> dict[str, Any] | None:
        return _read_json(self.auth_store(profile_id) / "manifest.json", None)

    def validate_snapshot(self, profile: dict[str, Any]) -> None:
        manifest = self.read_manifest(profile["id"])
        if not manifest or manifest.get("empty"):
            return
        store = self.auth_store(profile["id"])
        for entry in manifest.get("files", []):
            stored = store / entry.get("stored", "")
            if not stored.is_file():
                raise SwitchError(f"登录态快照缺少文件：{entry.get('name')}")
            if entry.get("dpapi"):
                try:
                    win32.dpapi_unprotect(stored.read_bytes())
                except Exception as exc:
                    raise SwitchError(
                        "登录态快照无法用当前 Windows 账户解密，请在该档案下重新登录一次。"
                    ) from exc

    def _quarantine_live(self, reason: str) -> Path | None:
        files = self.live_auth_files()
        if not files:
            return None
        target = self.quarantine_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{reason}"
        target.mkdir(parents=True, exist_ok=True)
        for item in files:
            try:
                shutil.move(str(item), str(target / item.name))
            except Exception as exc:
                self.logger.warning("隔离登录态文件失败 %s: %s", item, exc)
        self._prune_quarantine()
        return target

    def _prune_quarantine(self, keep: int = 20) -> None:
        if not self.quarantine_dir.is_dir():
            return
        dirs = sorted([p for p in self.quarantine_dir.iterdir() if p.is_dir()])
        for old in dirs[:-keep]:
            shutil.rmtree(old, ignore_errors=True)

    def restore_auth(self, profile: dict[str, Any]) -> dict[str, Any]:
        manifest = self.read_manifest(profile["id"])
        # Validate the snapshot before touching the live files.  A broken DPAPI
        # blob must never leave the user logged out.
        if manifest and not manifest.get("empty"):
            self.validate_snapshot(profile)
        quarantined = self._quarantine_live(profile["id"])
        if not manifest:
            if profile.get("kind") == "default":
                return {"restored": False, "reason": "no-snapshot-default"}
            return {"restored": False, "reason": "no-snapshot", "quarantined": str(quarantined or "")}
        if manifest.get("empty"):
            return {"restored": False, "reason": "logged-out", "quarantined": str(quarantined or "")}
        SHARED_AUTH_DIR.mkdir(parents=True, exist_ok=True)
        store = self.auth_store(profile["id"])
        for entry in manifest.get("files", []):
            stored = store / entry.get("stored", "")
            raw = stored.read_bytes()
            if entry.get("dpapi"):
                raw = win32.dpapi_unprotect(raw)
            (SHARED_AUTH_DIR / entry["name"]).write_bytes(raw)
        return {"restored": True, "files": len(manifest.get("files", []))}

    # ------------------------------------------------------------- 启动客户端

    def active_profile_from_env(self) -> str:
        """Infer the last selected profile for a manually started client."""
        configured = (win32.get_user_env("WORKBUDDY_CONFIG_DIR") or "").strip()
        if configured:
            try:
                target = Path(configured).resolve()
            except OSError:
                target = Path(configured)
            for profile in self.profiles:
                if profile.get("kind") == "default":
                    continue
                try:
                    if self.config_dir(profile).resolve() == target:
                        return profile["id"]
                except OSError:
                    continue
        return DEFAULT_PROFILE_ID

    def set_active_profile_env(self, profile: dict[str, Any]) -> bool:
        """Persist the selected profile for launches outside this tool.

        WorkBuddy's normal shortcut inherits the user environment.  Without
        this, a manual restart would use the default config while the shared
        auth file still belongs to another profile, contaminating the default
        account with the wrong login.

        返回是否两个变量都写入成功；失败必须由调用方处理，不能当成成功。
        """
        try:
            if profile.get("kind") == "default":
                ok = win32.set_user_env("WORKBUDDY_CONFIG_DIR", None)
                ok = win32.set_user_env("WORKBUDDY_USER_DATA_DIR", None) and ok
            else:
                ok = win32.set_user_env("WORKBUDDY_CONFIG_DIR", str(self.config_dir(profile)))
                ok = (
                    win32.set_user_env("WORKBUDDY_USER_DATA_DIR", str(self.user_data_dir(profile)))
                    and ok
                )
            if not ok:
                self.logger.error("写入当前档案环境变量失败 profile=%s", profile.get("id"))
            return bool(ok)
        except Exception as exc:
            self.logger.warning("写入当前档案环境变量失败: %s", exc)
            return False

    def build_env(self, profile: dict[str, Any]) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("WORKBUDDY_", "CODEBUDDY_"))}
        env.pop("ELECTRON_RUN_AS_NODE", None)
        if self.settings.get("injectPanel", True):
            env["WORKBUDDY_REMOTE_DEBUGGING_PORT"] = str(self.debug_port())
        if profile.get("kind") != "default":
            config = str(self.config_dir(profile))
            env["WORKBUDDY_CONFIG_DIR"] = config
            env["CODEBUDDY_CONFIG_DIR"] = config
            env["WORKBUDDY_USER_DATA_DIR"] = str(Path(config) / "app")
        return env

    # --------------------------------------------------------- 注入与代理

    def agent_path(self) -> Path:
        return self.root / "agent.json"

    def debug_port(self) -> int:
        try:
            return int(self.settings.get("debugPort") or 9339)
        except (TypeError, ValueError):
            return 9339

    def agent_port(self) -> int:
        """后台接口用固定端口，重启后页面里的旧配置依然有效。"""
        try:
            return int(self.settings.get("agentPort") or 9340)
        except (TypeError, ValueError):
            return 9340

    def agent_token(self) -> str:
        """固定 token，保证后台重启后已注入的面板仍然可用。"""
        token = (self.settings.get("agentToken") or "").strip()
        if not token:
            import secrets

            token = secrets.token_urlsafe(18)
            self.settings["agentToken"] = token
            self._save_settings()
        return token

    def debug_args(self) -> list[str]:
        return [
            f"--remote-debugging-port={self.debug_port()}",
            "--remote-allow-origins=*",
        ]

    def agent_info(self) -> dict[str, Any] | None:
        info = _read_json(self.agent_path(), None)
        if not isinstance(info, dict):
            return None
        if not info.get("port") or not info.get("token"):
            return None
        if info.get("pid") and not win32.is_alive(int(info["pid"])):
            return None
        return info

    def acquire_singleton(self) -> bool:
        return self.agent_info() is None

    def release_singleton(self) -> None:
        info = _read_json(self.agent_path(), None)
        if isinstance(info, dict) and int(info.get("pid") or 0) == os.getpid():
            try:
                self.agent_path().unlink(missing_ok=True)
            except OSError:
                pass

    def ensure_watcher(self, wait_seconds: float = 6.0) -> dict[str, Any] | None:
        """确保后台注入进程在跑（随登录自启，这里只是兜底拉起）。"""
        info = self.agent_info()
        if info:
            return info
        exe = sys.executable
        if exe.lower().endswith("python.exe"):
            pythonw = str(Path(exe).with_name("pythonw.exe"))
            if Path(pythonw).is_file():
                exe = pythonw
        script = str(Path(__file__).resolve().parent.parent / "wbs.py")
        base_flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        last_error: Exception | None = None
        for flags in (base_flags | CREATE_BREAKAWAY_FROM_JOB, base_flags):
            try:
                subprocess.Popen(
                    [exe, script, "watch", "--quiet"],
                    cwd=str(Path(script).parent),
                    creationflags=flags,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
                last_error = None
                break
            except OSError as exc:
                last_error = exc
        if last_error is not None:
            self.logger.warning("启动后台进程失败: %s", last_error)
            return None
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            time.sleep(0.25)
            info = self.agent_info()
            if info:
                return info
        return None

    def launch_profile(self, profile: dict[str, Any], wait_seconds: int = 30) -> dict[str, Any]:
        exe = self.find_app_exe()
        if not exe:
            raise SwitchError("未找到 WorkBuddy.exe，请在设置里指定客户端路径")
        if not self.set_active_profile_env(profile):
            raise SwitchError(
                "无法写入当前档案的启动环境变量，已取消启动（避免下次手动启动使用错误配置目录）"
            )
        spec = LaunchSpec(exe=exe, env=self.build_env(profile), cwd=str(Path(exe).parent))
        self.config_dir(profile).mkdir(parents=True, exist_ok=True)
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

        command = [spec.exe]
        debug_port = 0
        if self.settings.get("injectPanel", True):
            self.ensure_watcher()
            debug_port = self.debug_port()
            command += self.debug_args()

        process = subprocess.Popen(
            command,
            cwd=spec.cwd,
            env=spec.env,
            creationflags=flags,
            close_fds=True,
        )
        pid = process.pid
        deadline = time.time() + wait_seconds
        detected: win32.Proc | None = None
        while time.time() < deadline:
            if not win32.is_alive(pid):
                break
            main = self._main_process()
            if main and (main.pid == pid or main.pid in self._descendants(pid) or pid in self._descendants(main.pid)):
                detected = main
                break
            time.sleep(0.25)
        if detected is None:
            # 启动命令已发出但没有出现可识别的主进程：不写 runtime、不更新
            # lastUsedAt，不把这一步记成成功，交由上层走回滚。
            self.logger.error(
                "未检测到客户端主进程 pid=%s exe=%s", pid, exe
            )
            raise SwitchError("客户端已启动但未检测到主进程，切换中止")
        runtime = {
            "profileId": profile["id"],
            "profileName": profile.get("name", ""),
            "pid": detected.pid,
            "startedAt": now_ms(),
            "exe": exe,
            "debugPort": debug_port,
            "agentPort": (self.agent_info() or {}).get("port", 0),
        }
        _write_json(self.runtime_path, runtime)
        try:
            with self._registry_transaction():
                self._profile_ref(profile["id"])["lastUsedAt"] = now_ms()
        except (KeyError, SwitchError) as exc:
            # 档案刚被另一个进程删除、或注册表被占：不影响已经成功的启动。
            self.logger.warning("更新 lastUsedAt 失败 profile=%s: %s", profile["id"], exc)
            profile["lastUsedAt"] = now_ms()
        self.logger.info("启动客户端 profile=%s pid=%s", profile["id"], runtime["pid"])
        return {"pid": runtime["pid"], "detected": True, "exe": exe}

    # ------------------------------------------------------------ 切换操作记录

    def _op_path(self, op_id: str) -> Path:
        return self.operations_dir / f"{_sanitize_op_id(op_id)}.json"

    def get_operation(self, op_id: str) -> dict[str, Any] | None:
        """按操作 ID 读取持久化的切换记录；不存在或非法返回 None。"""
        op_id = _sanitize_op_id(op_id)
        if not op_id:
            return None
        data = _read_json(self._op_path(op_id), None)
        return data if isinstance(data, dict) else None

    def _save_operation(self, operation: dict[str, Any]) -> None:
        operation["updatedAt"] = now_ms()
        _write_json(self._op_path(operation["id"]), operation)
        self._prune_operations()

    def _prune_operations(self, keep: int = 20) -> None:
        try:
            entries = [p for p in self.operations_dir.glob("*.json") if p.is_file()]
            entries.sort(key=lambda p: self._file_mtime_ns(p))
        except OSError:
            return
        for old in entries[:-keep]:
            try:
                old.unlink()
            except OSError:
                continue

    def _new_operation(
        self, op_id: str, profile_id: str, force: bool, target_name: str = ""
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "id": op_id,
            "profile": profile_id,
            "targetName": target_name,
            "state": "running",  # running | success | failed
            "force": bool(force),
            "rolledBack": False,
            "startedAt": now_ms(),
            "finishedAt": 0,
            "error": "",
            "steps": [],
        }

    def begin_operation(
        self, profile_id: str, op_id: str = "", force: bool = False
    ) -> tuple[dict[str, Any], bool]:
        """同步登记一条切换操作，返回 (记录, 是否新建)。

        用 O_EXCL 独占创建，两个请求同时拿同一个 ID 时只有一个能建成；
        另一个读到已存在的记录并返回 created=False，不会重复执行。
        """
        op_id = _sanitize_op_id(op_id) or f"op-{uuid.uuid4().hex[:20]}"
        existing = self.get_operation(op_id)
        if existing is not None:
            return existing, False
        try:
            name = str(self.get(profile_id).get("name", ""))
        except KeyError:
            name = ""
        operation = self._new_operation(op_id, profile_id, force, name)
        if self._create_operation_file(operation):
            return operation, True
        existing = self.get_operation(op_id)
        if existing is not None:
            return existing, False
        raise SwitchError("无法登记切换操作，请稍后重试")

    def _create_operation_file(self, operation: dict[str, Any]) -> bool:
        path = self._op_path(operation["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "x", encoding="utf-8") as handle:
                json.dump(operation, handle, ensure_ascii=False, indent=2)
        except FileExistsError:
            return False
        except OSError as exc:
            raise SwitchError(f"无法登记切换操作：{exc}") from exc
        self._prune_operations()
        return True

    def is_switch_busy(self) -> bool:
        """是否已有切换在进行（同进程线程锁或跨进程文件锁）。"""
        if self._switch_lock.locked():
            return True
        with cross_process_lock(self.switch_lock_path, blocking=False) as locked:
            return not locked

    def _fail_operation(self, operation: dict[str, Any], message: str) -> None:
        operation["state"] = "failed"
        operation["error"] = message or "切换失败"
        operation["finishedAt"] = now_ms()
        self._save_operation(operation)

    def last_operation(self) -> dict[str, Any] | None:
        """最近一次切换记录（按 finishedAt/updatedAt 取最新），供状态展示。"""
        try:
            items = [p for p in self.operations_dir.glob("*.json") if p.is_file()]
        except OSError:
            return None
        latest: dict[str, Any] | None = None
        for path in items:
            data = _read_json(path, None)
            if isinstance(data, dict) and (
                latest is None or (data.get("updatedAt") or 0) > (latest.get("updatedAt") or 0)
            ):
                latest = data
        return latest

    # ---------------------------------------------------------------- 切换

    def switch(
        self,
        profile_id: str,
        dry_run: bool = False,
        progress: Progress | None = None,
        op_id: str = "",
        force: bool = False,
        operation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if dry_run:
            return self._switch_impl(profile_id, dry_run=True, progress=progress)

        if operation is None:
            # 同一个操作 ID 只执行一次：重复提交直接返回已有记录。
            operation, created = self.begin_operation(profile_id, op_id, force)
            if not created:
                return {
                    "ok": operation.get("state") == "success",
                    "duplicate": True,
                    "operation": operation,
                    "steps": operation.get("steps", []),
                    "profile": operation.get("profile"),
                }

        if not self._switch_lock.acquire(blocking=False):
            # 操作记录已经登记过了，必须落成失败结果，否则前端只能查到 404。
            message = "已有切换正在进行，请稍候"
            self._fail_operation(operation, message)
            raise SwitchError(message)
        try:
            def record(step: str, status: str, detail: dict[str, Any] | None) -> None:
                operation["steps"].append(
                    {"step": step, "status": status, "detail": detail or {}, "at": now_ms()}
                )
                if step == "recover" and status == "done":
                    operation["rolledBack"] = True
                self._save_operation(operation)
                if progress:
                    progress(step, status, detail)

            try:
                # 进程内锁挡住同进程线程，文件锁挡住另一个命令行/后台进程。
                with cross_process_lock(self.switch_lock_path, blocking=False) as locked:
                    if not locked:
                        raise SwitchError("另一个切换进程正在进行，请稍候再试")
                    result = self._switch_impl(
                        profile_id, dry_run=False, progress=record, force=force
                    )
                operation["state"] = "success"
                operation["finishedAt"] = now_ms()
                self._save_operation(operation)
                return {
                    "ok": True,
                    "operation": operation,
                    "steps": result.get("steps", []),
                    "profile": result.get("profile"),
                }
            except Exception as exc:
                self._fail_operation(operation, str(exc) or exc.__class__.__name__)
                raise
        finally:
            self._switch_lock.release()

    def _switch_impl(
        self,
        profile_id: str,
        dry_run: bool = False,
        progress: Progress | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        target = self.get(profile_id)
        running = self.running_app()
        runtime = _read_json(self.runtime_path, {}) or {}
        owner_id = running["profileId"] if running else (runtime.get("profileId") or DEFAULT_PROFILE_ID)
        try:
            self.get(owner_id)
        except KeyError:
            owner_id = DEFAULT_PROFILE_ID

        def emit(step: str, status: str, detail: dict[str, Any] | None = None) -> None:
            if progress:
                progress(step, status, detail or {})

        plan = {
            "target": target["id"],
            "targetName": target.get("name", ""),
            "running": bool(running),
            "runningPid": running["pid"] if running else None,
            "owner": owner_id,
            "needsSync": not (not running and owner_id == target["id"]),
        }
        if dry_run:
            return {"ok": True, "dryRun": True, "plan": plan, "steps": []}

        # 目标快照必须在关闭客户端之前验证，坏快照不能把当前会话关掉。
        self.validate_snapshot(target)
        # 目标是隔离档案时依赖 WORKBUDDY_CONFIG_DIR 目录隔离；客户端升级后
        # 若不再支持，切换会“看似成功、实际跑错档案”。这里在触碰任何状态
        # 之前做能力预检，失败即中止。
        if target.get("kind") != "default" and not self.check_env_capability(self.find_app_exe()):
            raise SwitchError(
                "未能确认当前客户端支持 WORKBUDDY_CONFIG_DIR 目录隔离，已取消切换（未改动任何数据）；"
                "请运行 wbs.py doctor 检查客户端版本"
            )
        steps: list[dict[str, Any]] = []
        closed_app = False
        auth_touched = False
        try:
            if running:
                emit("close", "start")
                result = self.close_app(force=force)
                closed_app = bool(result.get("closed"))
                steps.append({"step": "close", "ok": closed_app, **result})
                emit("close", "done" if closed_app else "error", result)
                if not closed_app:
                    if result.get("reason") == "close-timeout":
                        raise SwitchError(
                            "客户端未在宽限时间内退出，已取消切换（未强制结束进程，可能正在运行任务）；"
                            "请确认客户端内没有进行中的任务后，选择「强制切换」或手动退出重试"
                        )
                    raise SwitchError("无法关闭 WorkBuddy，请手动退出后重试")
                # Chromium 的单实例锁和调试端口需要一点时间完全释放；否则
                # 紧接着启动的新进程偶尔不会重新打开远程调试端口。
                time.sleep(1.5)
            else:
                emit("close", "skip", {"reason": "not-running"})
                steps.append({"step": "close", "ok": True, "skipped": True})

            if not running and owner_id == target["id"]:
                emit("snapshot", "skip", {"reason": "same-profile"})
                steps.append({"step": "snapshot", "ok": True, "skipped": True})
                emit("restore", "skip", {"reason": "same-profile"})
                steps.append({"step": "restore", "ok": True, "skipped": True})
            else:
                emit("snapshot", "start", {"owner": owner_id})
                manifest = self.save_auth(owner_id)
                steps.append(
                    {
                        "step": "snapshot",
                        "ok": True,
                        "owner": owner_id,
                        "files": len(manifest.get("files", [])),
                        "empty": manifest.get("empty"),
                        "preserved": bool(manifest.get("preserved")),
                    }
                )
                emit("snapshot", "done", {"files": len(manifest.get("files", []))})
                emit("restore", "start", {"target": target["id"]})
                auth_touched = True
                try:
                    restored = self.restore_auth(target)
                except Exception as exc:
                    self.logger.error("恢复登录态失败：%s", exc)
                    emit("restore", "error", {"message": str(exc)})
                    steps.append({"step": "restore", "ok": False, "message": str(exc)})
                    raise SwitchError(f"恢复登录态失败：{exc}") from exc
                steps.append({"step": "restore", "ok": True, **restored})
                emit("restore", "done", restored)

            emit("launch", "start")
            try:
                launched = self.launch_profile(target)
            except Exception as exc:
                emit("launch", "error", {"message": str(exc)})
                steps.append({"step": "launch", "ok": False, "message": str(exc)})
                raise
            if not launched.get("detected"):
                # Popen 成功不等于客户端真的起来了：没有可识别的主进程
                # 就不能把 launch 记成成功，走下方回滚。
                message = "客户端已启动但未检测到主进程，切换中止"
                emit("launch", "error", {"message": message})
                steps.append({"step": "launch", "ok": False, "message": message})
                raise SwitchError(message)
            steps.append({"step": "launch", "ok": True, **launched})
            emit("launch", "done", launched)
            return {"ok": True, "plan": plan, "steps": steps, "profile": target["id"]}
        except Exception as exc:
            if closed_app or auth_touched:
                self.logger.error("切换失败，正在恢复原档案 %s", owner_id, exc_info=True)
                try:
                    # 先把“下次手动启动”的环境变量恢复成原档案：即使原客户端
                    # 本来就是关闭的、不需要重新拉起，也不能把目标档案的
                    # WORKBUDDY_CONFIG_DIR 留给用户下一次手动启动。写失败必须
                    # 上报为回滚失败，不能报告“已恢复原档案”。
                    env_restored = self.set_active_profile_env(self.get(owner_id))
                    if self.running_app():
                        # 恢复路径必须尽快回到原档案，保留强制结束行为。
                        self.close_app(grace_seconds=6, force=True)
                    if auth_touched:
                        self.restore_auth(self.get(owner_id))
                    if closed_app:
                        self.launch_profile(self.get(owner_id))
                    if not env_restored:
                        raise SwitchError("无法恢复原档案的启动环境变量")
                    self.logger.info("已恢复原档案 %s", owner_id)
                    emit("recover", "done", {"owner": owner_id})
                except Exception as recovery_exc:
                    self.logger.exception("自动恢复原档案失败")
                    emit("recover", "error", {"message": str(recovery_exc)})
                    raise SwitchError(f"{exc}；自动恢复失败：{recovery_exc}") from exc
            raise

    def switch_detached(self, profile_id: str) -> int:
        """从客户端内部触发时，用脱离作业对象的方式启动后台切换进程。"""
        exe = sys.executable
        script = str(Path(__file__).resolve().parent.parent / "wbs.py")
        args = [exe, script, "switch", profile_id, "--quiet"]
        base_flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        for flags in (base_flags | CREATE_BREAKAWAY_FROM_JOB, base_flags):
            try:
                process = subprocess.Popen(
                    args,
                    creationflags=flags,
                    close_fds=True,
                    cwd=str(Path(script).parent),
                )
                return process.pid
            except OSError:
                continue
        raise SwitchError("无法启动后台切换进程，请在客户端外双击「启动切号器」")

    # ---------------------------------------------------------------- 状态

    def state(self, include_sizes: bool = True) -> dict[str, Any]:
        running = self.running_app()
        runtime = _read_json(self.runtime_path, {}) or {}
        items = []
        for profile in self.profiles:
            config = self.config_dir(profile)
            account = self.read_account(profile)
            manifest = self.read_manifest(profile["id"])
            avatar = self.avatar_info(profile, account)
            items.append(
                {
                    "id": profile["id"],
                    "name": profile.get("name", ""),
                    "kind": profile.get("kind", "isolated"),
                    "color": profile.get("color", "#2B6CF6"),
                    "note": profile.get("note", ""),
                    "createdAt": profile.get("createdAt", 0),
                    "lastUsedAt": profile.get("lastUsedAt", 0),
                    "configDir": str(config),
                    "exists": config.exists(),
                    "account": account,
                    "loggedIn": bool(account),
                    "hasAvatar": avatar["exists"],
                    "avatarVersion": avatar["version"],
                    "hasSnapshot": bool(manifest and manifest.get("files")),
                    "snapshotAt": (manifest or {}).get("savedAt", 0),
                    "sizeBytes": self.dir_size(profile) if include_sizes else 0,
                    "sizeText": _human_bytes(self.dir_size(profile)) if include_sizes else "",
                    "running": bool(running and running["profileId"] == profile["id"]),
                    "isDefault": profile.get("kind") == "default",
                }
            )
        exe = self.find_app_exe()
        return {
            "tool": {"version": TOOL_VERSION, "dataRoot": str(self.root)},
            "app": {
                "exe": exe,
                "version": self.app_version(),
                "running": bool(running),
                "pid": running["pid"] if running else None,
                "profileId": running["profileId"] if running else None,
                "manual": running["manual"] if running else False,
            },
            "running": running,
            "profiles": items,
            "settings": {
                "appExe": self.settings.get("appExe", ""),
                "dpapi": bool(self.settings.get("dpapi", True)),
                "shareDirs": self.settings.get("shareDirs", []),
                "graceSeconds": self.settings.get("graceSeconds", 12),
                "dataRoot": str(self.root),
            },
            "paths": {
                "liveAuthDir": str(SHARED_AUTH_DIR),
                "defaultConfig": str(DEFAULT_CONFIG_DIR),
            },
            "firstRun": not bool(runtime.get("profileId")),
            "lastOperation": self.last_operation(),
        }
    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "appExe" in payload and payload["appExe"] is not None:
            value = str(payload["appExe"]).strip()
            if value and not Path(value).is_file():
                raise SwitchError("指定的客户端路径不存在")
            self.settings["appExe"] = value
        if "dpapi" in payload:
            self.settings["dpapi"] = bool(payload["dpapi"])
        if "graceSeconds" in payload:
            try:
                self.settings["graceSeconds"] = max(3, min(60, int(payload["graceSeconds"])))
            except (TypeError, ValueError):
                pass
        if "shareDirs" in payload and isinstance(payload["shareDirs"], list):
            self.settings["shareDirs"] = [str(x) for x in payload["shareDirs"] if str(x).strip()]
        self._save_settings()
        return self.settings

    # ---------------------------------------------------------- 安装/卸载

    def _pythonw(self) -> str:
        exe = sys.executable
        if exe.lower().endswith("python.exe"):
            candidate = Path(exe).with_name("pythonw.exe")
            if candidate.is_file():
                return str(candidate)
        return exe

    def set_autostart(self, enabled: bool) -> bool:
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    script = str(Path(__file__).resolve().parent.parent / "wbs.py")
                    launcher = Path(script).with_name("启动后台.vbs")
                    if launcher.is_file():
                        command = f'wscript.exe "{launcher}"'
                    else:
                        command = f'"{self._pythonw()}" "{script}" watch --quiet'
                    winreg.SetValueEx(key, "WorkBuddySwitch", 0, winreg.REG_SZ, command)
                else:
                    try:
                        winreg.DeleteValue(key, "WorkBuddySwitch")
                    except FileNotFoundError:
                        pass
            return True
        except Exception as exc:
            self.logger.warning("设置开机自启失败: %s", exc)
            return False

    def _stop_watcher(self) -> None:
        """结束正在跑的后台进程（安装/升级时用于加载新代码）。"""
        info = self.agent_info()
        if not info:
            return
        pid = int(info["pid"])
        self.logger.info("结束旧后台进程 pid=%s", pid)
        win32.terminate(pid)
        deadline = time.time() + 6
        while time.time() < deadline and win32.is_alive(pid):
            time.sleep(0.1)
        try:
            self.agent_path().unlink(missing_ok=True)
        except OSError:
            pass

    def install(self) -> dict[str, Any]:
        """把调试端口写进用户环境变量，并让后台进程随登录自启。

        客户端主进程会读 WORKBUDDY_REMOTE_DEBUGGING_PORT 并自己开 CDP 端口，
        所以不需要改快捷方式、不需要管理员权限、也不需要额外启动器。

        若已有后台进程在跑，先结束再启动：否则升级后的代码不会生效，
        面板仍由旧进程注入旧脚本。
        """
        env_ok = win32.set_user_env("WORKBUDDY_REMOTE_DEBUGGING_PORT", str(self.debug_port()))
        autostart = self.set_autostart(True)
        self.settings["injectPanel"] = True
        self._save_settings()
        self._stop_watcher()
        watcher = self.ensure_watcher()
        return {"env": env_ok, "autostart": autostart, "watcher": watcher, "port": self.debug_port()}

    def uninstall(self) -> dict[str, Any]:
        env_ok = win32.set_user_env("WORKBUDDY_REMOTE_DEBUGGING_PORT", None)
        win32.set_user_env("WORKBUDDY_CONFIG_DIR", None)
        win32.set_user_env("WORKBUDDY_USER_DATA_DIR", None)
        autostart = self.set_autostart(False)
        self._stop_watcher()
        return {"env": env_ok, "autostart": autostart}

    # ---------------------------------------------------------------- 诊断

    def doctor(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        def add(name: str, status: str, detail: str = "") -> None:
            checks.append({"name": name, "status": status, "detail": detail})

        exe = self.find_app_exe()
        add("客户端可执行文件", "ok" if exe else "fail", exe or "未找到 WorkBuddy.exe")
        add(
            "客户端版本",
            "ok" if self.app_version() else "warn",
            self.app_version() or "未能读取版本（不影响切换）",
        )
        add(
            "现有配置文件",
            "ok" if DEFAULT_CONFIG_DIR.is_dir() else "warn",
            str(DEFAULT_CONFIG_DIR),
        )
        auth_files = self.live_auth_files()
        add(
            "共享登录态文件",
            "ok" if auth_files else "warn",
            f"{len(auth_files)} 个：{', '.join(p.name for p in auth_files)}" if auth_files else f"{SHARED_AUTH_DIR} 下未找到",
        )
        capability = self.check_env_capability(exe)
        add(
            "隔离目录支持（WORKBUDDY_CONFIG_DIR）",
            "ok" if capability else "fail",
            "客户端主进程支持按环境变量切换配置目录"
            if capability
            else "未能在 app.asar 中确认该能力，请升级/更换客户端后重试",
        )
        try:
            probe = self.root / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            add("数据目录可写", "ok", str(self.root))
        except Exception as exc:
            add("数据目录可写", "fail", f"{self.root}: {exc}")
        if win32.dpapi_available():
            try:
                token = b"probe-" + os.urandom(8)
                ok = win32.dpapi_unprotect(win32.dpapi_protect(token)) == token
                add("DPAPI 加密", "ok" if ok else "fail", "快照可用 Windows 用户凭据加密")
            except Exception as exc:
                add("DPAPI 加密", "fail", str(exc))
        else:
            add("DPAPI 加密", "warn", "当前平台不支持，快照将以原样保存")
        running = self.running_app()
        add(
            "运行状态",
            "ok",
            f"正在运行 · {running['profileName']}（pid {running['pid']}）" if running else "未运行",
        )
        info = self.agent_info()
        add(
            "后台进程",
            "ok" if info else "warn",
            f"运行中（pid {info['pid']}，接口 127.0.0.1:{info['port']}）"
            if info
            else "未运行，请双击「安装.bat」启动后台",
        )
        env_port = win32.get_user_env("WORKBUDDY_REMOTE_DEBUGGING_PORT")
        add(
            "调试端口环境变量",
            "ok" if env_port else "warn",
            f"WORKBUDDY_REMOTE_DEBUGGING_PORT={env_port}" if env_port else "未设置，执行 install 后写一次即可",
        )
        panel = Path(__file__).resolve().parent.parent / "inject" / "panel.js"
        add("面板脚本", "ok" if panel.is_file() else "fail", str(panel))
        try:
            import socket
            import urllib.request

            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.settimeout(0.5)
            busy = probe.connect_ex(("127.0.0.1", self.debug_port())) == 0
            probe.close()
            if busy and running:
                try:
                    url = f"http://127.0.0.1:{self.debug_port()}/json/version"
                    with urllib.request.urlopen(url, timeout=1) as response:
                        json.loads(response.read().decode("utf-8"))
                    add(
                        f"调试端口 {self.debug_port()}",
                        "ok",
                        "客户端调试端口已就绪，面板注入可用",
                    )
                except Exception:
                    add(
                        f"调试端口 {self.debug_port()}",
                        "warn",
                        "端口被其他程序占用，请关闭占用程序或修改 debugPort",
                    )
            elif busy:
                add(
                    f"调试端口 {self.debug_port()}",
                    "warn",
                    "端口被其他程序占用，请关闭占用程序或修改 debugPort",
                )
            elif running:
                add(
                    f"调试端口 {self.debug_port()}",
                    "warn",
                    "客户端正在运行但没有开放调试端口，完全退出客户端再打开一次",
                )
            else:
                add(
                    f"调试端口 {self.debug_port()}",
                    "ok",
                    "当前未占用（客户端启动时会自动打开）",
                )
        except Exception:
            add(f"调试端口 {self.debug_port()}", "warn", "无法检测")
        try:
            tail = (self.logs_dir / "switcher.log").read_text(encoding="utf-8", errors="replace")[-6000:]
        except OSError:
            tail = ""
        if "已注入账号面板入口" in tail:
            add("面板入口注入", "ok", "最近一次启动注入成功")
        elif "注入失败" in tail:
            add("面板入口注入", "warn", "最近一次注入失败，详见 logs/switcher.log")
        else:
            add("面板入口注入", "warn", "尚无注入记录（客户端启动后会自动注入）")
        for profile in self.profiles:
            config = self.config_dir(profile)
            add(
                f"档案 {profile.get('name')}",
                "ok" if config.exists() else "warn",
                str(config),
            )
        failures = [c for c in checks if c["status"] == "fail"]
        return {"ok": not failures, "checks": checks}

    def check_env_capability(self, exe: str = "") -> bool:
        """确认客户端 app.asar 仍支持目录隔离环境变量。

        结果按 asar 的路径 + 修改时间 + 大小做指纹缓存：客户端升级
        （指纹变化）后会重新扫描，正常切换仍使用缓存，不增加耗时。
        """
        exe = exe or self.find_app_exe()
        if not exe:
            self._asar_capability = False
            self._asar_key = None
            return False
        asar = Path(exe).parent / "resources" / "app.asar"
        try:
            stat = asar.stat()
            key = (str(asar), stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._asar_capability = False
            self._asar_key = None
            return False
        if self._asar_capability is not None and self._asar_key == key:
            return self._asar_capability
        if not asar.is_file():
            self._asar_capability = False
            self._asar_key = key
            return False
        needles = (b"WORKBUDDY_USER_DATA_DIR", b"WORKBUDDY_CONFIG_DIR")
        found = {needle: False for needle in needles}
        try:
            with asar.open("rb") as handle:
                carry = b""
                while True:
                    chunk = handle.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    window = carry + chunk
                    for needle in needles:
                        if needle in window:
                            found[needle] = True
                    carry = window[-64:]
                    if all(found.values()):
                        break
        except Exception:
            pass
        self._asar_capability = all(found.values())
        self._asar_key = key
        return self._asar_capability

    def harden(self) -> dict[str, Any]:
        sid = win32.current_user_sid()
        if not sid:
            return {"ok": False, "message": "无法获取当前用户 SID"}
        try:
            result = subprocess.run(
                [
                    "icacls",
                    str(self.root),
                    "/inheritance:r",
                    "/grant:r",
                    f"*{sid}:(OI)(CI)F",
                ],
                capture_output=True,
                text=True,
                creationflags=CREATE_NO_WINDOW,
                timeout=60,
            )
            ok = result.returncode == 0
            return {
                "ok": ok,
                "message": (result.stdout or result.stderr or "").strip(),
                "sid": sid,
                "root": str(self.root),
            }
        except Exception as exc:
            return {"ok": False, "message": str(exc)}


def render_table(rows: Iterable[dict[str, Any]]) -> str:
    rows = [r for r in rows if r]
    if not rows:
        return "（空）"
    headers = ["id", "名称", "账号", "版本", "最近使用", "占用", "状态"]
    widths = [len(h) for h in headers]
    table = []
    for row in rows:
        cells = [
            str(row.get("id", "")),
            str(row.get("name", "")),
            str(row.get("account", "")),
            str(row.get("edition", "")),
            str(row.get("lastUsed", "")),
            str(row.get("size", "")),
            str(row.get("state", "")),
        ]
        table.append(cells)
        widths = [max(w, min(28, len(c))) for w, c in zip(widths, cells)]
    lines = []
    lines.append("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    lines.append("  ".join("-" * w for w in widths))
    for cells in table:
        lines.append("  ".join(c.ljust(w) for c, w in zip(cells, widths)))
    return "\n".join(lines)


def format_ts(ms: int) -> str:
    if not ms:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))
