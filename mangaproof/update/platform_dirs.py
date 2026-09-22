# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""更新流程用到的临时目录与持久状态位置（需求 §36、§37、§49、§58）。

规则（需求原文）：

- **Windows**：用系统临时目录 ``TEMP`` / ``TMP``，最终目录为
  ``TEMP/MangaProof-update-package/`` 与 ``TEMP/MangaProof-update-installer/``；
- **Linux / macOS**：优先 ``XDG_CACHE_HOME``，否则 ``~/.cache/``，
  下面同样是这两个目录；
- **禁止硬编码具体路径** —— 所以本模块是唯一的路径来源，
  安装器与主程序都从这里取，绝不各自拼字符串。

目录语义：

======================  ==========================================================
``update-package``      下载下来的更新包 + 用户数据备份 + 成功标记（需求 §49/§58）
``update-installer``    从程序目录复制出来的安装器（需求 §40：必须从临时目录运行）
======================  ==========================================================

本模块**零依赖**（只用标准库）：安装器也要 import 它。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: 三个固定目录名（需求 §36/§37/§40/§49）
PACKAGE_DIRNAME = "MangaProof-update-package"
INSTALLER_DIRNAME = "MangaProof-update-installer"
DATA_BACKUP_DIRNAME = "MangaProof-update-data"

#: 成功标记文件名（需求 §58：放在更新临时目录；内容里带 token + version 供校验）
SUCCESS_MARKER_NAME = "success.marker"

#: 安装器状态文件名（需求 §64 的异常中断恢复依据；与 marker 同目录，便于一起清理）
STATE_FILE_NAME = "installer-state.json"

#: 安装器可执行文件名（需求 §43）
INSTALLER_NAME = "MangaProof-update-installer"

#: 传给安装器的 --platform 取值（与 mangaproof.update.detector 的 os_key 对齐）
if sys.platform == "win32":
    PLATFORM_KEY = "windows"
elif sys.platform == "darwin":
    PLATFORM_KEY = "macos"
else:
    PLATFORM_KEY = "linux"          # Android 也用 linux 解释器；更新入口第一版隐藏


def installer_filename() -> str:
    """安装器文件名（Windows 带 ``.exe``）。"""
    return f"{INSTALLER_NAME}.exe" if sys.platform == "win32" else INSTALLER_NAME


def _windows_temp() -> Path:
    for key in ("TEMP", "TMP"):
        value = os.environ.get(key)
        if value and Path(value).is_dir():
            return Path(value)
    # 环境变量不可信/缺失时的兜底：标准库自己算（仍然不是硬编码路径）
    import tempfile

    return Path(tempfile.gettempdir())


def _xdg_cache() -> Path:
    value = os.environ.get("XDG_CACHE_HOME")
    if value and Path(value).is_absolute():
        return Path(value)
    return Path.home() / ".cache"


def temp_root() -> Path:
    """更新临时目录的根：Windows 用 TEMP/TMP，其余用 XDG_CACHE_HOME/~/.cache。"""
    return _windows_temp() if sys.platform == "win32" else _xdg_cache()


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def update_package_dir(*, create: bool = True) -> Path:
    """更新包 / 数据备份 / 成功标记的存放目录。"""
    path = temp_root() / PACKAGE_DIRNAME
    return _ensure(path) if create else path


def installer_dir(*, create: bool = True) -> Path:
    """安装器副本的运行目录（需求 §40：安装器必须从临时目录运行）。"""
    path = temp_root() / INSTALLER_DIRNAME
    return _ensure(path) if create else path


def data_backup_dir(*, create: bool = True) -> Path:
    """用户数据备份目录（需求 §49：在更新包目录下）。"""
    path = update_package_dir(create=create) / DATA_BACKUP_DIRNAME
    return _ensure(path) if create else path


def success_marker_path() -> Path:
    """成功标记的约定路径（需求 §58）——新版主程序与安装器都按此路径读写。"""
    return update_package_dir() / SUCCESS_MARKER_NAME


def state_file_path() -> Path:
    """安装器状态文件路径（需求 §64）。"""
    return update_package_dir() / STATE_FILE_NAME


def cleanup_temp_dirs() -> list[str]:
    """删除本流程创建的三个临时目录（需求 §61 的清理步骤）。

    只删**我们自己的**固定目录名，绝不递归删除临时根目录。
    :returns: 被删除的目录路径（供日志/诊断）。
    """
    import shutil

    removed: list[str] = []
    for path in (
        temp_root() / INSTALLER_DIRNAME,
        temp_root() / PACKAGE_DIRNAME,
    ):
        if path.is_dir():
            try:
                shutil.rmtree(path)
                removed.append(str(path))
            except OSError:
                # 清理失败不该让"更新成功"变成失败：下次更新会复用/重建
                pass
    return removed
