# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""Windows 专有更新逻辑（需求 §38、§39、§41、§42）。

只在 Windows 上被 :func:`mangaproof.update.platform.current` 导入；
本模块内部用到 ctypes（标准库），不需要额外第三方依赖。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("mangaproof.update.platform.windows")

#: UAC 被用户取消的错误码
ERROR_CANCELLED = 1223

#: 提权安装器的环境变量清理清单前缀：PyInstaller 6.22.3+ 对**提权运行的 onefile**
#: 会校验父进程与继承的 _PYI_* 变量，命中时会拒绝启动（GHSA-9fxf-4qw3-ghmr）。
#: 因此提权启动前把这些变量清掉。
_PYI_PREFIX = "_PYI_"


def install_dir() -> Path:
    """需求 §38：Windows 的安装目录 = 主程序 exe 所在目录。"""
    return Path(sys.executable).resolve().parent


def parent_dir() -> Path:
    """需求 §38：安装目录的父目录（权限检测与解压目标都在这一层）。"""
    return install_dir().parent


def probe_name() -> str:
    return "MangaProof-upgrade-test"


def needs_elevation() -> bool:
    """需求 §39：安装目录父目录的实际写入能力（真实建/删探针目录）。"""
    from mangaproof.update.platform import probe_writable

    return not probe_writable(parent_dir(), probe_name=probe_name())


def launch_installer(exe: Path, args: list[str], *, elevate: bool) -> int:
    """启动安装器（需求 §41/§42/§46）。

    - 不需要提权：直接 ``CreateProcess``（``subprocess.Popen``）；
    - 需要提权：``ShellExecuteExW`` + ``runas`` —— 必须用 Ex 版本才能拿到进程句柄，
      也才能区分"用户点了否"（``ERROR_CANCELLED``）与其它失败。

    :returns: 新进程 PID（提权路径下拿不到 PID 时返回 0，但已确认启动成功）
    :raises OSError: 启动失败（调用方据此保持主程序运行并报错，需求 §46）
    """
    exe = Path(exe)
    if not exe.is_file():
        raise FileNotFoundError(f"找不到安装器：{exe}")

    if not elevate:
        proc = subprocess.Popen([str(exe), *args], close_fds=True)
        log.info("已启动安装器（普通权限），pid=%s", proc.pid)
        return proc.pid

    return _shell_execute_runas(exe, args)


def _shell_execute_runas(exe: Path, args: list[str]) -> int:
    """用 ShellExecuteExW(runas) 提权启动，返回 PID（拿不到时为 0）。"""
    import ctypes
    from ctypes import wintypes

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NOASYNC = 0x00000100
    SEE_MASK_FLAG_NO_UI = 0x00000400
    SW_SHOWNORMAL = 1

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI
    info.lpVerb = "runas"
    info.lpFile = str(exe)
    info.lpParameters = subprocess.list2cmdline(args)
    info.lpDirectory = str(exe.parent)
    info.nShow = SW_SHOWNORMAL

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == ERROR_CANCELLED:
            raise PermissionError("用户取消了授权（UAC）")
        raise OSError(error, f"提权启动安装器失败（GetLastError={error}）")

    pid = 0
    if info.hProcess:
        pid = int(kernel32.GetProcessId(info.hProcess))
        kernel32.CloseHandle(info.hProcess)
    log.info("已提权启动安装器，pid=%s", pid or "未知")
    return pid


def installer_environment() -> dict[str, str]:
    """给安装器准备的环境变量：清掉 ``_PYI_*``（见模块顶部说明）。"""
    import os

    return {k: v for k, v in os.environ.items() if not k.startswith(_PYI_PREFIX)}
