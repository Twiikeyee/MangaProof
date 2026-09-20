# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""本项目自身许可（GPL-3.0-only）文本的定位与读取。

为什么需要
----------
GPLv3 §6 要求分发目标码时**随附一份本许可副本**，因此三个 PyInstaller spec
都会把仓库根的 `LICENSE` 与 `THIRD_PARTY_LICENSES.md` 打进产物
（`licenses/` 目录，`sys._MEIPASS` 下）。本模块负责在

- 打包产物（`licenses/`，另兼容早期放在包根的情况），
- 直接运行源码（程序目录 = 仓库根），
- 源码树（从安装位置反推）

三种布局下都能找到许可文件，供「帮助 → 许可证…」与「关于」使用。

找不到文件时不抛异常：返回空文本，由调用方给出"请见程序目录下 LICENSE"的提示——
许可展示缺失不该让程序崩掉。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("mangaproof.app_license")

#: 与三个 spec 的 `_datas` 目标目录保持一致
BUNDLE_SUBDIR = "licenses"

LICENSE_FILE = "LICENSE"
THIRD_PARTY_FILE = "THIRD_PARTY_LICENSES.md"


def _candidate_paths(name: str) -> list[Path]:
    """按「打包产物 → 程序目录 → 源码树」的顺序给出候选路径。"""
    candidates: list[Path] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base = Path(meipass)
        candidates.append(base / BUNDLE_SUBDIR / name)
        candidates.append(base / name)          # 兼容放在产物包根的情况

    try:
        from mangaproof.config import paths

        app_dir = paths.get_app_dir()
    except Exception:                            # pragma: no cover - 环境异常兜底
        app_dir = None
    if app_dir:
        candidates.append(Path(app_dir) / name)

    # 源码树根：mangaproof/app_license.py → 上一级的上一级
    candidates.append(Path(__file__).resolve().parent.parent / name)
    return candidates


def find_license_file(name: str = LICENSE_FILE) -> Optional[Path]:
    """返回第一个存在的许可文件路径；都没有则返回 None。"""
    for path in _candidate_paths(name):
        if path.is_file():
            return path
    log.warning("未找到许可文件 %s（候选：%s）", name, [str(p) for p in _candidate_paths(name)])
    return None


def load_license_text(name: str = LICENSE_FILE) -> str:
    """读取许可文本；文件缺失或读取失败时返回空串（调用方自行兜底）。"""
    path = find_license_file(name)
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        log.exception("读取许可文件失败：%s", path)
        return ""


def license_location_hint(name: str = LICENSE_FILE) -> str:
    """给用户看的"许可文件在哪"提示（找不到时说明预期位置）。"""
    path = find_license_file(name)
    if path is not None:
        return str(path)
    return f"程序目录下的 {name}"
