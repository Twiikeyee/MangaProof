"""进程退出收尾（平台差异集中在这里）。

为什么 Android 上要绕过正常退出流程
------------------------------------
Qt 官方文档对这条退出路径有明确说明（Qt for Android Environment Variables 页，
`QT_ANDROID_NO_EXIT_CALL` 条目，Qt 6.11）：

    In some cases, an Android app might not be able to safely clean all threads
    while calling exit() and it might crash. This is because there are C++
    threads running and destroying these without joining them terminates an
    application. These threads cannot be joined because it's not possible to
    know if they are running.

这就是本项目真机上「关闭/退出应用时闪退」的机制（**QTBUG-85449 家族**，
"Android: crash on exit"）：退出阶段要跑全局析构与 Python 解释器 finalize，
会去销毁仍在运行的 Qt/C++ 线程 → 进程被 terminate。

Qt 官方给的绕行方式正是「不调用 `exit()`，交给 Android 系统处理，代价是不跑全局
析构」；本模块与它同向：Android 上直接 `os._exit()`，**跳过 CPython 收尾与
Qt/C++ 析构**，由系统回收进程。桌面平台保持 `sys.exit()` 的原有语义
（atexit、析构、缓冲区 flush 照常）。

为什么是安全的（已在代码里核实，不是假设）
------------------------------------------
- **数据不丢**：`MainWindow.closeEvent()` 里显式 `save_task()` + `_save_settings()`
  （并先等各 worker 退出），这些都发生在 `QApplication.exec()` 返回**之前**；
  退出逻辑不依赖析构或 atexit。
- **日志不丢**：`logging_setup` 用的 `RotatingFileHandler` 每条记录即 flush，
  另有实时 stderr 代理。
- 子进程/线程：`closeEvent()` 已请求取消并 `wait()` 各 worker；Android 进程由系统回收。

补充：`QT_ANDROID_NO_EXIT_CALL=1` 对本路径**无效**——它只作用于 Qt 自己那条收尾
（`androidjnimain.cpp`：main 返回后 `if (!qEnvironmentVariableIsSet(
"QT_ANDROID_NO_EXIT_CALL")) exit(ret);`），而我们的进程由 Python 主导退出。
它也**不必**设置：其语义是"让进程别退出"，一旦某条原生路径先返回、而这里的
`os._exit()` 没跑到，结果会从"崩溃"变成"卡死"（更糟）。故仅作文档记录。

参考：https://doc.qt.io/qt-6/android-environment-variables.html
（"Enabling or disabling workarounds" → `QT_ANDROID_NO_EXIT_CALL`）
"""

from __future__ import annotations

import os
import sys
from typing import NoReturn


def is_android() -> bool:
    """当前是否运行在 Android 上。

    三重判定（任一命中即为 Android）——因为 p4a 编译的 CPython 3.11 并不把
    `sys.platform` 报成 `"android"`（那是 CPython 官方 Android 构建 3.13+ 的行为）：

    1. `sys.platform == "android"`：官方 Android 构建；
    2. `ANDROID_ROOT` 环境变量：Android 框架为每个应用进程设置；
    3. Qt 自身的判定（`QOperatingSystemVersion`，最权威，PySide6 6.11.2 已绑定）。
    """
    if sys.platform == "android":
        return True
    if os.environ.get("ANDROID_ROOT"):
        return True
    try:
        from PySide6.QtCore import QOperatingSystemVersion

        return (QOperatingSystemVersion.currentType()
                == QOperatingSystemVersion.OSType.Android)  # type: ignore[attr-defined]
    except Exception:
        return False


def exit_app(code: int | None = 0) -> NoReturn:
    """退出进程；Android 上跳过收尾流程（见模块 docstring）。"""
    exit_code = 0 if code is None else int(code)
    if is_android():
        os._exit(exit_code)
    sys.exit(exit_code)
