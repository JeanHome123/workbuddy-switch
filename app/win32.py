"""Windows 底层能力：进程/窗口控制、DPAPI 加解密。

只依赖标准库 ctypes，不调用 wmic / PowerShell，避免额外的安全策略与
性能开销。所有函数在非 Windows 平台调用都会抛 RuntimeError。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes as wt
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    # 显式声明参数类型：否则 64 位下 HWND 会被按 c_int 截断，
    # IsWindowVisible / GetWindowRect 全部返回失败（看起来像“没有窗口”）。
    _user32.IsWindowVisible.argtypes = [wt.HWND]
    _user32.IsWindowVisible.restype = wt.BOOL
    _user32.IsIconic.argtypes = [wt.HWND]
    _user32.IsIconic.restype = wt.BOOL
    _user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    _user32.GetWindowRect.restype = wt.BOOL
    _user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wt.DWORD
else:  # pragma: no cover - 仅 Windows 目标
    _kernel32 = _user32 = _advapi32 = _crypt32 = None

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000

WM_CLOSE = 0x0010
WAIT_TIMEOUT = 0x00000102

CRYPTPROTECT_UI_FORBIDDEN = 0x01
TOKEN_QUERY = 0x0008
TokenUser = 1


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise RuntimeError("该功能仅支持 Windows")


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    exe: str


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def list_processes() -> list[Proc]:
    """枚举当前进程列表。"""
    _require_windows()
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    result: list[Proc] = []
    try:
        ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            result.append(
                Proc(int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile or "")
            )
            ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    return result


def process_image_path(pid: int) -> str:
    """取进程可执行文件完整路径，失败返回空串。"""
    _require_windows()
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wt.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def is_alive(pid: int) -> bool:
    _require_windows()
    if not pid:
        return False
    handle = _kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        return _kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        _kernel32.CloseHandle(handle)


def terminate(pid: int) -> bool:
    _require_windows()
    handle = _kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(_kernel32.TerminateProcess(handle, 1))
    finally:
        _kernel32.CloseHandle(handle)


def close_windows(pid: int) -> int:
    """向该进程的所有顶层窗口发送 WM_CLOSE，返回发送成功的窗口数。"""
    _require_windows()
    sent = 0
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _callback(hwnd, _lparam):
        nonlocal sent
        owner = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            sent += 1
        return True

    _user32.EnumWindows(WNDENUMPROC(_callback), 0)
    return sent


def post_message(hwnd: int, message: int, wparam: int = 0, lparam: int = 0) -> bool:
    _require_windows()
    return bool(_user32.PostMessageW(hwnd, message, wparam, lparam))


def count_visible_windows(pid: int) -> int:
    """统计该进程当前“像主界面”的可见顶层窗口。

    WorkBuddy 点关闭会缩到托盘（窗口隐藏、进程还在），此时为 0；
    如果仍有可见且未最小化的窗口，说明客户端还停在界面上，可能有任务在跑。
    1×1 之类的小辅助窗口、提示窗口不计入，避免误判。
    """
    _require_windows()
    count = 0
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _callback(hwnd, _lparam):
        nonlocal count
        owner = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        if not _user32.IsWindowVisible(hwnd) or _user32.IsIconic(hwnd):
            return True
        rect = wt.RECT()
        if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            if rect.right - rect.left >= 100 and rect.bottom - rect.top >= 100:
                count += 1
        return True

    _user32.EnumWindows(WNDENUMPROC(_callback), 0)
    return count


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes):
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _from_blob(blob: _DATA_BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        _kernel32.LocalFree(blob.pbData)


def dpapi_available() -> bool:
    return IS_WINDOWS


def dpapi_protect(data: bytes, entropy: bytes = b"wb-account-switch") -> bytes:
    """用当前 Windows 用户凭据（DPAPI）加密数据。"""
    _require_windows()
    if not data:
        return b""
    blob_in, _keep = _to_blob(data)
    blob_entropy, _keep2 = _to_blob(entropy)
    blob_out = _DATA_BLOB()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        "wb-account-switch",
        ctypes.byref(blob_entropy),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData 失败")
    return _from_blob(blob_out)


def dpapi_unprotect(data: bytes, entropy: bytes = b"wb-account-switch") -> bytes:
    """解密 dpapi_protect 的结果。"""
    _require_windows()
    if not data:
        return b""
    blob_in, _keep = _to_blob(data)
    blob_entropy, _keep2 = _to_blob(entropy)
    blob_out = _DATA_BLOB()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(blob_entropy),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData 失败")
    return _from_blob(blob_out)


def current_user_sid() -> str:
    """返回当前用户的字符串 SID（如 S-1-5-21-...），失败返回空串。"""
    _require_windows()
    token = wt.HANDLE()
    if not _advapi32.OpenProcessToken(
        _kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        return ""
    try:
        size = wt.DWORD(0)
        _advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
        if not size.value:
            return ""
        buffer = ctypes.create_string_buffer(size.value)
        if not _advapi32.GetTokenInformation(
            token, TokenUser, buffer, size, ctypes.byref(size)
        ):
            return ""
        sid_ptr = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
        sid_str = ctypes.c_wchar_p()
        if not _advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_str)):
            return ""
        try:
            return sid_str.value or ""
        finally:
            _kernel32.LocalFree(sid_str)
    finally:
        _kernel32.CloseHandle(token)


def default_browser_app_exe(candidates: list[str]) -> str:
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return ""


HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002


def broadcast_environment_change() -> None:
    """通知资源管理器环境变量变了，让之后新启动的进程能立刻读到。"""
    if not IS_WINDOWS:
        return
    try:
        result = wt.DWORD(0)
        _user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            "Environment",
            SMTO_ABORTIFHUNG,
            3000,
            ctypes.byref(result),
        )
    except Exception:
        pass


def set_user_env(name: str, value: str | None) -> bool:
    """写当前用户的环境变量（HKCU\\Environment），value 为 None 表示删除。"""
    if not IS_WINDOWS:
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
            if value is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        broadcast_environment_change()
        return True
    except Exception:
        return False


def get_user_env(name: str) -> str:
    if not IS_WINDOWS:
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except Exception:
        return ""
