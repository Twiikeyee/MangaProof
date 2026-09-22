# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""更新页面（需求 §10、§11、§13、§29~§34）。

界面结构严格按需求 §11：

.. code-block:: text

    更新分支        [ stable ▼ ]
    更新渠道        [ Cloudflare R2 ▼ ]
    MirrorChyan CDK [                    ]
    代理            [                    ] [ 测试代理 ]
    下载限速        [ 不限速 ▼ ]
    --------------------------------
    （状态/进度区）
    [ 检查更新 ]                    [ 取消 ]

两条容易做错的约束，这里刻意用代码固化：

1. **按钮左右位置固定**（需求 §11.7：「检查更新」在左、「取消」在右）。
   因此**不用** ``QDialogButtonBox`` —— 它会按平台规范重排（macOS 会把主按钮放右侧）。
2. **只有点「检查更新」才保存配置**（需求 §13）：对话框内部持有 draft，
   点「检查更新」时才提交到 ``settings.update`` 并发 ``settings_committed``
   让主窗口落盘；点「取消」直接丢弃 draft。

视觉沿用现有 MangaProof 风格（需求 1.1.1）：不写死颜色、不自建样式表，
对话框自动继承 ``ui/theme.py`` 的全局暗色主题；进度条用 ``QProgressBar``
（检查阶段为不确定模式，需求 §31）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from mangaproof import APP_NAME
from mangaproof.config.settings import (
    R2_PUBLIC_BASE,
    SPEED_LIMIT_CHOICES,
    UPDATE_BRANCHES,
    UPDATE_CHANNELS,
    Settings,
    UpdateSettings,
)
from mangaproof.ui.update_worker import (
    CheckOutcome,
    ProxyTestWorker,
    UpdateCheckWorker,
    UpdateDownloadWorker,
)
from mangaproof.update import cdk_store
from mangaproof.update.errors import UpdateError
from mangaproof.update.humanize import human_size, human_speed
from mangaproof.update.models import CheckResult

log = logging.getLogger("mangaproof.ui.update_dialog")

#: 渠道显示名（值 → UI 文案）
CHANNEL_LABELS: dict[str, str] = {
    "r2": "Cloudflare R2",
    "github": "GitHub Release",
    "mirrorchyan": "MirrorChyan（需 CDK）",
}

#: 分支显示名
BRANCH_LABELS: dict[str, str] = {
    "stable": "stable（正式版）",
    "beta": "beta（测试版）",
    "alpha": "alpha（内测版）",
}


def _speed_label(value: int) -> str:
    return "不限速" if value == 0 else f"{value} M"


class UpdateDialog(QDialog):
    """「关于 → 更新」打开的更新页面。"""

    #: 用户在对话框里确认了配置（点「检查更新」）→ 主窗口负责写 settings.json
    settings_committed = Signal()
    #: 更新包已下载并校验通过，请求主程序启动安装器并退出（需求 §46）
    install_requested = Signal(object)      # Path

    def __init__(self, settings: Settings, *, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} 更新")
        self.setModal(True)
        self.setMinimumWidth(560)

        self._settings = settings
        # draft：点「取消」时丢弃，点「检查更新」时提交（需求 §13）
        self._draft = UpdateSettings.from_dict(settings.update.to_dict())
        self._check_worker: UpdateCheckWorker | None = None
        self._download_worker: UpdateDownloadWorker | None = None
        self._proxy_worker: ProxyTestWorker | None = None
        self._result: CheckResult | None = None
        self._outcome: CheckOutcome | None = None
        self._package: Path | None = None
        self._state = "idle"        # idle / checking / checked / no_update / downloading / done

        self._build_ui()
        self._load_draft()

    # -- 构建界面 ----------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.branch_combo = QComboBox()
        for value in UPDATE_BRANCHES:
            self.branch_combo.addItem(BRANCH_LABELS.get(value, value), value)
        form.addRow("更新分支", self.branch_combo)

        self.channel_combo = QComboBox()
        for value in UPDATE_CHANNELS:
            self.channel_combo.addItem(CHANNEL_LABELS.get(value, value), value)
        form.addRow("更新渠道", self.channel_combo)

        self.cdk_edit = QLineEdit()
        self.cdk_edit.setPlaceholderText("仅 MirrorChyan 渠道需要；留空则只能走 R2 / GitHub")
        self.cdk_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("MirrorChyan CDK", self.cdk_edit)

        proxy_row = QHBoxLayout()
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText("留空表示不使用代理；支持 http(s):// 与 socks5://")
        proxy_row.addWidget(self.proxy_edit, 1)
        self.proxy_test_btn = QPushButton("测试代理")
        self.proxy_test_btn.clicked.connect(self._on_test_proxy)
        proxy_row.addWidget(self.proxy_test_btn)
        proxy_widget = QWidget()
        proxy_widget.setLayout(proxy_row)
        form.addRow("代理", proxy_widget)

        self.speed_combo = QComboBox()
        for value in SPEED_LIMIT_CHOICES:
            self.speed_combo.addItem(_speed_label(value), value)
        form.addRow("下载限速", self.speed_combo)

        root.addLayout(form)

        self.hint_label = QLabel()
        self.hint_label.setWordWrap(True)
        self.hint_label.setObjectName("updateHint")
        root.addWidget(self.hint_label)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(line)

        self.status_label = QLabel("选择分支与渠道后点击「检查更新」。")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        root.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        self.detail_label.setVisible(False)
        root.addWidget(self.detail_label)

        buttons = QHBoxLayout()
        self.primary_btn = QPushButton("检查更新")
        self.primary_btn.setDefault(True)
        self.primary_btn.clicked.connect(self._on_primary)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self._on_cancel)
        # 需求 §11.7：主按钮在左、取消在右 —— 用 HBox 固定，不交给 QDialogButtonBox
        buttons.addWidget(self.primary_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_btn)
        root.addLayout(buttons)
        #: 按钮行的布局引用：位置是需求 §11.7 的硬要求，测试要能直接断言顺序
        self.action_row = buttons

        self._update_hint()

    def _load_draft(self) -> None:
        self._select(self.branch_combo, self._draft.branch)
        self._select(self.channel_combo, self._draft.channel)
        self._select(self.speed_combo, self._draft.speed_limit)
        self.proxy_edit.setText(self._draft.proxy)
        self.cdk_edit.setText(cdk_store.load_cdk(self._draft))

    @staticmethod
    def _select(combo: QComboBox, value) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _update_hint(self) -> None:
        """提示 CDK 存哪、代理/限速对哪些渠道生效（需求 §14/§29/§30）。"""
        from mangaproof.utils.platform import is_android_strict

        parts: list[str] = []
        if is_android_strict():
            parts.append("CDK 保存在应用私有目录的 settings.json。")
        elif cdk_store.keyring_available():
            parts.append("CDK 保存在系统凭据库（keyring），不写入配置文件。")
        else:
            parts.append("系统凭据库不可用，CDK 将明文保存在 settings.json。")
        parts.append("代理与限速仅对 Cloudflare R2 / GitHub 生效；MirrorChyan 不使用它们。")
        self.hint_label.setText("　".join(parts))

    # -- 表单 ↔ draft ------------------------------------------------------

    def _collect_draft(self) -> UpdateSettings:
        self._draft.branch = self.branch_combo.currentData()
        self._draft.channel = self.channel_combo.currentData()
        self._draft.speed_limit = int(self.speed_combo.currentData())
        self._draft.proxy = self.proxy_edit.text().strip()
        return self._draft

    def _commit(self, *, save_cdk: bool = True) -> None:
        """把 draft 提交到真实设置并发信号（需求 §13：点检查更新才保存）。"""
        draft = self._collect_draft()
        if save_cdk:
            # 写入 keyring 成功时 draft.cdk 会被清空（需求 §14）
            cdk_store.save_cdk(draft, self.cdk_edit.text().strip())
        self._settings.update = draft
        self.settings_committed.emit()

    # -- 按钮行为 ----------------------------------------------------------

    def _on_primary(self) -> None:
        if self._state in ("idle", "checked", "no_update"):
            self._start_check()
        elif self._state == "update_available":
            self._start_download()
        elif self._state == "done" and self._package is not None:
            # 需求 §46：由主窗口负责"启动安装器 → 确认拉起成功 → 主程序退出"；
            # 先发信号（主窗口可能弹错误框），再关闭本对话框。
            self.install_requested.emit(self._package)
            self.accept()

    def _on_cancel(self) -> None:
        self._abort_workers()
        self.reject()

    def _abort_workers(self) -> None:
        if self._check_worker is not None and self._check_worker.isRunning():
            self._check_worker.requestInterruption()
        if self._download_worker is not None and self._download_worker.isRunning():
            self._download_worker.request_cancel()
        if self._proxy_worker is not None and self._proxy_worker.isRunning():
            self._proxy_worker.wait(2000)

    # -- 检查更新 ----------------------------------------------------------

    def _start_check(self) -> None:
        self._commit()                       # 需求 §13：保存当前配置后再检查

        self._state = "checking"
        self._result = None
        self._set_form_enabled(False)
        self.primary_btn.setEnabled(False)
        self.status_label.setText("正在检查更新……")
        self.detail_label.setVisible(False)
        self.progress.setRange(0, 0)         # 需求 §31：不确定进度条
        self.progress.setVisible(True)

        worker = UpdateCheckWorker(
            branch=self._draft.branch,
            source=self._draft.channel,
            proxy=self._draft.proxy,
            parent=self,
        )
        worker.progress.connect(self.status_label.setText)
        worker.succeeded.connect(self._on_check_ok)
        worker.failed.connect(self._on_check_failed)
        self._check_worker = worker
        worker.start()

    def _on_check_ok(self, outcome: CheckOutcome) -> None:
        self.progress.setVisible(False)
        self.progress.setRange(0, 100)
        self._set_form_enabled(True)
        self._outcome = outcome

        if outcome.kind == "unsupported":
            self._state = "no_update"
            self.status_label.setText(outcome.message)
            self.primary_btn.setText("检查更新")
            self.primary_btn.setEnabled(True)
            return

        result = outcome.result
        self._result = result
        if not result.has_update:
            # 需求 §32
            self._state = "no_update"
            self.status_label.setText(
                "当前已经是最新版本\n\n"
                f"当前版本：{result.current_display}\n"
                f"更新分支：{result.branch}"
            )
            self.primary_btn.setText("检查更新")
            self.primary_btn.setEnabled(True)
            return

        # 需求 §33：不自动下载，等用户点「立即更新」
        self._state = "update_available"
        size_text = human_size(outcome.filesize) if outcome.filesize else (
            outcome.size_note or "—"
        )
        note = f"\n\n更新说明：\n{result.release.release_note}" if result.release.release_note else ""
        self.status_label.setText(
            "发现新版本\n\n"
            f"当前版本：{result.current_display}\n"
            f"最新版本：{result.latest_display}\n\n"
            f"分支：{result.branch}\n\n"
            f"文件：\n{outcome.filename or '—'}\n\n"
            f"大小：\n{size_text}{note}"
        )
        self.primary_btn.setText("立即更新")
        self.primary_btn.setEnabled(True)

    def _on_check_failed(self, error: object) -> None:
        self.progress.setVisible(False)
        self.progress.setRange(0, 100)
        self._set_form_enabled(True)
        self._state = "checked"
        self.primary_btn.setText("检查更新")
        self.primary_btn.setEnabled(True)
        self.status_label.setText(self._error_text(error))

    # -- 下载 --------------------------------------------------------------

    def _start_download(self) -> None:
        if self._result is None:
            return
        from mangaproof.update import detector

        target = detector.current_target()
        if target is None:
            self.status_label.setText("当前系统架构暂不支持更新")
            return

        self._state = "downloading"
        self._set_form_enabled(False)
        self.primary_btn.setEnabled(False)
        self.detail_label.setVisible(True)
        self.status_label.setText("正在准备下载……")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(True)

        worker = UpdateDownloadWorker(
            branch=self._draft.branch,
            source=self._draft.channel,
            release=self._result.release,
            target=target,
            dest_dir=self._download_dir(),
            proxy=self._draft.proxy,
            speed_limit_mbps=self._draft.speed_limit,
            cdk=self.cdk_edit.text().strip(),
            parent=self,
        )
        worker.progress.connect(self._on_download_progress)
        worker.succeeded.connect(self._on_download_ok)
        worker.failed.connect(self._on_download_failed)
        self._download_worker = worker
        worker.start()

    def _download_dir(self) -> Path:
        """下载目录（需求 §36/§37）：Windows 用 TEMP，Linux/macOS 用 CACHE。"""
        from mangaproof.update.platform_dirs import update_package_dir

        return update_package_dir()

    def _on_download_progress(
        self, done: int, total: object, speed: object, phase: str
    ) -> None:
        self.status_label.setText(f"{phase}…" if phase.endswith("…") is False else phase)
        total_int = int(total) if isinstance(total, int) and total > 0 else None
        if total_int:
            percent = min(100, int(done * 100 / total_int))
            self.progress.setRange(0, 100)
            self.progress.setValue(percent)
        else:
            self.progress.setRange(0, 0)     # 需求 §34：无 Content-Length 用不确定进度
        parts = [f"{human_size(done)} / {human_size(total_int) if total_int else '未知'}"]
        if isinstance(speed, (int, float)) and speed:
            parts.append(f"速度：{human_speed(float(speed))}")
        self.detail_label.setText("　".join(parts))

    def _on_download_ok(self, path: object) -> None:
        from mangaproof.update import checksum

        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self._state = "done"
        self._package = Path(str(path))
        digest = checksum.sha256_of(self._package)
        self.status_label.setText(
            "更新包已下载并通过校验\n\n"
            f"文件：{self._package.name}\n"
            f"SHA-256：{digest[:16]}…"
        )
        self.detail_label.setText(str(self._package))
        self.primary_btn.setText("立即安装并重启")
        self.primary_btn.setEnabled(True)
        self._set_form_enabled(False)
        self.cancel_btn.setText("稍后")

    def _on_download_failed(self, error: object) -> None:
        from mangaproof.update.downloader import DownloadCancelled

        self.progress.setVisible(False)
        self.progress.setRange(0, 100)
        self._set_form_enabled(True)
        self.detail_label.setVisible(False)
        if isinstance(error, DownloadCancelled):
            self._state = "update_available"
            self.primary_btn.setText("立即更新")
            self.primary_btn.setEnabled(True)
            self.status_label.setText("已取消下载。")
            return
        self._state = "update_available"
        self.primary_btn.setText("立即更新")
        self.primary_btn.setEnabled(True)
        self.status_label.setText(self._error_text(error))

    # -- 代理测试 ----------------------------------------------------------

    def _on_test_proxy(self) -> None:
        proxy = self.proxy_edit.text().strip()
        self.proxy_test_btn.setEnabled(False)
        self.proxy_test_btn.setText("测试中…")
        worker = ProxyTestWorker(proxy=proxy, parent=self)
        worker.finished_with.connect(self._on_proxy_result)
        self._proxy_worker = worker
        worker.start()

    def _on_proxy_result(self, ok: bool, message: str) -> None:
        self.proxy_test_btn.setEnabled(True)
        self.proxy_test_btn.setText("测试代理")
        self.detail_label.setVisible(True)
        self.detail_label.setText(("✓ " if ok else "✗ ") + message)

    # -- 辅助 --------------------------------------------------------------

    def _set_form_enabled(self, enabled: bool) -> None:
        for widget in (
            self.branch_combo, self.channel_combo, self.cdk_edit,
            self.proxy_edit, self.proxy_test_btn, self.speed_combo,
        ):
            widget.setEnabled(enabled)

    @staticmethod
    def _error_text(error: object) -> str:
        if isinstance(error, UpdateError):
            return f"更新失败：\n{error}"
        return f"更新失败：\n{error}"

    def closeEvent(self, event) -> None:      # noqa: N802 - Qt 命名
        self._abort_workers()
        super().closeEvent(event)

    # -- 供主窗口调用 ------------------------------------------------------

    def take_package(self) -> Path | None:
        """取走已校验的更新包路径（主窗口用它启动安装器）。"""
        return self._package


__all__ = ["UpdateDialog", "CHANNEL_LABELS", "BRANCH_LABELS"]
