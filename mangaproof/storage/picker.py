# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""文件 / 文件夹选择器：桌面端与 Android 端在这里分流。

桌面端（零改动）
----------------
就是原来那两行 `QFileDialog` 静态调用：对话框形态、返回类型、可被测试 patch 的
方式全部与改造前一致（**不带**任何 extra option）。

Android 端（只多一个选项）
--------------------------
同样用 `QFileDialog`，但显式传 `DontUseNativeDialog`：

- Qt 在 Android 上默认走**平台原生对话框**（`QAndroidPlatformTheme::
  usePlatformNativeDialog(FileDialog) == true`），而 Qt 6.11.2 的
  `QAndroidPlatformFileDialogHelper` 存在**同线程重入死锁** —— 选中/取消后
  `QtAndroidPrivate::handleActivityResult()` 在**持有非递归 `QMutex`** 的情况下
  同步回调，信号一路走到 `QDialog::done()` → `hide()` → `helper->hide()` →
  `unregisterActivityResultListener()`，**再次获取同一把锁** → Android 主线程
  （= Qt GUI 线程）自锁死，界面永久卡死；
- `DontUseNativeDialog` 让 `QFileDialog` 完全不创建平台 helper
  （`QFileDialogPrivate::canBeNativeDialog()` 带该选项时直接返回 false，
  于是 `init()` 里 `nativeDialogInUse` 为 false），因此**不碰 Activity、不碰那把锁**，
  就是一个纯粹的 Qt 控件版对话框 —— 纯 Python 路径，没有 JNI、没有跨进程协议、
  没有 Java 侧线程，也就没有"不稳定"的中间环节。

代价（已知并接受）：控件版对话框浏览的是**真实路径**，因此访问
`/storage/emulated/0/...` 需要 `MANAGE_EXTERNAL_STORAGE` 已被用户授予
（清单里已声明，见 `scripts/android/build_android.py`）；Android 11+ 的
`Android/data`、`Android/obb` 与 `/data` 其余部分不可读，属系统限制。

历史方案（均已放弃，勿再引入）：自建 Java 原生选择器 + 共享文件协议（不稳定），
以及靠清单注入 `android:extractNativeLibs` 修"so 不解压"（该属性压不过 AGP 的
`useLegacyPackaging`，实测无效）。详见
`docs/Android端适配设计_原生文件读写_横屏全屏_分层图标_内存策略.md` §2.13。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QWidget

from mangaproof.utils.platform import is_android_strict

log = logging.getLogger("mangaproof.storage.picker")


def _extra_options(*, for_directory: bool) -> QFileDialog.Option:
    """平台差异化选项：**只有 Android** 需要强制控件版对话框（见模块文档）。

    桌面端返回空选项，保证与改造前的调用**逐参数一致**。
    """
    if not is_android_strict():
        return QFileDialog.Option(0)
    if for_directory:
        return QFileDialog.Option.DontUseNativeDialog | QFileDialog.Option.ShowDirsOnly
    return QFileDialog.Option.DontUseNativeDialog


def _android_start_dir() -> str:
    """Android 上给控件版对话框一个可用的初始目录。

    控件版对话框的默认目录是 `/`（Android 根文件系统，对用户没有意义且多半不可读），
    所以挑第一个存在的外部存储常见目录作为起点；都不存在就交给 Qt 的默认行为。
    """
    if not is_android_strict():
        return ""
    for candidate in (
        "/storage/emulated/0/Download",
        "/storage/emulated/0/Documents",
        "/storage/emulated/0",
    ):
        if Path(candidate).is_dir():
            return candidate
    return ""


def pick_psd_file(parent: QWidget | None = None) -> str:
    """选择一个 PSD/PSB 文件。

    :return: 本地文件路径；取消时返回空串（与 `QFileDialog` 语义一致）
    """
    path_str, _ = QFileDialog.getOpenFileName(
        parent,
        "打开单个 PSD",
        _android_start_dir(),
        "PSD/PSB 文件 (*.psd *.psb)",
        options=_extra_options(for_directory=False),
    )
    return path_str


def pick_folder(parent: QWidget | None = None) -> str:
    """选择一个漫画文件夹。

    :return: 本地目录路径；取消时返回空串（与 `QFileDialog` 语义一致）
    """
    folder = QFileDialog.getExistingDirectory(
        parent,
        "打开漫画文件夹",
        _android_start_dir(),
        options=_extra_options(for_directory=True),
    )
    return folder
