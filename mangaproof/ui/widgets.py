# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""通用小部件。

目前只有一件：不响应滚轮的下拉框。
"""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox


class NoWheelComboBox(QComboBox):
    """任何情况下都不响应滚轮的下拉框（聚焦与否都一样）。

    Fusion 风格默认允许滚轮直接改下拉框的值
    （QStyle::SH_ComboBox_AllowWheelScrolling），代价是"想滚页面/顺手划一下
    就改掉了设置，还不知道原来选的是什么"——下拉框里都是语义选项，
    误改后很难察觉，也没有撤销。这里一律忽略滚轮事件：

    - 在滚动区内：事件冒泡给滚动区，滚轮只管滚页面；
    - 不在滚动区内：事件冒泡给父级，没有滚动条时即被丢弃，什么都不会发生。

    改值一律走显式交互：点击展开选择，或聚焦后用键盘
    （↑/↓ 切换、首字母跳转、空格展开）。
    """

    def wheelEvent(self, event) -> None:   # noqa: N802（Qt 命名）
        event.ignore()
