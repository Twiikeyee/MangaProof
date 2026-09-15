"""选择器门面：桌面端与 Android 端在这里分流。

设计原则（**桌面端零改动**）
--------------------------
桌面端走的就是原来那两行 `QFileDialog` 调用，行为、对话框、返回类型、可被测试
patch 的方式全部保持一致；Android 分支只有在 `is_android_strict()` 为真时才会
被 import（`android_picker` 模块因此不会被桌面加载）。

Android 上为什么不能用 `QFileDialog`
-----------------------------------
Qt 6.11.2 的 `QAndroidPlatformFileDialogHelper` 存在同线程重入死锁：选中/取消后
Java 在持非递归 `QMutex` 的情况下同步回调 → `QDialog::done()` → `hide()` →
`helper->hide()` → `unregisterActivityResultListener()` 再取同一把锁 → 主线程
自锁死，界面永久卡死。详见
`docs/Android端适配设计_原生文件读写_横屏全屏_分层图标_内存策略.md`。
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import QFileDialog, QWidget

from mangaproof.utils.platform import is_android_strict

log = logging.getLogger("mangaproof.storage.picker")

#: Android：用户取消 / 失败时的可读文案（由调用方决定是否展示）
MSG_CANCELLED = "已取消选择"


def is_native_picker() -> bool:
    """当前是否使用 Android 原生选择器（供日志/测试断言）。"""
    return is_android_strict()


def pick_psd_file(parent: QWidget | None = None) -> str:
    """选择一个 PSD/PSB 文件。

    :return: 本地文件路径；用户取消或失败时返回空串（与 `QFileDialog` 语义一致）
    """
    if not is_android_strict():
        path_str, _ = QFileDialog.getOpenFileName(
            parent, "打开单个 PSD", "", "PSD/PSB 文件 (*.psd *.psb)"
        )
        return path_str

    from mangaproof.storage import android_picker

    try:
        result = android_picker.pick("file")
    except Exception as exc:                    # 通道异常 → 不当成崩溃
        log.warning("Android 文件选择器异常：%s", exc, exc_info=True)
        _warn(parent, f"无法打开系统文件选择器：{exc}")
        return ""
    if result["status"] == android_picker.RESULT_OK:
        return str(result["path"])
    if result["status"] == android_picker.RESULT_CANCEL:
        return ""
    _warn(parent, str(result["message"]) or "选择文件失败")
    return ""


def pick_folder(parent: QWidget | None = None) -> str:
    """选择一个漫画文件夹。

    :return: 本地目录路径；用户取消或失败时返回空串（与 `QFileDialog` 语义一致）
    """
    if not is_android_strict():
        folder = QFileDialog.getExistingDirectory(parent, "打开漫画文件夹", "")
        return folder

    from mangaproof.storage import android_picker

    try:
        result = android_picker.pick("folder")
    except Exception as exc:
        log.warning("Android 文件夹选择器异常：%s", exc, exc_info=True)
        _warn(parent, f"无法打开系统文件夹选择器：{exc}")
        return ""
    if result["status"] == android_picker.RESULT_OK:
        return str(result["path"])
    if result["status"] == android_picker.RESULT_CANCEL:
        return ""
    _warn(parent, str(result["message"]) or "选择文件夹失败")
    return ""


def _warn(parent: QWidget | None, message: str) -> None:
    """把选择器的失败原因告诉用户（不静默失败）。"""
    log.warning("选择器提示：%s", message)
    if parent is None:
        return
    from PySide6.QtWidgets import QMessageBox

    QMessageBox.warning(parent, "无法选择", message)
