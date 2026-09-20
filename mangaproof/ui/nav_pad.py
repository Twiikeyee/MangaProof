# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""Android 触屏用的方向键模拟（浮动十字键）。

为什么需要
----------
桌面端靠键盘方向键完成"上/下一个 PSD、上/下一个图层"（`main_window.prev_psd` 等）；
触屏设备没有方向键，而画布上的手势已用于**平移/拖框**，不适合再抢来翻页。
因此 Android 上在画布右下角浮出一组十字键。

设计要点（需求方已确认：B + ① + 置灰、不做长按）
-----------------------------------------------
* **位置**：Viewer 右下角浮动，半透明，不用时几乎不干扰画面；
* **接通方式（①）**：按钮点击**直接调用** `MainWindow` 的四个动作方法，不伪造按键事件
  —— 没有间接层，也不会撞上"快捷键歧义""文本框守卫"这类机制；
* **置灰**：未打开任务、或已到边界（第一个/最后一个）时对应按钮禁用，触屏没有
  "按了没反应"的反馈，置灰能直接说明"到头了"；
* **不做长按连发**：保持简单，真机需要再加。

只在 Android 创建（由 `MainWindow._build_ui` 用 `is_android_strict()` 判定），
桌面端不导入、不创建、布局零变化。

实现细节
--------
* 容器**不绘制背景**（透明），只有四个按钮自己画半透明底：这样容器覆盖的那块区域里，
  按钮之间的缝隙依然能被画布收到，不影响拖拽平移/拖框；
* 按钮 `NoFocus`：点击不抢焦点，避免影响后续快捷键行为（例如问题批注输入框）；
* 尺寸用 dp 值，跟随全局 `QT_SCALE_FACTOR` 缩放，与其它控件保持一致；
* 字符用 ▲▼◀▶（已实测 MiSans 自带这些字形，不依赖回退字体）。
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QPushButton, QWidget

#: 单个按钮边长（dp）。比桌面工具栏按钮(33dp 高)大，贴近触屏推荐的可点面积。
BUTTON_DP = 44
#: 按钮间距（dp）
BUTTON_GAP_DP = 4
#: 距画布右下角的留白（dp）
MARGIN_DP = 12
#: 按钮上的字符（MiSans 自带字形，已实测）
GLYPH_UP, GLYPH_DOWN, GLYPH_LEFT, GLYPH_RIGHT = "▲", "▼", "◀", "▶"

_BUTTON_QSS = """
QPushButton {
    background-color: rgba(32, 34, 39, 190);
    color: #e4e6eb;
    border: 1px solid rgba(255, 255, 255, 38);
    border-radius: 6px;
    font-size: 17px;
    padding: 0px;
}
QPushButton:pressed {
    background-color: rgba(74, 144, 217, 215);
    border-color: rgba(255, 255, 255, 90);
}
QPushButton:disabled {
    background-color: rgba(32, 34, 39, 90);
    color: rgba(228, 230, 235, 80);
    border-color: rgba(255, 255, 255, 18);
}
"""


class NavPad(QWidget):
    """画布右下角的浮动十字键（仅 Android 创建）。

    :param parent: 父窗口（Viewer），自身不绘制背景
    :param callbacks: 四个动作的回调，键为 ``prev_file`` / ``next_file`` /
                      ``prev_layer`` / ``next_layer``；缺哪个就禁用哪个按钮
    """

    def __init__(self, parent: QWidget, callbacks: Optional[dict[str, Callable[[], None]]] = None):
        super().__init__(parent)
        self._callbacks = dict(callbacks or {})

        self.btn_up = self._make_button(GLYPH_UP, "上一个 PSD", "prev_file")
        self.btn_down = self._make_button(GLYPH_DOWN, "下一个 PSD", "next_file")
        self.btn_left = self._make_button(GLYPH_LEFT, "上一个图层", "prev_layer")
        self.btn_right = self._make_button(GLYPH_RIGHT, "下一个图层", "next_layer")

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(BUTTON_GAP_DP)
        # 十字布局：上行居中放 ▲，下行依次 ◀ ▼ ▶
        grid.addWidget(self.btn_up, 0, 1, alignment=Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(self.btn_left, 1, 0)
        grid.addWidget(self.btn_down, 1, 1)
        grid.addWidget(self.btn_right, 1, 2)
        # 容器透明：缝隙处的事件仍落到画布上（不影响拖拽平移/拖框）
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    # ------------------------------------------------------------------ 构造

    def _make_button(self, glyph: str, tip: str, action: str) -> QPushButton:
        btn = QPushButton(glyph, self)
        btn.setFixedSize(BUTTON_DP, BUTTON_DP)
        btn.setToolTip(tip)
        btn.setStyleSheet(_BUTTON_QSS)
        # 点击不抢焦点：否则会从 Viewer / 批注输入框手里把焦点拿走
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.clicked.connect(lambda _=False, a=action: self._invoke(a))
        return btn

    def _invoke(self, action: str) -> None:
        callback = self._callbacks.get(action)
        if callback is not None:
            callback()

    # ------------------------------------------------------------------ 状态

    def set_enabled_state(
        self,
        *,
        can_prev_file: bool,
        can_next_file: bool,
        can_prev_layer: bool,
        can_next_layer: bool,
    ) -> None:
        """按当前任务/文件/图层位置刷新四个按钮的可用性。

        置灰而不是"点了没反应"：触屏没有键盘那种"按下去没生效"的直觉反馈。
        """
        self.btn_up.setEnabled(can_prev_file)
        self.btn_down.setEnabled(can_next_file)
        self.btn_left.setEnabled(can_prev_layer)
        self.btn_right.setEnabled(can_next_layer)

    # ------------------------------------------------------------------ 定位

    def reposition(self) -> None:
        """贴到父控件右下角（父控件 resize 时调用）。"""
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        self.move(
            max(0, parent.width() - self.width() - MARGIN_DP),
            max(0, parent.height() - self.height() - MARGIN_DP),
        )
