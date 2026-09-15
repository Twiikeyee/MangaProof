"""存储 / 选择器适配层（Android 专有实现集中在这里，桌面端零改动）。

模块
----
- `picker`：选择器门面。桌面端 = 原有 `QFileDialog`（行为完全不变）；
  Android = 系统原生 SAF 选择器（`android_picker`，绕开 Qt 6.11.2 原生文件
  对话框的同线程重入死锁）。
- `android_picker`：**只在 Android 上被 import**，通过共享文件协议驱动
  `PickerActivity`（Java）并把 SAF URI 映射成真实路径。

背景与完整论证见
`docs/Android端适配设计_原生文件读写_横屏全屏_分层图标_内存策略.md`。
"""

from __future__ import annotations

__all__ = ["picker"]
