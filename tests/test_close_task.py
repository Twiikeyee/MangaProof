"""关闭当前任务（Ctrl+W /「文件 → 关闭当前任务」）测试。

覆盖：
- 关闭后窗口完整回到「未打开」状态（任务、文档缓存、画布、面板、按钮、标题）；
- 「先保存再关闭」：自动保存有 1.5s 防抖，标完立刻关闭也不丢改动；
- 保存失败（磁盘满 / 只读 / 权限）时中止关闭并说明原因，不静默丢进度；
- 关闭不清除「最近打开」记录，重新打开同一文件夹能接着上次的进度；
- 快捷键与菜单/工具栏接入：Ctrl+W 默认绑定、无冲突、无任务时是禁用状态。

运行：QT_QPA_PLATFORM=offscreen uv run python -m pytest tests/test_close_task.py -v
     （或直接 uv run python tests/test_close_task.py）
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from mangaproof import APP_NAME, __version__
from mangaproof.config.settings import (
    CORE_SHORTCUT_LABELS,
    DEFAULT_ISSUE_TYPES,
    DEFAULT_KEYBINDINGS,
    SettingsManager,
    shortcut_conflicts,
)
from mangaproof.review import persistence
from mangaproof.review.state import PASSED
from mangaproof.ui.main_window import MainWindow
from mangaproof.ui.viewer_widget import SOURCE_MERGED

DATA_DIR = Path(__file__).parent / "data" / "chapter01"

app = QApplication.instance() or QApplication([])


def _copy_fixtures(dst: Path) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for p in sorted(DATA_DIR.glob("*.psd")):
        shutil.copy2(p, dst / p.name)
    return dst


def _wait_for_task(window: MainWindow, timeout_s: float = 60.0) -> None:
    deadline = time.time() + timeout_s
    while (window.task is None or window._current_file == "") and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    assert window.task is not None, "任务加载超时（后台 worker 未完成）"
    assert window._current_file != "", "首文件异步打开超时"


def _open_fixture_task(root: Path, window: MainWindow):
    """打开夹具文件夹并返回 (folder, window)。"""
    folder = _copy_fixtures(root / "chapter01")
    window.resize(1000, 700)
    window.show()
    app.processEvents()
    window.open_folder(folder)
    _wait_for_task(window)
    window.activateWindow()      # 快捷键只在窗口激活时触发
    app.processEvents()
    return folder


# ==================================================================== 状态复位


def test_close_task_resets_to_empty_state() -> None:
    """关闭后窗口回到未打开状态：任务/缓存/画布/面板/按钮/标题全部复位。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        window = MainWindow(SettingsManager(root / "settings.json"))
        folder = _open_fixture_task(root, window)
        try:
            # 关闭前：确实处于「有任务 + 正在标注 + 正在自动对比」的状态
            assert window.action_close.isEnabled()
            assert window.file_panel.list_widget.count() >= 3
            window.toggle_compare()
            assert window._compare.is_running is True
            window.toggle_redraw_mode()
            assert window.viewer.redraw_mode is True
            assert window.stats_panel.psd_name_label.text() != "当前 PSD：-"

            window.close_task()
            app.processEvents()

            # 任务与文档
            assert window.task is None
            assert window._base_dir is None
            assert window._current_file == ""
            assert window._current_index == -1
            assert window._docs == {}
            assert window._layer_ids_by_file == {}
            assert window._layer_names_by_file == {}
            assert len(window._layer_cache) == 0, "图层像素缓存未释放"
            assert window._pending_qimages == {}
            assert window._preload_targets == set()
            assert window._extra_targets == set()

            # 画布：无文档、原图、非拖框、无虚线框、相机复位
            assert window.viewer.document is None
            assert window.viewer.source == SOURCE_MERGED
            assert window.viewer.redraw_mode is False
            assert window.viewer.pending_type is None
            assert window.viewer.layer_outline is None
            assert window.viewer.camera.zoom == 1.0
            assert window.zoom_label.text() == "缩放：100%"

            # 面板
            assert window.file_panel.list_widget.count() == 0
            assert window.file_panel.title_label.text() == "PSD 文件"
            assert window.layer_panel.list_widget.count() == 0
            assert window.stats_panel.psd_name_label.text() == "当前 PSD：-"
            assert "0 / 0" in window.stats_panel.psd_cells["reviewed"].text()
            assert window.stats_panel.progress_bar.value() == 0
            assert window.stats_panel._chips == []
            assert window.issue_panel.layer_name_label.text() == "图层：-"
            assert window.issue_panel.status_label.text() == "状态：○ 未监制"
            assert window.issue_panel.issue_list.count() == 0
            assert window.issue_panel.pass_btn.isEnabled() is False

            # 标题 / 状态栏 / 按钮状态
            assert window.windowTitle() == f"{APP_NAME} v{__version__}"
            assert window.save_label.text() == "未打开任务"
            assert window.preload_label.text() == ""
            assert window.warmup_label.text() == ""
            assert window.action_close.isEnabled() is False
            assert window.action_save.isEnabled() is False
            assert window.action_report.isEnabled() is False
            assert window.ratio_combo.isEnabled() is False
            assert window._autosave_timer.isActive() is False

            # 关闭 ≠ 删除记录：任务文件还在，「最近打开」仍留着这个文件夹
            assert persistence.progress_path_for_folder(folder).exists()
            assert str(folder.resolve()) in window.recent_manager.paths

            # 空画布能正常绘制，并给出下一步指引
            assert "未打开任务" in window.viewer.empty_hint
            assert window.viewer.grab().isNull() is False
        finally:
            window.close()
            app.processEvents()

    print("PASS test_close_task_resets_to_empty_state")


def test_close_task_noop_without_task() -> None:
    """没打开任务时关闭：静默无操作，不弹框、不报错。"""
    with tempfile.TemporaryDirectory() as tmp:
        window = MainWindow(SettingsManager(Path(tmp) / "settings.json"))
        try:
            with patch.object(QMessageBox, "warning") as warn:
                window.close_task()
            assert warn.call_count == 0
            assert window.task is None
        finally:
            window.close()
            app.processEvents()

    print("PASS test_close_task_noop_without_task")


# ============================================================ 先保存再关闭 / 中止


def test_close_task_flushes_pending_save() -> None:
    """标完立刻关闭（防抖窗口内）也不丢改动；重开同一文件夹能接着上次。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        window = MainWindow(SettingsManager(root / "settings.json"))
        folder = _open_fixture_task(root, window)
        try:
            rel = window._current_file
            lid = window._layer_ids_by_file[rel][0]
            window.task.set_status(rel, lid, PASSED)
            window._refresh_all_panels()
            window._mark_dirty()             # 起 1.5s 防抖计时（此时还没落盘）
            assert window.save_label.text() == "未保存"

            window.close_task()              # 不等防抖，直接关闭
            app.processEvents()

            assert window._autosave_timer.isActive() is False
            assert window.save_label.text() == "未打开任务"
            progress = persistence.progress_path_for_folder(folder)
            saved = persistence.load_task(progress)
            assert saved.status_of(rel, lid) == PASSED, "关闭前未落盘，改动丢失"

            # 重新打开：进度原样恢复（关闭只是收起任务，不是丢弃）
            with patch.object(MainWindow, "_strong_rebind_warning", return_value=True):
                window.open_folder(folder)
            _wait_for_task(window)
            assert window.task.status_of(rel, lid) == PASSED
        finally:
            window.close()
            app.processEvents()

    print("PASS test_close_task_flushes_pending_save")


def test_close_task_aborts_when_save_fails() -> None:
    """保存失败（磁盘满 / 只读 / 权限）→ 中止关闭，保留现场并说明原因。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        window = MainWindow(SettingsManager(root / "settings.json"))
        _open_fixture_task(root, window)
        try:
            import mangaproof.ui.main_window as mw

            with patch.object(
                mw.persistence, "save_task", side_effect=OSError("磁盘空间不足")
            ), patch.object(
                QMessageBox, "warning", return_value=QMessageBox.StandardButton.Ok
            ) as warn:
                window.close_task()
            app.processEvents()

            assert window.task is not None, "保存失败时不应关闭任务"
            assert window._base_dir is not None
            assert window.viewer.document is not None
            assert warn.call_count == 1, warn.call_args_list
            assert "保存失败" in warn.call_args[0][2]
            assert "磁盘空间不足" in warn.call_args[0][2]
            assert window.save_label.text().startswith("保存失败")

            # 故障排除后再关：正常关闭
            window.close_task()
            assert window.task is None
        finally:
            window.close()
            app.processEvents()

    print("PASS test_close_task_aborts_when_save_fails")


# ================================================================ 入口与快捷键


def test_close_task_entry_points_and_shortcut() -> None:
    """菜单/工具栏接入 + Ctrl+W 默认绑定、无冲突、随任务有无切换禁用状态。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "settings.json"
        assert DEFAULT_KEYBINDINGS["close_task"] == "Ctrl+W"
        assert CORE_SHORTCUT_LABELS["close_task"] == "关闭当前任务"
        assert shortcut_conflicts(DEFAULT_KEYBINDINGS, DEFAULT_ISSUE_TYPES) == {}

        window = MainWindow(SettingsManager(path))
        window.show()
        window.activateWindow()
        app.processEvents()
        try:
            # 未打开任务：按钮/菜单项禁用
            assert window.action_close.isEnabled() is False
            assert "Ctrl+W" in window.action_close.text(), window.action_close.text()
            assert "Ctrl+W 关闭" in window.hint_label.text(), window.hint_label.text()
            # 工具栏与文件菜单都挂上了这个动作
            assert window.action_close in window.findChildren(type(window.action_close))
            file_menu = window.menuBar().actions()[0].menu()
            assert window.action_close in file_menu.actions()

            # 无任务时按 Ctrl+W：静默无操作（不弹框、不崩）
            with patch.object(QMessageBox, "warning") as warn:
                QTest.keyClick(
                    window, QKeySequence("Ctrl+W")[0].key(),
                    QKeySequence("Ctrl+W")[0].keyboardModifiers(),
                )
                app.processEvents()
            assert warn.call_count == 0
            assert window.task is None

            # 打开任务后：可用 → 按 Ctrl+W 关闭
            _open_fixture_task(root, window)
            assert window.action_close.isEnabled() is True
            assert window.settings_manager.settings.binding("close_task") == "Ctrl+W"

            QTest.keyClick(
                window, QKeySequence("Ctrl+W")[0].key(),
                QKeySequence("Ctrl+W")[0].keyboardModifiers(),
            )
            app.processEvents()
            assert window.task is None, "Ctrl+W 应关闭当前任务"
            assert window.action_close.isEnabled() is False

            # 绑定会随设置落盘（可在 设置 →「设置快捷键…」里重绑定）
            window.settings_manager.save()
            import json
            raw = json.loads(path.read_text(encoding="utf-8"))
            assert raw["keybindings"]["close_task"] == "Ctrl+W"

            # 改绑打开类快捷键后，空画布提示跟着更新（不写死默认键）
            window.settings.keybindings["open_folder"] = "Ctrl+Alt+O"
            window._rebuild_shortcuts()
            assert "Ctrl+Alt+O" in window.viewer.empty_hint, window.viewer.empty_hint
        finally:
            window.close()
            app.processEvents()

    print("PASS test_close_task_entry_points_and_shortcut")


if __name__ == "__main__":
    failed = 0
    for name, fn in [
        (k, v) for k, v in sorted(globals().items()) if k.startswith("test_")
    ]:
        try:
            fn()
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            import traceback

            traceback.print_exc()
    sys.exit(1 if failed else 0)
