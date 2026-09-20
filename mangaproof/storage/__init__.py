# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""存储 / 选择器适配层（Android 专有分支集中在这里，桌面端零改动）。

`picker`
--------
选择器门面，两端都用 `QFileDialog`：

- 桌面：与改造前**逐参数一致**（不传任何 option），行为完全不变；
- Android：额外传 `DontUseNativeDialog`，强制走 Qt 控件版对话框 —— Qt 在 Android 上
  默认使用平台原生 SAF 对话框，而 Qt 6.11.2 的
  `QAndroidPlatformFileDialogHelper` 存在同线程重入死锁（选中/取消后界面永久卡死），
  带该选项后 `canBeNativeDialog()` 直接返回 false，**完全不碰 Activity**。

Android 上用的是**真实路径**，因此访问 `/storage/emulated/0/...` 需要
`MANAGE_EXTERNAL_STORAGE` 已授予（清单已声明）。

历史方案（均已放弃，勿再引入）：自建 Java 原生选择器 + 共享文件协议；
清单注入 `extractNativeLibs`。详见
`docs/Android端适配设计_原生文件读写_横屏全屏_分层图标_内存策略.md` §2.13。
"""

from __future__ import annotations

__all__ = ["picker"]
