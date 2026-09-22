# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""Linux 专有更新逻辑（需求 §38、§39、§41、§42）。

提权降级链（与成熟更新器一致的做法，见调研报告 §5.2）::

    不需要提权 → 直接运行
    需要提权   → pkexec（只跑 CLI 安装逻辑）
                  ↓ 返回 127（机制不可用）
                 sudo -A + SUDO_ASKPASS（zenity / kdialog / ssh-askpass）
                  ↓ 全部失败
                 报错并提示用户"复制命令到终端执行"

返回码语义（pkexec 官方手册）：``126`` = 用户取消认证（**停止降级**，
否则会连问两次密码）；``127`` = 未授权/无法取得授权（才降级）。

`pkexec` 会清洗 ``DISPLAY`` / ``XAUTHORITY``，因此**不能**用它启动 GUI；
本模块只把它用于安装器（安装器以 ``--cli`` 方式做文件操作，
图形进度由主程序的更新页面承担）。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("mangaproof.update.platform.linux")

PKEXEC_CANCELLED = 126
PKEXEC_UNAVAILABLE = 127

#: 图形化取密码程序（按可用性顺序探测）
_ASKPASS_CANDIDATES = ("ssh-askpass", "lxqt-openssh-askpass", "ksshaskpass")


def install_dir() -> Path:
    """需求 §38：Linux 的安装目录 = 主程序可执行文件所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # 直接跑源码时，可执行文件是 python —— 用程序目录（settings.json 同处）
    from mangaproof.config import paths

    return paths.get_app_dir()


def parent_dir() -> Path:
    return install_dir().parent


def probe_name() -> str:
    return "MangaProof-upgrade-test"


def needs_elevation() -> bool:
    """需求 §39：实测父目录可写性（不依赖 ``os.access``）。"""
    from mangaproof.update.platform import probe_writable

    return not probe_writable(parent_dir(), probe_name=probe_name())


def has_graphical_session() -> bool:
    """是否有图形会话（决定能不能弹授权对话框）。"""
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def launch_installer(exe: Path, args: list[str], *, elevate: bool) -> int:
    """启动安装器（需求 §42/§46）。返回 PID；提权路径拿不到 PID 时返回 0。"""
    exe = _ensure_executable(Path(exe))
    if not elevate:
        proc = subprocess.Popen([str(exe), *args], close_fds=True)
        log.info("已启动安装器（普通权限），pid=%s", proc.pid)
        return proc.pid
    return _launch_elevated(exe, args)


def _ensure_executable(exe: Path) -> Path:
    """需求 §41：启动前确认存在、可执行，必要时 ``chmod +x``。"""
    if not exe.is_file():
        raise FileNotFoundError(f"找不到安装器：{exe}")
    if not os.access(exe, os.X_OK):
        mode = exe.stat().st_mode | 0o111
        exe.chmod(mode)
        log.info("已为安装器补上可执行位：%s", exe)
    return exe


def _launch_elevated(exe: Path, args: list[str]) -> int:
    command = [str(exe), *args]

    if shutil.which("pkexec") and has_graphical_session():
        try:
            # --disable-internal-agent：无 polkit agent 时不要退化成文本提示（看起来像卡死）
            proc = subprocess.Popen(
                ["pkexec", "--disable-internal-agent", *command], close_fds=True
            )
            log.info("已通过 pkexec 启动安装器，pid=%s", proc.pid)
            return proc.pid
        except OSError as exc:
            log.warning("pkexec 启动失败，降级到 sudo -A：%s", exc)

    askpass = _find_askpass()
    if shutil.which("sudo") and askpass:
        env = dict(os.environ, SUDO_ASKPASS=askpass)
        proc = subprocess.Popen(
            ["sudo", "-A", *command], close_fds=True, env=env
        )
        log.info("已通过 sudo -A 启动安装器（askpass=%s），pid=%s", askpass, proc.pid)
        return proc.pid

    raise PermissionError(
        "需要管理员权限才能更新，但当前环境没有可用的图形化授权方式。\n"
        "请在终端中手动执行：\n"
        f"  sudo {' '.join(command)}"
    )


def _find_askpass() -> str | None:
    for name in _ASKPASS_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    for name in ("zenity", "kdialog"):
        found = shutil.which(name)
        if found:
            return found
    return None
