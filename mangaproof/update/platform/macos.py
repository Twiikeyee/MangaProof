# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""macOS 专有更新逻辑（需求 §38、§39、§41、§42、§55）。

两处与其它平台不同：

1. **安装目录是 .app 整包**：``/Applications/MangaProof.app/Contents/MacOS/MangaProof``
   向上三层才是 ``MangaProof.app``（需求 §38/§55），替换时整包改名，
   而不是只换 ``Contents/MacOS/MangaProof``。
2. **提权用 osascript**：``do shell script "…" with administrator privileges``；
   用户取消时 AppleScript 返回 ``-128``（``user.canceled``）。
   提权**只做文件操作**，启动新版要走非提权的 ``open -n -a``。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("mangaproof.update.platform.macos")

OSASCRIPT_CANCEL = -128


def executable_path() -> Path:
    """主程序可执行文件（.app 内是 ``Contents/MacOS/MangaProof``）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    from mangaproof.config import paths

    return paths.get_app_dir() / "MangaProof"


def install_dir() -> Path:
    """需求 §38/§55：``…/MangaProof.app/Contents/MacOS/MangaProof`` 向上三层。"""
    exe = executable_path()
    if exe.parent.name == "MacOS" and exe.parent.parent.name == "Contents":
        return exe.parent.parent.parent          # …/MangaProof.app
    return exe.parent


def parent_dir() -> Path:
    return install_dir().parent


def probe_name() -> str:
    return "MangaProof-upgrade-test"


def needs_elevation() -> bool:
    """需求 §39：实测 ``/Applications`` 这类父目录是否可写。"""
    from mangaproof.update.platform import probe_writable

    return not probe_writable(parent_dir(), probe_name=probe_name())


def launch_installer(exe: Path, args: list[str], *, elevate: bool) -> int:
    """启动安装器（需求 §41/§42/§46）。"""
    exe = _ensure_executable(Path(exe))
    if not elevate:
        proc = subprocess.Popen([str(exe), *args], close_fds=True)
        log.info("已启动安装器（普通权限），pid=%s", proc.pid)
        return proc.pid

    parts = " ".join([_quote(str(exe)), *(_quote(a) for a in args)])
    return _run_with_administrator(parts)


def _ensure_executable(exe: Path) -> Path:
    """需求 §41：确认存在、可执行，必要时 chmod +x。"""
    if not exe.is_file():
        raise FileNotFoundError(f"找不到安装器：{exe}")
    if not os.access(exe, os.X_OK):
        exe.chmod(exe.stat().st_mode | 0o111)
        log.info("已为安装器补上可执行位：%s", exe)
    return exe


def _quote(text: str) -> str:
    """AppleScript 字面量转义（``do shell script`` 里传 shell 命令，必须转义）。"""
    return "'" + str(text).replace("'", "'\\''") + "'"


def _run_with_administrator(shell_command: str) -> int:
    script = f'do shell script "{shell_command}" with administrator privileges'
    proc = subprocess.Popen(
        ["/usr/bin/osascript", "-e", script], close_fds=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    log.info("已通过 osascript 提权启动安装器，pid=%s", proc.pid)
    return proc.pid


def open_app(path: Path, *, new_instance: bool = True) -> int:
    """以**非提权**身份启动新版 .app（需求 §55/§57）。

    用 ``open -n -a`` 而不是直接执行 ``Contents/MacOS/MangaProof``：
    前者经 LaunchServices 注册（Dock/激活行为正确），``-n`` 强制新实例，
    避免"参数被已有实例吃掉"导致成功标记写不出来。
    """
    args = ["/usr/bin/open"]
    if new_instance:
        args.append("-n")
    args += ["-a", str(path)]
    proc = subprocess.Popen(args, close_fds=True)
    return proc.pid
