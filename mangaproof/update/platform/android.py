# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""Android 专有更新逻辑（需求 §65、§66）。

Android **不使用独立安装器**，也不做 ``.old`` 替换（需求 §65）：

.. code-block:: text

    检查更新 → 立即更新 → 获取下载信息 → 下载 APK → SHA-256
        → 写入系统 Download → 调用系统安装器

因此本模块只提供"安装目录"等最小信息；任何"替换/提权/启动安装器"的调用
都会抛 :class:`UnsupportedPlatformError`，避免上层误以为 Android 也能走桌面那套流程。

注意：第一版**隐藏 Android 的更新入口**（需求方决策），
所以本模块当前只被"取程序目录/用户数据目录"这类通用调用触达。
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("mangaproof.update.platform.android")


def install_dir() -> Path:
    """Android 的程序目录（应用私有目录，见 config/paths.py 的 get_app_dir）。"""
    from mangaproof.config import paths

    return paths.get_app_dir()


def parent_dir() -> Path:
    return install_dir().parent


def needs_elevation() -> bool:
    """Android 没有"提权"概念：应用私有目录天然可写。"""
    return False


def launch_installer(exe: Path, args: list[str], *, elevate: bool) -> int:
    raise NotImplementedError(
        "Android 不使用独立安装器（需求 §65）：应用 APK 由系统安装器安装"
    )


def download_dir() -> Path:
    """系统 Download 目录（需求 §65：APK 落到这里再交给系统安装器）。

    Android 11+ 上 ``/storage/emulated/0/Download`` 需要
    ``MANAGE_EXTERNAL_STORAGE``（清单已声明，见 scripts/android/build_android.py）。
    """
    return Path("/storage/emulated/0/Download")
