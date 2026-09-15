"""问题面板（需求 §33～§38）。

当前图层的 Issue 列表、状态按钮、添加入口（拖框 / 自定义批注）。
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from mangaproof.review.issue import Issue
from mangaproof.review.state import FAILED, PASSED, UNREVIEWED
from mangaproof.ui.theme import (
    COLOR_ACCENT,
    COLOR_FAIL,
    COLOR_PASS,
    COLOR_UNREVIEWED,
    COLOR_WARN,
)


class _ElidedLabel(QLabel):
    """单行显示、超出宽度以省略号截断的标签。

    PSD 文字图层名常与文字内容相同（可能非常长）：与图层列表面板的
    QListWidget 一致，按宽度省略而不是撑宽布局或换行。完整名称悬停
    查看（tooltip）。
    """

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        # 水平 Ignored：布局忽略本标签的文本宽度，面板宽度由其他控件
        # 决定，长图层名不再把 UI 撑宽
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        elided = self.fontMetrics().elidedText(
            self.text(), Qt.TextElideMode.ElideRight, self.width()
        )
        flags = int(self.alignment()) | int(Qt.TextFlag.TextSingleLine)
        painter.drawText(self.rect(), flags, elided)
        painter.end()


class IssuePanel(QWidget):
    status_change_requested = Signal(str)     # UNREVIEWED / PASSED / FAILED
    add_issue_requested = Signal()            # 进入拖框模式（方式 B）
    continuous_toggled = Signal(bool)         # 「连续标注」开关
    auto_box_requested = Signal()             # 自动框选当前图层
    custom_comment_requested = Signal()       # 自定义批注（Ctrl+Enter）
    edit_issue_requested = Signal(str)        # issue_id（双击编辑）
    delete_issue_requested = Signal(str)      # issue_id

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._issues: List[Issue] = []
        self._status = UNREVIEWED

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        title = QLabel("当前图层问题")
        title.setStyleSheet("font-weight: bold;")
        layout.addWidget(title)

        self.layer_name_label = _ElidedLabel("图层：-")
        layout.addWidget(self.layer_name_label)

        self.status_label = QLabel("状态：○ 未监制")
        layout.addWidget(self.status_label)

        status_row = QHBoxLayout()
        self.pass_btn = QPushButton("✓ 通过")
        self.pass_btn.setStyleSheet(f"QPushButton {{ color: {COLOR_PASS}; }}")
        self.pass_btn.setToolTip("标记当前图层通过")
        self.fail_btn = QPushButton("✗ 未通过")
        self.fail_btn.setStyleSheet(f"QPushButton {{ color: {COLOR_FAIL}; }}")
        self.fail_btn.setToolTip("标记当前图层未通过")
        self.reset_btn = QPushButton("○ 重置")
        self.reset_btn.setToolTip("重置为未监制（同时清除该层问题）")
        status_row.addWidget(self.pass_btn)
        status_row.addWidget(self.fail_btn)
        status_row.addWidget(self.reset_btn)
        layout.addLayout(status_row)

        self.pass_btn.clicked.connect(lambda: self.status_change_requested.emit(PASSED))
        self.fail_btn.clicked.connect(lambda: self.status_change_requested.emit(FAILED))
        self.reset_btn.clicked.connect(lambda: self.status_change_requested.emit(UNREVIEWED))

        self.issue_list = QListWidget()
        self.issue_list.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.issue_list)

        add_row = QHBoxLayout()
        self.add_btn = QPushButton("＋ 添加问题")
        self.add_btn.clicked.connect(self.add_issue_requested.emit)
        self.add_btn.setToolTip("在画布上拖框圈出问题位置，然后选择问题类型")
        add_row.addWidget(self.add_btn, 3)

        # 连续标注开关：勾选状态必须一眼可见（文字 + 高亮底色双重提示）
        self.continuous_btn = QPushButton("连续标注")
        self.continuous_btn.setCheckable(True)
        self.continuous_btn.setStyleSheet(
            f"QPushButton:checked {{ background-color: {COLOR_ACCENT};"
            f" color: white; border-color: {COLOR_ACCENT}; font-weight: bold; }}"
        )
        self.continuous_btn.toggled.connect(self._on_continuous_toggled)
        add_row.addWidget(self.continuous_btn, 2)
        layout.addLayout(add_row)
        self._update_continuous_visual()

        # 自动框选：按当前图层视觉边界自动生成红框（仅在拖框模式下可用）
        self.auto_box_btn = QPushButton("▣ 自动框选")
        self.auto_box_btn.clicked.connect(self.auto_box_requested.emit)
        self.auto_box_btn.setEnabled(False)
        layout.addWidget(self.auto_box_btn)
        self._update_auto_box_visual()

        self.custom_btn = QPushButton("✎ 自定义批注")
        self.custom_btn.clicked.connect(self.custom_comment_requested.emit)
        layout.addWidget(self.custom_btn)

        self.delete_btn = QPushButton("🗑 删除选中问题")
        self.delete_btn.clicked.connect(self._on_delete)
        layout.addWidget(self.delete_btn)

        self.hint_label = QLabel("")
        self.hint_label.setObjectName("hintLabel")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

    # -- 更新 --------------------------------------------------------------

    def clear(self) -> None:
        """回到「未打开任务」的初始状态（关闭当前任务用）。"""
        self._issues = []
        self._status = UNREVIEWED
        self.layer_name_label.setText("图层：-")
        self.layer_name_label.setToolTip("图层：-")
        self.status_label.setText("状态：○ 未监制")
        self.issue_list.clear()
        self.set_hint("")
        self.set_buttons_enabled(False)
        self.set_auto_box_enabled(False)

    def set_current(self, layer_name: str, status: str, issues: List[Issue]) -> None:
        self._issues = list(issues)
        self._status = status
        self.layer_name_label.setText(f"图层：{layer_name}")
        self.layer_name_label.setToolTip(f"图层：{layer_name}")
        status_text = {"unreviewed": "○ 未监制", "passed": "✓ 已通过", "failed": "✗ 未通过"}
        self.status_label.setText(f"状态：{status_text.get(status, status_text['unreviewed'])}")

        self.issue_list.clear()
        for issue in self._issues:
            comment = f" — {issue.comment}" if issue.comment else ""
            rect_info = ""
            x, y, w, h = issue.rect
            if w > 0 and h > 0:
                rect_info = f"　[{int(x)},{int(y)} {int(w)}×{int(h)}]"
            item = QListWidgetItem(f"#{issue.issue_no} {issue.type}{comment}{rect_info}")
            item.setData(Qt.ItemDataRole.UserRole, issue.issue_id)
            self.issue_list.addItem(item)

    def set_hint(self, text: str) -> None:
        self.hint_label.setText(text)

    # -- 连续标注 ----------------------------------------------------------

    def continuous(self) -> bool:
        return self.continuous_btn.isChecked()

    def set_continuous(self, enabled: bool) -> None:
        """设置连续标注开关（不触发 continuous_toggled，避免回环保存）。"""
        if self.continuous_btn.isChecked() == bool(enabled):
            self._update_continuous_visual()
            return
        self.continuous_btn.blockSignals(True)
        self.continuous_btn.setChecked(bool(enabled))
        self.continuous_btn.blockSignals(False)
        self._update_continuous_visual()

    def _on_continuous_toggled(self, checked: bool) -> None:
        self._update_continuous_visual()
        self.continuous_toggled.emit(bool(checked))

    def _update_continuous_visual(self) -> None:
        """勾选状态双重可见：按钮文字 + 高亮底色（:checked 样式）。"""
        on = self.continuous_btn.isChecked()
        self.continuous_btn.setText("✓ 连续标注" if on else "连续标注")
        self.continuous_btn.setToolTip(
            "连续标注：开启后标完一个问题仍保持拖框模式，可接着标下一个；\n"
            "关闭（默认）时标完一个问题自动退出拖框模式。\n"
            "对「红框模式」与问题类型快捷键（1~0、Q~D…）都生效。\n"
            f"当前：{'开启' if on else '关闭'}"
        )

    # -- 自动框选 ----------------------------------------------------------

    def set_auto_box_enabled(self, enabled: bool) -> None:
        """自动框选仅在拖框模式（红框模式 / 已选问题类型）下可用。"""
        self.auto_box_btn.setEnabled(bool(enabled))

    def _update_auto_box_visual(self) -> None:
        self.auto_box_btn.setToolTip(
            "自动框选：按当前图层的视觉内容范围自动生成红框\n"
            "（上下左右各外扩 5 像素，中心与蓝色虚线框一致）。\n"
            "需先进入拖框模式：按 R 红框模式，或按问题类型快捷键。"
        )

    def set_shortcut_labels(self, bindings: dict, issue_type_tips: str = "") -> None:
        """动态显示当前绑定的快捷键（需求 §30）。

        bindings: {"pass", "fail", "redraw", "custom", "cancel"} 快捷键文案；
        issue_type_tips: 问题类型快捷键清单（显示在拖框按钮 tooltip）。
        """
        self.pass_btn.setText(f"✓ 通过 ({bindings.get('pass', 'Enter')})")
        self.pass_btn.setToolTip(f"标记当前图层通过　快捷键：{bindings.get('pass', 'Enter')}")
        self.fail_btn.setText(f"✗ 未通过 ({bindings.get('fail', '/')})")
        self.fail_btn.setToolTip(f"标记当前图层未通过　快捷键：{bindings.get('fail', '/')}")
        self.add_btn.setText(f"＋ 添加问题 ({bindings.get('redraw', 'R')})")
        self.auto_box_btn.setText(f"▣ 自动框选 ({bindings.get('auto_box', 'A')})")
        self.custom_btn.setText(f"✎ 自定义批注 ({bindings.get('custom', 'Ctrl+Enter')})")
        if issue_type_tips:
            self.add_btn.setToolTip(issue_type_tips)

    def set_buttons_enabled(self, enabled: bool) -> None:
        self.pass_btn.setEnabled(enabled)
        self.fail_btn.setEnabled(enabled)
        self.reset_btn.setEnabled(enabled)

    def _on_double_click(self, item: QListWidgetItem) -> None:
        issue_id = item.data(Qt.ItemDataRole.UserRole)
        if issue_id:
            self.edit_issue_requested.emit(issue_id)

    def _on_delete(self) -> None:
        item = self.issue_list.currentItem()
        if item is None:
            return
        issue_id = item.data(Qt.ItemDataRole.UserRole)
        if issue_id:
            self.delete_issue_requested.emit(issue_id)
