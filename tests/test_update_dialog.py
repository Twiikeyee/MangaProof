# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""更新页面的单测（需求 §11、§13、§31~§34）。

跑在离屏 Qt 上，不联网、不真的启动 worker（只验界面结构与"保存时机"语义）。
需要 `qapp` fixture（见 tests/test_android_ui_scale.py 的同名 fixture 定义）。

另有三条**布局**回归（都是实测踩到过的坑）：
- 下拉框不响应滚轮（与设置页一致）；
- 「代理」行的输入框左右边界、测试按钮右边界与其他行严格对齐；
- 进入「检查中」时进度条与状态文案的变化不许压扁表单行高。
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


# -- 布局回归 ---------------------------------------------------------------


def _form_layout(dialog):
    """取出对话框里的 QFormLayout（表单区）。"""
    from PySide6.QtWidgets import QFormLayout

    for i in range(dialog.layout().count()):
        item = dialog.layout().itemAt(i)
        if isinstance(item, QFormLayout):
            return item
    raise AssertionError("更新页面里找不到表单布局")


def _laid_out(dialog):
    """让对话框真正走一遍布局（不 show 的话各控件几何值都是 0）。"""
    from PySide6.QtWidgets import QApplication

    dialog.show()
    for _ in range(2):
        QApplication.processEvents()
    return dialog


def _box(dialog, widget):
    """控件在对话框坐标系里的 ``(左, 右)``（代理行嵌在复合控件里，必须换算）。"""
    top_left = widget.mapTo(dialog, widget.rect().topLeft())
    return top_left.x(), top_left.x() + widget.width()


def test_combos_ignore_wheel(dialog):
    """下拉框不许被滚轮改值（与设置页同款 NoWheelComboBox）。

    Fusion 风格默认允许滚轮直接改下拉框的值，误滚改掉分支/渠道后很难察觉，
    而且这里改的还是"检查更新用哪个分支"。
    """
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QWheelEvent

    from mangaproof.ui.widgets import NoWheelComboBox

    dlg, _ = dialog
    for combo in (dlg.branch_combo, dlg.channel_combo, dlg.speed_combo):
        assert isinstance(combo, NoWheelComboBox)
        before = combo.currentIndex()
        event = QWheelEvent(
            QPoint(5, 5), combo.mapToGlobal(QPoint(5, 5)),
            QPoint(0, -120), QPoint(0, -120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )
        combo.wheelEvent(event)
        assert combo.currentIndex() == before, "滚轮不该改变下拉框选中项"


def test_proxy_row_aligns_with_other_rows(dialog):
    """「代理」行两端与其他输入行严格对齐（曾经左缩 9px、右缩 9px）。

    复合控件（QWidget 包 QHBoxLayout）自带内容边距，且默认按 sizeHint 摆放，
    所以必须清零边距 + Expanding，才能贴齐字段列的两端。
    """
    dlg, _ = dialog
    _laid_out(dlg)

    proxy_left, proxy_right = _box(dlg, dlg.proxy_edit)
    cdk_left, cdk_right = _box(dlg, dlg.cdk_edit)
    btn_left, btn_right = _box(dlg, dlg.proxy_test_btn)
    branch_left, _ = _box(dlg, dlg.branch_combo)

    assert proxy_left == cdk_left, "代理输入框左边界要与 CDK 一致"
    assert proxy_left == branch_left, "代理输入框左边界要与下拉框一致"
    assert btn_right == cdk_right, "测试代理按钮右边界要与其他输入框一致"
    assert proxy_right < btn_left, "输入框与按钮不能重叠"

    form = _form_layout(dlg)
    # 「代理」两个字与 CDK 标签同为右对齐，右边界必须齐平
    proxy_label = form.labelForField(dlg.proxy_edit.parentWidget())
    cdk_label = form.labelForField(dlg.cdk_edit)
    assert proxy_label is not None and cdk_label is not None
    assert _box(dlg, proxy_label)[1] == _box(dlg, cdk_label)[1]


def test_progress_bar_does_not_squeeze_form(dialog):
    """进入「检查中」时不许压扁表单（曾经「代理」行被压到 12px）。

    Qt 在"窗口大小不变"的前提下重排布局，空间不够时 QFormLayout 会自己压缩
    行高。修法：进度条常驻占位（空闲禁用置灰）+ 状态区固定高度 + 最小高度一次
    算准，于是状态切换只改内容，不会重新抢高度。
    """
    dlg, _ = dialog
    _laid_out(dlg)
    heights_before = [dlg.proxy_edit.height(), dlg.cdk_edit.height()]
    dialog_height_before = dlg.height()

    dlg.status_label.setText("正在检查更新……")
    dlg.progress.setRange(0, 0)          # 需求 §31：不确定进度条
    dlg._show_progress()
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()

    assert dlg.progress.isVisible(), "进度条占位常驻（空闲只是禁用置灰）"
    assert [dlg.proxy_edit.height(), dlg.cdk_edit.height()] == heights_before, \
        "代理行被压扁了"
    assert dlg.height() == dialog_height_before, "窗口高度不该被内容变化改掉"

    dlg._reset_progress()
    assert not dlg.progress.isEnabled(), "空闲时进度条置灰"
    assert [dlg.proxy_edit.height(), dlg.cdk_edit.height()] == heights_before


def test_output_area_scrolls_instead_of_growing(qapp, tmp_path):
    """输出区（对话框下半）文案再长也只滚动，不撑大窗口、不裁掉内容。

    更新说明是外部文本（长度不可控），必须能滚：曾经这里没有滚动容器，
    长说明直接把窗口顶着长；后来换成滚动容器但留了个 addStretch，
    弹性空间顶掉滚动条，长文案就只剩裁掉、滚不动。
    """
    from PySide6.QtWidgets import QApplication

    manager = SettingsManager(tmp_path / "settings.json")
    manager.save()
    dlg = UpdateDialog(manager.settings)
    rows = (dlg.branch_combo, dlg.channel_combo, dlg.cdk_edit,
            dlg.proxy_edit, dlg.speed_combo)
    try:
        _laid_out(dlg)
        height_before = dlg.height()
        area_before = dlg.status_area.height()
        rows_before = [w.height() for w in rows]

        dlg.status_label.setText(
            "发现新版本\n\n"
            + "\n".join(f"{i}. 第 {i} 条更新说明，故意写得很长很长。" for i in range(200))
        )
        for _ in range(3):
            QApplication.processEvents()

        scrollbar = dlg.status_area.verticalScrollBar()
        assert scrollbar.maximum() > 0, "长文案必须产生可滚动范围（有内容被裁掉）"
        assert scrollbar.pageStep() == dlg.status_area.viewport().height()
        assert dlg.height() == height_before, "输出区不许把窗口撑大"
        assert dlg.status_area.height() == area_before, "输出区高度固定"
        # 上面各行的行高不许因为输出变长而改变（逐行与原值比对：
        # 行与行之间本来就有 1px 取整差异，不能拿两行互相比）
        assert [w.height() for w in rows] == rows_before, "表单行高被输出区挤掉了"

        # 用户滚到底，再开始新动作 → 视口回到顶部，且旧内容被清空
        scrollbar.setValue(scrollbar.maximum())
        dlg._clear_output()
        QApplication.processEvents()
        assert scrollbar.value() == 0, "清空输出后视口要回到顶部"
        assert dlg.output_text() == ""
    finally:
        dlg.close()


def test_new_action_clears_previous_output(qapp, tmp_path):
    """开始新动作时清空上一轮输出（否则分不清哪条是本次结论）。"""
    from PySide6.QtWidgets import QApplication

    from mangaproof.ui.update_worker import CheckOutcome
    from mangaproof.update.models import CheckResult, ReleaseInfo
    from mangaproof.update.version import AppVersion

    manager = SettingsManager(tmp_path / "settings.json")
    manager.save()
    dlg = UpdateDialog(manager.settings)
    try:
        _laid_out(dlg)
        result = CheckResult(
            current=AppVersion.parse("1.0.0"),
            release=ReleaseInfo(
                version=AppVersion.parse("v1.1.0"), version_name="v1.1.0",
                release_note="新增更新功能",
            ),
            branch="stable",
        )
        dlg._on_check_ok(
            CheckOutcome(kind="ok", result=result, filename="pkg.tar.gz", filesize=1024)
        )
        assert "发现新版本" in dlg.output_text()
        assert dlg.cancel_btn.text() == "取消"

        # 点「立即更新」：旧结论先被清掉，再写本轮开头文案
        dlg._result = result
        dlg._clear_output()
        dlg.status_label.setText("正在准备下载……")
        QApplication.processEvents()
        text = dlg.output_text()
        assert "发现新版本" not in text
        assert "正在准备下载……" in text

        # 下载完成后「取消」变「稍后」；状态与文案也要能被下一轮整体清掉
        dlg.cancel_btn.setText("稍后")
        dlg._clear_output()
        QApplication.processEvents()
        assert dlg.output_text() == ""
        assert dlg.status_area.verticalScrollBar().value() == 0
    finally:
        dlg.close()


def test_proxy_result_goes_to_output_and_clears_it(qapp, tmp_path):
    """测试代理的结果写进输出区，且新一次测试会清掉上一次的结果。"""
    from PySide6.QtWidgets import QApplication

    manager = SettingsManager(tmp_path / "settings.json")
    manager.save()
    dlg = UpdateDialog(manager.settings)
    try:
        _laid_out(dlg)
        dlg._on_proxy_result(True, "代理可用（耗时 123 ms）")
        QApplication.processEvents()
        assert "代理可用" in dlg.output_text()

        dlg._on_proxy_result(False, "连接被拒绝")
        QApplication.processEvents()
        text = dlg.output_text()
        assert "连接被拒绝" in text
        assert "代理可用" not in text, "上一次的结果要被替换掉"
    finally:
        dlg.close()
