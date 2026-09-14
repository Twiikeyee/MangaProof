"""最近打开记录测试：独立 recent.json 存储 + 菜单行为。

覆盖：
- RecentManager 的增删/去重/上限/持久化/损坏容错；
- settings.json 旧键（recent_paths）一次性迁移，且不再写回；
- 设置文件损坏/重置不影响最近打开记录（两文件分离的意义）；
- 主窗口「最近打开」菜单：启动即填充、失效项点掉即移除、清空记录、
  打开任务后自动记录。

运行：QT_QPA_PLATFORM=offscreen uv run python -m pytest tests/test_recent.py -v
     （或直接 uv run python tests/test_recent.py）
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtWidgets import QApplication, QMessageBox

from mangaproof.config.recent import MAX_RECENT, RecentManager, normalize
from mangaproof.config.settings import SettingsManager
from mangaproof.ui.main_window import MainWindow

DATA_DIR = Path(__file__).parent / "data" / "chapter01"

app = QApplication.instance() or QApplication([])


def _write_settings(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _copy_fixtures(dst: Path) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for p in sorted(DATA_DIR.glob("*.psd")):
        shutil.copy2(p, dst / p.name)
    return dst


def _wait_for_task(window: MainWindow, timeout_s: float = 60.0) -> None:
    """打开为后台异步流程：轮询事件循环直到任务绑定且首文件打开完成。"""
    deadline = time.time() + timeout_s
    while (window.task is None or window._current_file == "") and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    assert window.task is not None, "任务加载超时（后台 worker 未完成）"
    assert window._current_file != "", "首文件异步打开超时"


def _menu_items(window: MainWindow) -> list[tuple[str, bool]]:
    """菜单项 (文本, 是否可用)；分隔符为空文本。"""
    return [(a.text(), a.isEnabled()) for a in window.recent_menu.actions()]


# =============================================================== RecentManager


def test_recent_add_dedupe_and_cap() -> None:
    """归一化去重、最近在前、最多保留 MAX_RECENT 条。"""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "chapter01"
        folder.mkdir()
        manager = RecentManager(Path(tmp) / "recent.json")

        first = str(folder)
        manager.add(first)
        # 同一文件夹的不同写法（尾斜杠 / 中间 ./）不应产生多条
        manager.add(str(folder) + "/")
        manager.add(str(folder / "." / ""))
        assert manager.paths == [str(folder.resolve())], manager.paths

        # 重新打开旧记录 → 提到最前（而不是新增一条）
        others = [str(Path(tmp) / f"ch{i:02d}") for i in range(1, 4)]
        for p in others:
            manager.add(p)
        manager.add(first)
        assert manager.paths[0] == str(folder.resolve())
        assert len(manager.paths) == 4

        # 超出上限：丢弃最旧的尾部
        for i in range(MAX_RECENT + 5):
            manager.add(str(Path(tmp) / f"many{i:02d}"))
        assert len(manager.paths) == MAX_RECENT
        assert manager.paths[0] == str((Path(tmp) / f"many{MAX_RECENT + 4}").resolve())
        assert str(folder.resolve()) not in manager.paths

        assert normalize(str(folder)) == str(folder.resolve())

    print("PASS test_recent_add_dedupe_and_cap")


def test_recent_persist_reload_and_remove() -> None:
    """落盘 → 重新加载一致；remove / clear 立即生效并写回文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "recent.json"
        a, b = str(Path(tmp) / "a"), str(Path(tmp) / "b")
        manager = RecentManager(path)
        manager.add(a)
        manager.add(b)

        raw = _read_json(path)
        assert raw["recent_version"] == 1
        assert raw["paths"] == [str(Path(b).resolve()), str(Path(a).resolve())]
        assert not path.with_suffix(".json.tmp").exists(), "临时文件未清理"

        reloaded = RecentManager(path)
        assert reloaded.paths == manager.paths

        assert reloaded.remove(b) is True
        assert reloaded.remove(b) is False          # 删不存在的 → False，不写空
        assert reloaded.paths == [str(Path(a).resolve())]
        assert _read_json(path)["paths"] == [str(Path(a).resolve())]

        reloaded.clear()
        assert reloaded.paths == []
        assert RecentManager(path).paths == []

    print("PASS test_recent_persist_reload_and_remove")


def test_recent_corrupt_file_recovers() -> None:
    """recent.json 损坏：不崩、按空处理，并能由旧记录重建后继续写入。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "recent.json"
        legacy = [str(Path(tmp) / "old_chapter")]
        path.write_text("{ 这不是 JSON", encoding="utf-8")

        manager = RecentManager(path, legacy_paths=legacy)
        assert manager.paths == [str(Path(legacy[0]).resolve())], manager.paths
        manager.add(str(Path(tmp) / "new_chapter"))
        assert len(_read_json(path)["paths"]) == 2      # 文件已被修复可用

        path.write_text('["裸数组不被接受"]', encoding="utf-8")
        assert RecentManager(path).paths == []

    print("PASS test_recent_corrupt_file_recovers")


def test_recent_migrates_legacy_settings_key() -> None:
    """旧版混在 settings.json 的 recent_paths：迁移到 recent.json 且不再写回。"""
    with tempfile.TemporaryDirectory() as tmp:
        settings_file = Path(tmp) / "settings.json"
        legacy = [str(Path(tmp) / "chapter01"), str(Path(tmp) / "single.psd"),
                  str(Path(tmp) / "chapter01")]          # 含重复项
        _write_settings(settings_file, {"recent_paths": legacy, "layer_display_ratio": 0.4})

        manager = SettingsManager(settings_file)
        assert manager.recent_path == Path(tmp) / "recent.json"   # 与设置同目录、独立文件
        assert manager.legacy_recent_paths == legacy

        recent = RecentManager(manager.recent_path, legacy_paths=manager.legacy_recent_paths)
        assert recent.paths == [str((Path(tmp) / "chapter01").resolve()),
                               str((Path(tmp) / "single.psd").resolve())]
        assert recent.file_path.exists()

        manager.save()
        saved = _read_json(settings_file)
        assert "recent_paths" not in saved, saved
        assert saved["layer_display_ratio"] == 0.4
        # 迁移只在首次：settings.json 再被重写也不会影响 recent.json
        assert RecentManager(manager.recent_path).paths == recent.paths

    print("PASS test_recent_migrates_legacy_settings_key")


def test_recent_survives_settings_loss() -> None:
    """设置文件损坏/重建不清空最近打开记录（独立文件的初衷）。"""
    with tempfile.TemporaryDirectory() as tmp:
        settings_file = Path(tmp) / "settings.json"
        manager = RecentManager(Path(tmp) / "recent.json")
        manager.add(str(Path(tmp) / "chapter01"))

        settings_file.write_text("坏掉的设置", encoding="utf-8")
        broken = SettingsManager(settings_file)      # 回落默认设置，不抛异常
        assert broken.legacy_recent_paths == []
        assert RecentManager(broken.recent_path).paths == manager.paths

        broken.save()                                 # 重建 settings.json
        assert RecentManager(broken.recent_path).paths == manager.paths

    print("PASS test_recent_survives_settings_loss")


# ==================================================================== GUI 菜单


def test_recent_menu_populated_on_startup() -> None:
    """启动即填充菜单（旧版要本次会话打开过一次任务才出现记录）。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = root / "chapter01"
        folder.mkdir()
        psd = root / "001.psd"
        psd.write_bytes(b"fake")
        dead = root / "gone"

        settings_file = root / "settings.json"
        _write_settings(settings_file, {"recent_paths": [str(folder), str(psd), str(dead)]})

        window = MainWindow(SettingsManager(settings_file))
        try:
            items = _menu_items(window)
            assert items[0] == (str(folder.resolve()), True), items
            assert items[1] == (str(psd.resolve()), True), items
            assert items[2] == (str(dead.resolve()), True), items   # 离线路径仍保留
            assert items[3][0] == ""                                 # 分隔符
            assert items[4] == ("清除最近打开记录", True), items
            assert (root / "recent.json").exists()
        finally:
            window.close()
            app.processEvents()

    print("PASS test_recent_menu_populated_on_startup")


def test_recent_menu_empty_state() -> None:
    """无记录：显示不可点的「（无记录）」，且不出现「清除」项。"""
    with tempfile.TemporaryDirectory() as tmp:
        window = MainWindow(SettingsManager(Path(tmp) / "settings.json"))
        try:
            assert _menu_items(window) == [("（无记录）", False)], _menu_items(window)
            assert not (Path(tmp) / "recent.json").exists()   # 没记录就不落文件
        finally:
            window.close()
            app.processEvents()

    print("PASS test_recent_menu_empty_state")


def test_recent_menu_dead_entry_removed_on_click() -> None:
    """点到失效路径：提示并把它从记录里移掉（不再每点一次弹一次）。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        alive = root / "chapter01"
        alive.mkdir()
        dead = root / "gone"
        _write_settings(
            root / "settings.json",
            {"recent_paths": [str(dead), str(alive)]},
        )

        window = MainWindow(SettingsManager(root / "settings.json"))
        try:
            with patch.object(
                QMessageBox, "warning", return_value=QMessageBox.StandardButton.Ok
            ) as warn:
                window.recent_menu.actions()[0].trigger()
            assert warn.call_count == 1, warn.call_args_list
            assert "已不存在" in warn.call_args[0][2]

            assert window.recent_manager.paths == [str(alive.resolve())]
            assert _read_json(root / "recent.json")["paths"] == [str(alive.resolve())]
            assert _menu_items(window)[0] == (str(alive.resolve()), True)
            assert str(dead.resolve()) not in [text for text, _ in _menu_items(window)]
        finally:
            window.close()
            app.processEvents()

    print("PASS test_recent_menu_dead_entry_removed_on_click")


def test_recent_menu_clear_action() -> None:
    """「清除最近打开记录」清空文件并回到空菜单状态。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = root / "chapter01"
        folder.mkdir()
        _write_settings(root / "settings.json", {"recent_paths": [str(folder)]})

        window = MainWindow(SettingsManager(root / "settings.json"))
        try:
            clear_action = window.recent_menu.actions()[-1]
            assert clear_action.text() == "清除最近打开记录"
            clear_action.trigger()
            assert window.recent_manager.paths == []
            assert _read_json(root / "recent.json")["paths"] == []
            assert _menu_items(window) == [("（无记录）", False)], _menu_items(window)
        finally:
            window.close()
            app.processEvents()

    print("PASS test_recent_menu_clear_action")


def test_open_folder_records_recent() -> None:
    """真正打开任务后自动记入 recent.json，并出现在菜单最前。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = _copy_fixtures(root / "chapter01")
        window = MainWindow(SettingsManager(root / "settings.json"))
        window.resize(1000, 700)
        window.show()
        app.processEvents()
        try:
            window.open_folder(folder)
            _wait_for_task(window)

            expected = str(folder.resolve())
            assert window.recent_manager.paths[0] == expected
            assert _read_json(root / "recent.json")["paths"][0] == expected
            assert _menu_items(window)[0] == (expected, True)
            assert window.settings_manager.legacy_recent_paths == []   # 不再写进设置
        finally:
            window.close()
            app.processEvents()

    print("PASS test_open_folder_records_recent")


def test_recent_menu_reuses_action_parent() -> None:
    """重建菜单不留孤儿 action（旧实现挂在窗口上，clear() 回收不掉）。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_settings(
            root / "settings.json",
            {"recent_paths": [str(root / f"ch{i}") for i in range(5)]},
        )
        window = MainWindow(SettingsManager(root / "settings.json"))
        try:
            before = len(window.findChildren(type(window.recent_menu.actions()[0])))
            for _ in range(5):
                window._rebuild_recent_menu()
            after = len(window.findChildren(type(window.recent_menu.actions()[0])))
            assert after == before, (before, after)
        finally:
            window.close()
            app.processEvents()

    print("PASS test_recent_menu_reuses_action_parent")


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
