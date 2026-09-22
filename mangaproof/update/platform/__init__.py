# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""平台专有逻辑的运行时分发（需求 §6、§38、§39、§41、§42）。

**为什么必须运行时导入**（需求 §6）：Windows 不该加载 Linux 的提权依赖，
反之亦然；Android 更不该加载桌面提权模块。因此这里只做"判断系统 → 导入对应模块"，
各平台模块内部再去导入自己需要的第三方库（本包目前只用标准库）。

统一接口（各平台模块都必须实现）::

    install_dir() -> Path           # 需求 §38：被替换的那个目录
    parent_dir() -> Path            # 需求 §38：安装目录的父目录
    needs_elevation() -> bool       # 需求 §39：父目录是否可写（实测，不用 os.access）
    launch_installer(exe, args, *, elevate) -> int   # 需求 §41/§42/§46

Android 不在本包实现安装逻辑（需求 §65：Android 不使用独立安装器），
其模块只提供"安装目录"等最小信息，其余能力显式抛错。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import ModuleType

from mangaproof.utils.platform import is_android_strict

log = logging.getLogger("mangaproof.update.platform")


class UnsupportedPlatformError(RuntimeError):
    """当前平台不支持该更新操作（例如 Android 没有独立安装器）。"""


def _module_name() -> str:
    if is_android_strict():
        return "android"
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


#: ``current()`` 可能用到的全部平台模块名（= 本包的子模块）。
#: **打包必须靠它**：``current()`` 里的导入是拼接出来的运行时导入，
#: PyInstaller 的静态分析看不到，漏声明就会打出"检查更新能用、点安装就
#: No module named 'mangaproof.update.platform.windows'"的残废包
#: （三份 ``packaging/main_*.spec`` 都把它并进 hiddenimports）。
PLATFORM_MODULES = ("android", "windows", "macos", "linux")


def current() -> ModuleType:
    """导入并返回当前平台的模块（唯一入口）。"""
    name = _module_name()
    module = __import__(f"mangaproof.update.platform.{name}", fromlist=["_"])
    return module


def current_name() -> str:
    """当前平台模块名（日志用，避免为写日志而导入模块）。"""
    return _module_name()


def probe_writable(directory: Path, *, probe_name: str) -> bool:
    """实测"能否在 ``directory`` 里创建并删除条目"（需求 §39）。

    需求明确**禁止只用** ``os.access()`` 判断：在 ACL、只读挂载、
    macOS 的 TCC、Windows 的虚拟化重定向下，``os.access`` 会给出错误答案。
    因此这里真的建一个探针文件再删掉。
    """
    probe = Path(directory) / probe_name
    try:
        probe.mkdir(parents=True, exist_ok=True)
        probe.rmdir()
        return True
    except OSError:
        # 失败也要尽力清理残留（需求 §39：失败 → 清理残留 → 需要提权）
        try:
            if probe.is_dir():
                probe.rmdir()
            elif probe.exists():
                probe.unlink()
        except OSError:
            pass
        return False


__all__ = [
    "current",
    "current_name",
    "probe_writable",
    "PLATFORM_MODULES",
    "UnsupportedPlatformError",
]
