"""首次使用引导测试：没有配置文件时把设置页面摆到用户面前（不代替决策）。

覆盖：
- 判定：既没有 settings.json 也没有 recent.json 才算首次使用；
- 首次使用 → 启动后直接打开设置页面，并带一句「按自己习惯调整」的说明；
- 不该打扰的情况：已有 settings.json（老用户）、已打开过任务（有 recent.json）；
- 横幅：无配置文件时显示、可关闭（本次会话）、可直达设置；
  用户确认设置（settings.json 落盘）后自动收起；
- 全程不写任何文件：用户点取消/关横幅都不会凭空生成 settings.json。

运行：QT_QPA_PLATFORM=offscreen uv run python -m pytest tests/test_first_run.py -v
     （或直接 uv run python tests/test_first_run.py）
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtWidgets import QApplication

import mangaproof.ui.main_window as mw
from mangaproof.config.settings import Settings, SettingsManager
from mangaproof.ui.main_window import FIRST_RUN_BANNER, FIRST_RUN_INTRO, MainWindow
from mangaproof.ui.settings_dialog import SettingsDialog

app = QApplication.instance() or QApplication([])

ACCEPTED = SettingsDialog.DialogCode.Accepted
REJECTED = SettingsDialog.DialogCode.Rejected


class _DialogSpy:
    """替换 SettingsDialog.exec：记录是否弹过、带没带首次使用说明。

    patch.object 到类属性上的必须是**函数**（才会被绑定成方法、收到 dialog），
    所以这里暴露 exec_fn() 而不是 __call__。
    """

    def __init__(self, result: int = REJECTED):
        self.result = result
        self.calls = 0
        self.intro = ""

    def exec_fn(self):
        def _exec(dialog) -> int:
            self.calls += 1
            self.intro = dialog.intro_label.text() if dialog.intro_label else ""
            return self.result

        return _exec


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_first_use_opens_settings_dialog() -> None:
    """全新环境（无 settings.json / recent.json）：启动后直接打开设置页面。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manager = SettingsManager(root / "settings.json")
        assert manager.was_missing is True
        assert manager.is_first_use is True

        window = MainWindow(manager)
        try:
            # 横幅提示先在（不阻塞用户），设置页面由启动钩子拉起
            assert window.settings_banner.isHidden() is False
            assert FIRST_RUN_BANNER in window.settings_banner_label.text()

            spy = _DialogSpy(REJECTED)
            with patch.object(SettingsDialog, "exec", spy.exec_fn()):
                window.maybe_prompt_first_run_settings()   # main.py 启动钩子调的就是它
            assert spy.calls == 1, "首次使用应直接打开设置页面"
            assert spy.intro == FIRST_RUN_INTRO, spy.intro
            assert "默认" in spy.intro            # 只说明「不改也行」，不推荐具体值

            # 用户点取消：什么都不写，默认值照常生效，横幅留着继续提醒
            assert not (root / "settings.json").exists()
            assert window.settings_banner.isHidden() is False
        finally:
            window.close()          # closeEvent 会保存设置（模拟正常退出）
            app.processEvents()

    print("PASS test_first_use_opens_settings_dialog")


def test_no_prompt_for_existing_settings() -> None:
    """已有 settings.json（老用户）：不弹设置页、不显示横幅。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_json(root / "settings.json", {"layer_display_ratio": 0.4})

        manager = SettingsManager(root / "settings.json")
        assert manager.was_missing is False
        assert manager.is_first_use is False

        window = MainWindow(manager)
        try:
            assert window.settings_banner.isHidden() is True
            spy = _DialogSpy(REJECTED)
            with patch.object(SettingsDialog, "exec", spy.exec_fn()):
                window.maybe_prompt_first_run_settings()
            assert spy.calls == 0, "老用户不该被首次使用引导打扰"
        finally:
            window.close()
            app.processEvents()

    print("PASS test_no_prompt_for_existing_settings")


def test_no_prompt_when_only_recent_exists() -> None:
    """没有 settings.json 但用过（有 recent.json）：不弹设置页，只留横幅提醒。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_json(
            root / "recent.json",
            {"recent_version": 1, "paths": [str(root / "chapter01")]},
        )

        manager = SettingsManager(root / "settings.json")
        assert manager.was_missing is True
        assert manager.is_first_use is False, "开过任务就不算第一次使用"

        window = MainWindow(manager)
        try:
            spy = _DialogSpy(REJECTED)
            with patch.object(SettingsDialog, "exec", spy.exec_fn()):
                window.maybe_prompt_first_run_settings()
            assert spy.calls == 0
            # 但设置还没确认过 → 横幅仍然提示入口（不打断操作）
            assert window.settings_banner.isHidden() is False
        finally:
            window.close()
            app.processEvents()

    print("PASS test_no_prompt_when_only_recent_exists")


def test_banner_open_settings_and_dismiss() -> None:
    """横幅两个按钮：打开设置并确认后自动收起；✕ 只本次隐藏且不写文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        settings_file = root / "settings.json"

        window = MainWindow(SettingsManager(settings_file))
        try:
            assert window.settings_banner.isHidden() is False

            # 1) ✕：本次会话不再提示，且绝不凭空生成配置文件
            window.settings_banner_close.click()
            assert window.settings_banner.isHidden() is True
            assert not settings_file.exists()

            # 2) 重新给一次机会（新窗口 = 新会话）
            window2 = MainWindow(SettingsManager(settings_file))
            assert window2.settings_banner.isHidden() is False
            try:
                spy = _DialogSpy(ACCEPTED)
                with patch.object(SettingsDialog, "exec", spy.exec_fn()):
                    window2.settings_banner_button.click()
                assert spy.calls == 1
                assert spy.intro == FIRST_RUN_INTRO, spy.intro
                # 用户点了确定 → 配置落盘 → 横幅永久收起
                assert settings_file.exists()
                assert window2.settings_banner.isHidden() is True
                assert json.loads(settings_file.read_text(encoding="utf-8"))[
                    "settings_version"
                ] == 1
            finally:
                window2.close()
                app.processEvents()
        finally:
            window.close()
            app.processEvents()

    print("PASS test_banner_open_settings_and_dismiss")


def test_normal_exit_counts_as_configured() -> None:
    """正常退出会把默认设置落盘 → 下次启动不再提示（不反复打扰）。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        settings_file = root / "settings.json"

        window = MainWindow(SettingsManager(settings_file))
        assert window.settings_banner.isHidden() is False
        window.close()                      # 正常退出：settings.json 落盘
        app.processEvents()
        assert settings_file.exists()

        manager = SettingsManager(settings_file)
        assert manager.was_missing is False
        window2 = MainWindow(manager)
        try:
            assert window2.settings_banner.isHidden() is True
            spy = _DialogSpy(REJECTED)
            with patch.object(SettingsDialog, "exec", spy.exec_fn()):
                window2.maybe_prompt_first_run_settings()
            assert spy.calls == 0
        finally:
            window2.close()
            app.processEvents()

    print("PASS test_normal_exit_counts_as_configured")


def test_intro_only_when_requested() -> None:
    """普通打开设置（非首次使用）不带引导说明——不打扰老用户。"""
    plain = SettingsDialog(Settings())
    try:
        assert plain.intro_label is None
    finally:
        plain.close()
    guided = SettingsDialog(Settings(), None, intro=FIRST_RUN_INTRO)
    try:
        assert guided.intro_label is not None
        assert guided.intro_label.text() == FIRST_RUN_INTRO
        assert guided.intro_label.wordWrap() is True
    finally:
        guided.close()

    print("PASS test_intro_only_when_requested")


def test_banner_refreshes_after_settings_change() -> None:
    """任一保存设置的入口都会刷新横幅状态（改显示比例也生效）。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        settings_file = root / "settings.json"
        window = MainWindow(SettingsManager(settings_file))
        try:
            assert window.settings_banner.isHidden() is False
            idx = window.ratio_combo.findData(0.4)
            window.ratio_combo.setCurrentIndex(idx)      # 触发 _on_ratio_changed → 保存
            app.processEvents()
            assert settings_file.exists()
            assert window.settings_banner.isHidden() is True
        finally:
            window.close()
            app.processEvents()

    print("PASS test_banner_refreshes_after_settings_change")


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
