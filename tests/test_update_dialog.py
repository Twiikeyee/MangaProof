# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""更新页面的单测（需求 §11、§13、§31~§34）。

跑在离屏 Qt 上，不联网、不真的启动 worker（只验界面结构与"保存时机"语义）。
需要 `qapp` fixture（见 tests/test_android_ui_scale.py 的同名 fixture 定义）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from mangaproof.config.settings import SettingsManager, UpdateSettings  # noqa: E402
from mangaproof.ui.update_dialog import CHANNEL_LABELS, UpdateDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    """复用仓库既有约定：离屏 QApplication（qtbot 未使用，QApplication 全局唯一）。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _no_keyring_and_temp_cache(monkeypatch, tmp_path):
    """测试里不碰真实系统凭据库，也不往 ~/.cache 写东西。"""
    from mangaproof.update import cdk_store

    monkeypatch.setattr(cdk_store, "keyring_available", lambda: False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))


@pytest.fixture
def dialog(qapp, tmp_path):
    manager = SettingsManager(tmp_path / "settings.json")
    manager.save()
    dlg = UpdateDialog(manager.settings)
    yield dlg, manager
    dlg.close()


def test_form_has_all_controls_from_requirement(dialog):
    """需求 §11 的页面元素一个都不能少。"""
    dlg, _ = dialog
    assert dlg.branch_combo.count() == 3
    assert [dlg.branch_combo.itemData(i) for i in range(3)] == ["stable", "beta", "alpha"]
    assert [dlg.channel_combo.itemData(i) for i in range(3)] == [
        "r2", "github", "mirrorchyan",
    ]
    assert dlg.channel_combo.currentData() == "r2"          # 需求 §12 默认渠道
    assert dlg.branch_combo.currentData() == "stable"       # 需求 §12 默认分支
    assert dlg.cdk_edit.isEnabled()
    assert dlg.proxy_edit.text() == ""
    assert dlg.speed_combo.currentData() == 0               # 不限速
    assert dlg.proxy_test_btn.text() == "测试代理"


def test_buttons_are_primary_left_cancel_right(dialog):
    """需求 §11.7：底部「检查更新」在左、「取消」在右。

    不用 QDialogButtonBox 就是为了这个 —— 它会按平台规范重排。
    """
    dlg, _ = dialog
    assert dlg.primary_btn.text() == "检查更新"
    assert dlg.cancel_btn.text() == "取消"
    row = dlg.action_row
    assert row.indexOf(dlg.primary_btn) == 0, "主按钮必须在最左"
    assert row.indexOf(dlg.cancel_btn) == row.count() - 1, "取消必须在最右"
    # 两者之间必须有 stretch（否则会被拉成等宽或贴在一起）
    assert row.itemAt(1).spacerItem() is not None


def test_progress_is_hidden_initially(dialog):
    dlg, _ = dialog
    assert not dlg.progress.isVisible()
    assert not dlg.detail_label.isVisible()


def test_speed_limit_labels(dialog):
    dlg, _ = dialog
    labels = [dlg.speed_combo.itemText(i) for i in range(dlg.speed_combo.count())]
    assert labels == ["不限速", "10 M", "20 M", "30 M", "40 M", "50 M"]


def test_cancel_does_not_commit_changes(dialog):
    """需求 §13：点「取消」不保存本次未执行检查的修改。"""
    dlg, manager = dialog
    dlg.branch_combo.setCurrentIndex(dlg.branch_combo.findData("beta"))
    dlg.proxy_edit.setText("http://127.0.0.1:7890")
    saved_before = UpdateSettings.from_dict(manager.settings.update.to_dict())

    dlg._on_cancel()          # 等价于点「取消」

    assert manager.settings.update == saved_before, "取消不该写回设置"
    assert manager.settings.update.branch == "stable"


def test_commit_applies_draft_and_signals(dialog):
    """点「检查更新」才提交（需求 §13），并发信号让主窗口落盘。"""
    dlg, manager = dialog
    dlg.branch_combo.setCurrentIndex(dlg.branch_combo.findData("beta"))
    dlg.channel_combo.setCurrentIndex(dlg.channel_combo.findData("github"))
    dlg.proxy_edit.setText("socks5://127.0.0.1:1080")
    dlg.speed_combo.setCurrentIndex(dlg.speed_combo.findData(20))

    emitted: list[int] = []
    dlg.settings_committed.connect(lambda: emitted.append(1))
    dlg._commit()

    assert emitted == [1]
    assert manager.settings.update.branch == "beta"
    assert manager.settings.update.channel == "github"
    assert manager.settings.update.proxy == "socks5://127.0.0.1:1080"
    assert manager.settings.update.speed_limit == 20


def test_no_update_text_matches_requirement(qapp, tmp_path, monkeypatch):
    """需求 §32 的"无更新"文案。"""
    from mangaproof.ui.update_worker import CheckOutcome
    from mangaproof.update.models import CheckResult, ReleaseInfo
    from mangaproof.update.version import AppVersion

    manager = SettingsManager(tmp_path / "settings.json")
    manager.save()
    dlg = UpdateDialog(manager.settings)
    try:
        current = AppVersion.parse("1.1.0.alpha")
        result = CheckResult(
            current=current,
            release=ReleaseInfo(
                version=AppVersion.parse("v1.0.0"), version_name="v1.0.0"
            ),
            branch="stable",
        )
        dlg._commit()
        dlg._on_check_ok(CheckOutcome(kind="ok", result=result))
        text = dlg.status_label.text()
        assert "当前已经是最新版本" in text
        assert "当前版本：v1.1.0.alpha" in text
        assert "更新分支：stable" in text
        assert dlg.primary_btn.text() == "检查更新"
    finally:
        dlg.close()


def test_update_available_text_matches_requirement(qapp, tmp_path):
    """需求 §33 的"有更新"文案（含文件与大小）。"""
    from mangaproof.ui.update_worker import CheckOutcome
    from mangaproof.update.models import CheckResult, ReleaseInfo
    from mangaproof.update.version import AppVersion

    manager = SettingsManager(tmp_path / "settings.json")
    manager.save()
    dlg = UpdateDialog(manager.settings)
    try:
        result = CheckResult(
            current=AppVersion.parse("1.0.0"),
            release=ReleaseInfo(
                version=AppVersion.parse("v1.1.0"), version_name="v1.1.0",
                release_note="新增更新功能",
            ),
            branch="stable",
        )
        dlg._commit()
        dlg._on_check_ok(
            CheckOutcome(
                kind="ok", result=result,
                filename="MangaProof-1.1.0-linux-x64.tar.gz",
                filesize=99862975,
            )
        )
        text = dlg.status_label.text()
        assert "发现新版本" in text
        assert "当前版本：v1.0.0" in text
        assert "最新版本：v1.1.0" in text
        assert "MangaProof-1.1.0-linux-x64.tar.gz" in text
        assert "95.2 MB" in text
        assert "新增更新功能" in text
        assert dlg.primary_btn.text() == "立即更新", "不自动下载，等用户点"
    finally:
        dlg.close()


def test_download_progress_uses_indeterminate_without_total(qapp, tmp_path):
    """需求 §34：无 Content-Length 时用不确定进度条。"""
    manager = SettingsManager(tmp_path / "settings.json")
    manager.save()
    dlg = UpdateDialog(manager.settings)
    try:
        dlg._on_download_progress(1024 * 1024, None, 2048.0, "正在下载")
        assert dlg.progress.minimum() == 0 and dlg.progress.maximum() == 0
        assert "1.0 MB" in dlg.detail_label.text()
    finally:
        dlg.close()


def test_channel_labels_cover_all_values():
    from mangaproof.config.settings import UPDATE_CHANNELS

    assert set(CHANNEL_LABELS) == set(UPDATE_CHANNELS)
