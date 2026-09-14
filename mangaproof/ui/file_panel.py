"""PSD 文件列表面板（需求 §11.1、§29）。"""

from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mangaproof.review.state import (
    FAILED,
    PARTIAL,
    PASSED,
    STATUS_ICONS,
    UNREVIEWED,
)
from mangaproof.ui.theme import COLOR_FAIL, COLOR_PASS, COLOR_UNREVIEWED, COLOR_WARN

# 文件级状态 → （颜色, 图标）：键必须是 TaskState.file_status() 的返回值，
# 即 state 模块的状态常量，避免字符串拼写分叉导致落到未监制兜底样式。
STATUS_STYLES = {
    UNREVIEWED: (COLOR_UNREVIEWED, STATUS_ICONS[UNREVIEWED]),
    PASSED: (COLOR_PASS, STATUS_ICONS[PASSED]),
    FAILED: (COLOR_FAIL, STATUS_ICONS[FAILED]),
    PARTIAL: (COLOR_WARN, STATUS_ICONS[PARTIAL]),
}


class FilePanel(QWidget):
    file_activated = Signal(int)   # 文件索引

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._files: List[str] = []  # 相对路径（与任务 files 顺序一致）
        self._statuses: Dict[str, str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self.title_label = QLabel("PSD 文件")
        self.title_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.title_label)

        self.list_widget = QListWidget()
        self.list_widget.currentRowChanged.connect(self._on_current_row_changed)
        layout.addWidget(self.list_widget)

    def set_task_info(self, task_name: str, task_type: str) -> None:
        kind = "文件夹任务" if task_type == "folder" else "单文件任务"
        self.title_label.setText(f"PSD 文件 — {task_name}（{kind}）")

    def set_files(self, files: List[str]) -> None:
        self._files = list(files)
        self.list_widget.clear()
        for rel in files:
            item = QListWidgetItem(rel)
            item.setData(Qt.ItemDataRole.UserRole, rel)
            self.list_widget.addItem(item)

    def set_file_statuses(self, statuses: Dict[str, str]) -> None:
        """statuses: {rel_path: TaskState.file_status() 返回值}。

        取值域：PASSED("passed") / FAILED("failed") / PARTIAL("partial") /
        UNREVIEWED("unreviewed")，未知值按未监制样式兜底。
        """
        self._statuses = dict(statuses)
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            rel = item.data(Qt.ItemDataRole.UserRole)
            status = self._statuses.get(rel, UNREVIEWED)
            color, icon = STATUS_STYLES.get(status, STATUS_STYLES[UNREVIEWED])
            item.setText(f"{icon} {rel}")
            item.setForeground(QColor(color))

    def set_current_row(self, row: int) -> None:
        if 0 <= row < self.list_widget.count():
            self.list_widget.setCurrentRow(row)

    def clear_selection(self) -> None:
        self.list_widget.setCurrentRow(-1)

    def _on_current_row_changed(self, row: int) -> None:
        if row >= 0:
            self.file_activated.emit(row)
