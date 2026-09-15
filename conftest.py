"""pytest 公共夹具。

只做一件事：**保证整个测试会话里只有一个 QApplication 实例**。

为什么需要
---------
仓库里多个 GUI 测试模块各自写了 `QApplication.instance() or QApplication([])`，
这行代码会在**收集阶段**（import 时）执行。如果某个模块先被收集并抢先创建了
QApplication，随后的窗口激活 / 焦点相关断言（例如
`test_gui_smoke.py::test_no_wheel_combo_app_wide` 里"聚焦时滚轮不改值"的用例）
就会失败——表现为 `combo.hasFocus()` 恒为 False，与实际功能无关。

统一由本夹具按需创建一个会话级实例后，各模块的
`QApplication.instance() or QApplication([])` 都会拿到同一个对象，
收集顺序不再影响结果。
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def qapp():
    """唯一的 QApplication（headless 环境用 offscreen 平台）。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    # 不主动 quit：会话结束时由解释器回收即可（各测试自己负责关窗口）
