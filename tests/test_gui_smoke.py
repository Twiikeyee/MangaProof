"""GUI 冒烟测试（QT_QPA_PLATFORM=offscreen，无需显示器）。

覆盖：打开文件夹 → 自动恢复 → Enter// 状态流转 → 问题红框 →
←→↑↓ 导航 → Space 自动对比 → 自动保存 → 重启恢复 → 返修单生成。

运行：QT_QPA_PLATFORM=offscreen uv run python tests/test_gui_smoke.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QWidget,
)

import mangaproof.ui.main_window as mw
from mangaproof.config.settings import SettingsManager
from mangaproof.review import persistence
from mangaproof.review.state import FAILED, PARTIAL, PASSED, UNREVIEWED, TaskState
from mangaproof.ui.dialogs import IssueDialog
from mangaproof.ui.main_window import MainWindow
from mangaproof.ui.task_loader import TaskLoadWorker
from mangaproof.ui.theme import COLOR_FAIL, COLOR_PASS, COLOR_UNREVIEWED, COLOR_WARN

DATA_DIR = Path(__file__).parent / "data" / "chapter01"

app = QApplication.instance() or QApplication([])


def _copy_fixtures(dst: Path) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for p in sorted(DATA_DIR.glob("*.psd")):
        shutil.copy2(p, dst / p.name)
    return dst


def _wait_for_task(window: MainWindow, timeout_s: float = 30.0) -> None:
    """打开为后台异步流程：轮询事件循环直到任务绑定且首文件打开完成。"""
    deadline = time.time() + timeout_s
    while (window.task is None or window._current_file == "") and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    assert window.task is not None, "任务加载超时（后台 worker 未完成）"
    assert window._current_file != "", "首文件异步打开超时"


def _wait_for_file(window: MainWindow, rel: str, timeout_s: float = 30.0) -> None:
    """文件切换为异步流程（未命中预加载时经后台线程 + 进度框）。"""
    deadline = time.time() + timeout_s
    while window._current_file != rel and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    assert window._current_file == rel, (
        f"切换文件超时：期望 {rel}，实际 {window._current_file}"
    )


def _wait_for_report(window: MainWindow, timeout_s: float = 60.0) -> None:
    """返修单生成为后台流程（进度框 + 防 GUI 卡死）。"""
    deadline = time.time() + timeout_s
    while window._report_worker is not None and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    assert window._report_worker is None, "返修单生成超时（后台 worker 未完成）"
    assert window._report_dialog is None, "返修单进度框未关闭"


def test_preload_worker() -> None:
    """预加载线程：open 请求缓存 merged/背景/目标图层；队列可整体替换。"""
    from mangaproof.psd.document import PSDDocument
    from mangaproof.ui.preloader import KIND_OPEN, PreloadWorker

    with tempfile.TemporaryDirectory() as tmp:
        folder = _copy_fixtures(Path(tmp) / "chapter01")
        docs = {
            p.name: PSDDocument(p) for p in sorted(folder.glob("*.psd"))
        }
        for doc in docs.values():
            doc.build_layers()   # 模拟 attach 已解析图层树

        done = []
        warm = {}
        worker = PreloadWorker(lambda rel: docs.get(rel))
        worker.task_done.connect(
            lambda rel, kind, ok, images: (
                done.append((rel, kind, ok)),
                warm.setdefault(rel, {}).update(
                    {k: v for k, v in (images or {}).items() if v is not None}
                ),
            )
        )
        worker.start()

        # open 请求：merged + 背景 + 目标图层像素/视觉边界全部预热
        layer_id = docs["001.psd"].layers[1].id
        worker.submit_open("001.psd", layer_id)
        deadline = time.time() + 30
        while not any(d[0] == "001.psd" for d in done) and time.time() < deadline:
            app.processEvents()
            time.sleep(0.02)
        assert ("001.psd", KIND_OPEN, True) in done
        doc = docs["001.psd"]
        # open 请求 = merged + 目标图层（关键路径）；背景图在阶段 B 补提
        assert doc.has_merged()
        assert doc.layer_image(layer_id) is not None
        assert doc.layers[1].visual_bounds() is not None
        # 显示用 QImage 已后台预热（切换后首帧免转换）
        assert warm.get("001.psd", {}).get("merged") is not None

        # 两阶段队列：阶段 A 先铺 merged，阶段 B 补背景图与图层像素
        jobs = [
            ("002.psd", docs["002.psd"].layers[0].id),
            ("10.psd", docs["10.psd"].layers[0].id),
        ]
        worker.set_preloads(jobs, list(jobs))
        deadline = time.time() + 30
        while time.time() < deadline:
            ready = all(
                d.has_merged() and d.bg_image() is not None
                for d in (docs["002.psd"], docs["10.psd"])
            )
            if ready:
                break
            app.processEvents()
            time.sleep(0.02)
        assert docs["002.psd"].has_merged() and docs["002.psd"].bg_image() is not None
        assert docs["10.psd"].has_merged() and docs["10.psd"].bg_image() is not None
        # 目标图层像素与视觉边界已预热（快速路径定位免等待）
        assert docs["002.psd"].layer_image(docs["002.psd"].layers[0].id) is not None
        assert docs["002.psd"].layers[0].visual_bounds() is not None
        assert docs["10.psd"].layer_image(docs["10.psd"].layers[0].id) is not None

        # 快速切换：cancel_open 丢弃未处理请求，新 open 请求立即生效
        worker.cancel_open()
        worker.submit_open("002.psd", docs["002.psd"].layers[0].id)
        deadline = time.time() + 30
        while not any(d[0] == "002.psd" and d[1] == KIND_OPEN for d in done) \
                and time.time() < deadline:
            app.processEvents()
            time.sleep(0.02)
        assert any(d[0] == "002.psd" and d[1] == KIND_OPEN for d in done)

        # WARM_ALL 哨兵：预热文档全部图层的视觉边界（图层切换免等待）
        from mangaproof.ui.preloader import WARM_ALL

        worker.set_preloads([], [("001.psd", WARM_ALL)])
        deadline = time.time() + 30
        while not all(
            info.has_visual_bounds() for info in docs["001.psd"].layers
        ) and time.time() < deadline:
            app.processEvents()
            time.sleep(0.02)
        assert all(
            info.has_visual_bounds() for info in docs["001.psd"].layers
        )

        worker.stop()
        worker.wait(5000)

    print("PASS test_preload_worker")


def test_layer_panel_layout_and_elide() -> None:
    """图层列表：竖向占用与左侧文件面板对齐（2:3），长名省略号截断、
    禁止横向滚动、完整名 tooltip（与问题面板 _ElidedLabel 风格一致）。"""
    with tempfile.TemporaryDirectory() as tmp:
        sm = SettingsManager(Path(tmp) / "settings.json")
        window = MainWindow(sm)
        window.resize(1200, 800)
        window.show()
        app.processEvents()

        # 左右 dock 拉伸比一致：file:stats = layer:issue = 2:3
        left_layout = window.left_dock.widget().layout()
        right_layout = window.right_dock.widget().layout()
        assert left_layout.stretch(0) == 2 and left_layout.stretch(1) == 3
        assert right_layout.stretch(0) == 2 and right_layout.stretch(1) == 3

        lw = window.layer_panel.list_widget
        assert lw.textElideMode() == Qt.TextElideMode.ElideRight
        assert lw.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        assert lw.wordWrap() is False
        assert lw.uniformItemSizes() is True

        # 长图层名：条目保留完整文本、tooltip 完整、无横向滚动
        long_name = "对话气泡_第3页_主角台词_这一段非常长需要省略号截断" * 3
        window.layer_panel.set_layers([long_name])
        app.processEvents()
        item = lw.item(0)
        assert item is not None and long_name in item.text()
        assert item.toolTip() == item.text()

        # set_statuses 后 tooltip 跟随更新（含问题数后缀）
        window.layer_panel.set_statuses(["failed"], [2])
        item = lw.item(0)
        assert item is not None and "（2 个问题）" in item.toolTip()

        window.close()
        app.processEvents()

    print("PASS test_layer_panel_layout_and_elide")


def test_file_panel_status_icons() -> None:
    """回归：PSD 文件列表状态图标必须与 TaskState.file_status() 取值域对齐。

    历史缺陷：面板图标表按 "done" 取键，而 file_status() 全通过时返回
    "passed"，导致全部通过的 PSD 落到未监制兜底样式（灰 ○），
    而"有未通过"因键名恰好都是 "failed" 而正常显示红 ✗。
    """
    from mangaproof.ui.file_panel import STATUS_STYLES

    # 1) 契约：状态取值域必须全部被图标表覆盖，且不能有失效键
    probe = TaskState()
    probed = {
        probe.file_status("empty.psd", []),          # 无图层
        probe.file_status("none.psd", ["a", "b"]),   # 未监制
        probe.file_status("half.psd", ["a", "b"]),
        probe.file_status("pass.psd", ["a", "b"]),
        probe.file_status("fail.psd", ["a", "b"]),
    }
    probe.set_status("half.psd", "a", PASSED)        # 部分监制
    for lid in ("a", "b"):
        probe.set_status("pass.psd", lid, PASSED)    # 全部通过
        probe.set_status("fail.psd", lid, FAILED)    # 全部未通过
    probed |= {
        probe.file_status("half.psd", ["a", "b"]),
        probe.file_status("pass.psd", ["a", "b"]),
        probe.file_status("fail.psd", ["a", "b"]),
    }
    assert probed == {UNREVIEWED, PASSED, FAILED, PARTIAL}, probed
    assert probed <= set(STATUS_STYLES), set(probed) - set(STATUS_STYLES)

    # 2) 端到端：真实窗口 → 文件列表条目文本与前景色
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = _copy_fixtures(root / "chapter01")
        window = MainWindow(SettingsManager(root / "settings.json"))
        window.resize(1200, 800)
        window.show()
        app.processEvents()
        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window.open_folder(folder)
        _wait_for_task(window)

        rels = [r.relative_path for r in window.task.files]
        assert len(rels) >= 3, rels
        all_pass, all_fail, partial = rels[0], rels[1], rels[2]
        assert all(len(window._layer_ids_by_file[r]) >= 2 for r in rels), (
            window._layer_ids_by_file
        )

        def snapshot() -> dict:
            lw = window.file_panel.list_widget
            assert lw.count() == len(rels)
            return {
                lw.item(i).data(Qt.ItemDataRole.UserRole): (
                    lw.item(i).text(), lw.item(i).foreground().color().name()
                )
                for i in range(lw.count())
            }

        # 全部未监制：○ 灰
        for rel, (text, color) in snapshot().items():
            assert text.startswith("○ "), (rel, text)
            assert color == QColor(COLOR_UNREVIEWED).name(), (rel, color)

        for lid in window._layer_ids_by_file[all_pass]:
            window.task.set_status(all_pass, lid, PASSED)
        for lid in window._layer_ids_by_file[all_fail]:
            window.task.set_status(all_fail, lid, FAILED)
        # 部分监制：只通过第一层，其余保持未监制
        window.task.set_status(partial, window._layer_ids_by_file[partial][0], PASSED)
        window._refresh_file_panel()
        app.processEvents()

        shown = snapshot()
        text, color = shown[all_pass]
        assert text.startswith("✓ "), (text, color)
        assert color == QColor(COLOR_PASS).name(), (text, color)
        text, color = shown[all_fail]
        assert text.startswith("✗ "), (text, color)
        assert color == QColor(COLOR_FAIL).name(), (text, color)
        text, color = shown[partial]
        assert text.startswith("● "), (text, color)
        assert color == QColor(COLOR_WARN).name(), (text, color)

        window.close()
        app.processEvents()

    print("PASS test_file_panel_status_icons")


def test_task_loader_progress() -> None:
    """后台加载 worker：进度消息覆盖扫描/解析阶段，任务正确产出。"""
    with tempfile.TemporaryDirectory() as tmp:
        folder = _copy_fixtures(Path(tmp) / "chapter01")
        messages = []
        results = []
        worker = TaskLoadWorker("folder", folder)
        worker.progress.connect(lambda d, t, m: messages.append((d, t, m)))
        worker.succeeded.connect(lambda r: results.append(r))
        worker.start()
        deadline = time.time() + 30
        while not results and time.time() < deadline:
            app.processEvents()
            time.sleep(0.02)
        assert results, "worker 未完成"
        result = results[0]
        assert result.kind == "ok"
        assert result.task is not None
        assert any("扫描 PSD" in m for _, _, m in messages)
        parse_msgs = [m for _, _, m in messages if "解析 PSD" in m]
        assert len(parse_msgs) == 3, parse_msgs
        assert messages[-1][2] == "加载完成"

    print("PASS test_task_loader_progress")


def _pump_until(cond, timeout_s: float = 30.0) -> bool:
    """轮询事件循环直到条件成立（后台线程测试用），返回是否成立。"""
    deadline = time.time() + timeout_s
    while not cond() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    return bool(cond())


def _wait_for_numbering(window: MainWindow, timeout_s: float = 60.0) -> None:
    """问题编号检查为后台流程（进度框 + 防 GUI 卡死）。"""
    deadline = time.time() + timeout_s
    while window._numbering_worker is not None and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    assert window._numbering_worker is None, "编号检查超时（后台 worker 未完成）"
    assert window._numbering_dialog is None, "编号检查进度框未关闭"


def test_numbering_worker() -> None:
    """后台编号 worker：正常产出方案；先请求取消 → 不改动任务。"""
    from mangaproof.review.numbering import apply_numbering
    from mangaproof.ui.numbering_worker import KIND_CANCELLED, KIND_OK, NumberingWorker

    with tempfile.TemporaryDirectory() as tmp:
        folder = _copy_fixtures(Path(tmp) / "chapter01")
        files = sorted(folder.glob("*.psd"))
        task, _ = persistence.create_task_folder(folder, files)
        from mangaproof.psd.document import PSDDocument

        layer_ids = {
            p.name: [i.id for i in PSDDocument(folder / p.name).layers] for p in files
        }
        ids1 = layer_ids["001.psd"]
        a = task.add_issue("001.psd", ids1[1], "dialogue_01", "字体选择错误", "", (0, 0, 1, 1))
        task.add_issue("10.psd", layer_ids["10.psd"][1], "dialogue_01", "漏字", "", (0, 0, 1, 1))
        task.remove_issue(a.issue_id)          # 制造空号
        task.add_issue("001.psd", ids1[1], "dialogue_01", "居中错误", "", (0, 0, 1, 1))
        assert [i.issue_no for i in task.issues] == [2, 3]

        # 1) 正常：产出方案（不修改任务），进度推进到结束
        messages = []
        results = []
        worker = NumberingWorker(task, layer_ids)
        worker.progress.connect(lambda d, t, m: messages.append((d, t, m)))
        worker.succeeded.connect(lambda r: results.append(r))
        worker.start()
        _pump_until(lambda: bool(results))
        assert results and results[0].kind == KIND_OK
        plan = results[0].plan
        # 001.psd 的问题排在 10.psd 之前：#2（10.psd）保持 2，#3（001.psd）改为 1
        assert plan.total == 2 and plan.fixed == 1
        assert [i.issue_no for i in task.issues] == [2, 3], "worker 不应直接修改任务"
        assert messages[-1][0] == messages[-1][1]
        assert messages[-1][2] == "编号检查完成"
        apply_numbering(task, plan)
        assert [i.issue_no for i in task.issues] == [1, 2]

        # 2) 取消：先请求取消 → 第一次进度回调即中断，任务不变
        task.issues[0].issue_no = 42
        results2 = []
        worker2 = NumberingWorker(task, layer_ids)
        worker2.succeeded.connect(lambda r: results2.append(r))
        worker2.request_cancel()
        worker2.start()
        _pump_until(lambda: bool(results2))
        assert results2 and results2[0].kind == KIND_CANCELLED
        assert task.issues[0].issue_no == 42, "取消后任务不应被改动"

    print("PASS test_numbering_worker")


def test_check_issue_numbers_workflow() -> None:
    """主界面「检查问题编号」：进度框 → 编号按文档顺序重排 → 立即保存。"""
    from mangaproof.review.state import FAILED

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = _copy_fixtures(root / "chapter01")
        window = MainWindow(SettingsManager(root / "settings.json"))
        window.resize(1200, 800)
        window.show()
        app.processEvents()
        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window.open_folder(folder)
        _wait_for_task(window)

        # 无任务时按钮禁用；打开任务后可用
        assert window.action_renumber.isEnabled()

        ids = window._layer_ids_by_file["001.psd"]
        ids10 = window._layer_ids_by_file["10.psd"]
        first = window.task.add_issue("001.psd", ids[1], "dialogue_01", "字体选择错误",
                                      "", (10, 10, 50, 50))
        removed = window.task.add_issue("001.psd", ids[1], "dialogue_01", "漏字",
                                        "", (60, 60, 50, 50))
        last = window.task.add_issue("10.psd", ids10[1], "dialogue_01", "居中错误",
                                     "", (10, 10, 50, 50))
        window.task.remove_issue(removed.issue_id)
        back = window.task.add_issue("001.psd", ids[1], "dialogue_01", "原文字擦除错误",
                                     "", (120, 120, 50, 50))   # 回头补问题 → 编号最大
        for issue in (first, last, back):
            window.task.set_status(issue.file, issue.layer_id, FAILED)
        # 问题面板显示「当前图层」的问题 → 切到放了问题的图层
        window._select_layer_internal(1)
        window._refresh_all_panels()
        window._refresh_viewer_issues()
        window._mark_dirty()
        assert [i.issue_no for i in window.task.issues] == [1, 3, 4]

        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ) as info:
            window.check_issue_numbers()
            assert window._numbering_worker is not None, "编号检查未走后台线程"
            assert window._numbering_dialog is not None, "未显示编号检查进度框"
            assert not window.action_renumber.isEnabled(), "检查期间按钮应禁用"
            _wait_for_numbering(window)
            assert info.called, "有编号被修正时应给出结果提示"

        # 文档顺序：001.psd（按创建顺序）→ 10.psd，编号连续
        assert [i.issue_id for i in window.task.issues] == [
            first.issue_id, back.issue_id, last.issue_id
        ]
        assert [i.issue_no for i in window.task.issues] == [1, 2, 3]
        # 问题面板同步显示新编号
        assert window.issue_panel.issue_list.item(0).text().startswith("#1")
        assert window.issue_panel.issue_list.item(1).text().startswith("#2")
        # 立即落盘：重新读取进度文件编号已修正
        saved = persistence.load_task(window.progress_file_path())
        assert [i.issue_no for i in saved.issues] == [1, 2, 3]

        # 再次检查：编号已连续 → 只给状态栏提示，不再弹结果框
        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ) as info2:
            window.check_issue_numbers()
            _wait_for_numbering(window)
            assert not info2.called, "无需调整时不应弹结果提示"
        assert "无需调整" in window.statusBar().currentMessage()

        window.close()
        app.processEvents()

    print("PASS test_check_issue_numbers_workflow")


def test_report_worker() -> None:
    """后台返修单 worker：进度信号推进到完成；请求取消后不落盘。"""
    from mangaproof.psd.document import PSDDocument
    from mangaproof.review import persistence
    from mangaproof.ui.report_worker import KIND_CANCELLED, KIND_OK, ReportWorker

    with tempfile.TemporaryDirectory() as tmp:
        folder = _copy_fixtures(Path(tmp) / "chapter01")
        task, _ = persistence.create_task_folder(folder, sorted(folder.glob("*.psd")))
        doc = PSDDocument(folder / "001.psd")
        ids = [i.id for i in doc.layers]
        task.set_status("001.psd", ids[1], FAILED)
        task.add_issue(
            "001.psd", ids[1], "dialogue_01", "字体选择错误", "这里应使用 Bold",
            (40, 60, 120, 60),
        )
        layer_ids = {"001.psd": ids, "002.psd": [], "10.psd": []}

        # 1) 正常生成：进度消息推进，产物落盘
        out = folder / "worker.pdf"
        messages = []
        results = []
        worker = ReportWorker(task, layer_ids, out, folder, docs={"001.psd": doc})
        worker.progress.connect(lambda d, t, m: messages.append((d, t, m)))
        worker.succeeded.connect(lambda r: results.append(r))
        worker.start()
        _pump_until(lambda: bool(results))
        assert results, "返修单 worker 未完成"
        assert results[0].kind == KIND_OK
        assert Path(results[0].path) == out
        assert out.exists() and out.stat().st_size > 1000
        assert messages and messages[0][0] == 0
        assert messages[-1][0] == messages[-1][1]
        assert messages[-1][2] == "生成完成"

        # 2) 取消：请求取消后第一次进度回调即中断，PDF 不落盘
        out_cancel = folder / "worker_cancelled.pdf"
        results2 = []
        worker2 = ReportWorker(task, layer_ids, out_cancel, folder, docs={"001.psd": doc})
        worker2.succeeded.connect(lambda r: results2.append(r))
        worker2.request_cancel()
        worker2.start()
        _pump_until(lambda: bool(results2))
        assert results2, "取消后 worker 未返回"
        assert results2[0].kind == KIND_CANCELLED
        assert not out_cancel.exists(), "取消后不应留下返修单文件"

    print("PASS test_report_worker")


def test_report_progress_dialog_and_cancel() -> None:
    """返修单导出：进度框逐页推进（界面不冻结）；「取消」不留下半成品。"""
    import mangaproof.report.generator as gen
    from mangaproof.review.state import FAILED

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = _copy_fixtures(root / "chapter01")
        window = MainWindow(SettingsManager(root / "settings.json"))
        window.resize(1200, 800)
        window.show()
        app.processEvents()
        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window.open_folder(folder)
        _wait_for_task(window)

        # 4 个未通过图层 → 4 个明细页（多个进度步，取消点确定落在页边界）
        doc = window.current_doc
        for info in doc.layers[:4]:
            window.task.set_status(window._current_file, info.id, FAILED)
            window.task.add_issue(
                window._current_file, info.id, info.name, "漏字", "", (10, 20, 80, 40)
            )

        real_encode = gen._encode_page_image

        def slow_encode(img, image_format="png", quality=80):
            time.sleep(0.1)           # 放慢每页编码，模拟大页面
            return real_encode(img, image_format, quality)

        with patch.object(gen, "_encode_page_image", slow_encode), patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window._generate_report(interactive=False)
            dialog = window._report_dialog
            assert dialog is not None, "导出未显示进度框"
            assert dialog.windowModality() == Qt.WindowModality.WindowModal
            bar = dialog.findChild(QProgressBar)
            assert bar is not None, "进度框缺少进度条"
            # 不覆盖样式表 → 沿用全局主题的 QProgressBar 样式（与既有进度框一致）
            assert bar.styleSheet() == "", "返修单进度条不应单独定制样式"
            # 生成期间主线程仍在跑事件循环 → 进度持续推进，界面未冻结
            assert _pump_until(lambda: dialog.value() >= 2, 30), (
                f"进度未推进（当前 {dialog.value()}/{dialog.maximum()}）"
            )
            assert dialog.isVisible(), "生成期间进度框应保持可见"
            cancel_btn = next(
                b for b in dialog.findChildren(QPushButton) if b.text() == "取消"
            )
            cancel_btn.click()
            _wait_for_report(window)

        assert not (folder / "chapter01.pdf").exists(), "取消后不应留下返修单文件"
        assert "已取消" in window.statusBar().currentMessage()
        window.close()
        app.processEvents()

    print("PASS test_report_progress_dialog_and_cancel")


def test_dark_titlebar_installed() -> None:
    """暗色标题栏：应用级过滤器安装成功；Linux 下应用调用为 no-op。"""
    from mangaproof.ui.dark_titlebar import apply_dark_title_bar, install_dark_titlebar

    install_dark_titlebar(app)
    assert getattr(app, "_dark_titlebar_filter", None) is not None

    probe = QWidget()
    probe.resize(200, 100)
    probe.show()
    app.processEvents()
    apply_dark_title_bar(probe)   # 非 Windows 平台必须静默 no-op
    probe.close()

    print("PASS test_dark_titlebar_installed")


def test_app_icon_loaded() -> None:
    """应用图标：从 ico/ico.png 加载到 QApplication，窗口默认继承。"""
    from mangaproof.main import apply_app_icon

    icon_path = Path(__file__).parent.parent / "ico" / "ico.png"
    assert icon_path.exists(), "缺少 ico/ico.png"
    result = apply_app_icon(app, icon_path)
    assert result == icon_path
    assert not app.windowIcon().isNull()

    # 图标缺失时不阻塞启动
    assert apply_app_icon(app, icon_path.parent / "missing.png") is None

    print("PASS test_app_icon_loaded")


def test_console_switch_platform_aware() -> None:
    """控制台开关仅 Windows 可用；其他平台置灰且不影响直接运行 py。"""
    import sys

    from mangaproof.config.settings import Settings
    from mangaproof.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(Settings())
    # 仅 Windows 打包产物支持运行时切换 → 非 Windows 上复选框应禁用
    assert dialog.console_check.isEnabled() == (sys.platform == "win32")
    assert dialog.console_check.isChecked()  # 默认开启隐藏

    print("PASS test_console_switch_platform_aware")


def test_font_loading() -> None:
    """统一字体：MiSans 注册为应用字体与主题首选字体族；缺失回退。"""
    from mangaproof.fonts import load_app_fonts
    from mangaproof.ui.theme import apply_dark_theme

    font_path = Path(__file__).parent.parent / "font" / "MiSans-Medium.ttf"
    assert font_path.exists(), "缺少 font/MiSans-Medium.ttf"

    family = load_app_fonts(app, [font_path])
    assert family == "MiSans", family
    assert app.font().family() == "MiSans"

    # 缺失时回退，不阻塞启动
    assert load_app_fonts(app, [font_path.parent / "missing.ttf"]) is None

    # 主题样式表字体族首位为 MiSans
    apply_dark_theme(app, primary_family=family)
    css = app.styleSheet()
    assert '"MiSans"' in css
    first = css.split("font-family:", 1)[1].strip().split(";", 1)[0]
    assert first.startswith('"MiSans"'), first

    print("PASS test_font_loading")


def test_license_page() -> None:
    """第三方许可页：与「关于」分离，覆盖全部依赖/库/打包工具/字体。"""
    from mangaproof.third_party import build_third_party_items
    from mangaproof.ui.license_dialog import LicenseDialog

    items = build_third_party_items()
    names = [i.name for i in items]
    # 覆盖：运行时、PSD 解析、图像分析、GUI、PDF、图像处理、打包工具及其依赖、字体
    for keyword in ("Python", "psd-tools", "NumPy", "PySide6", "reportlab",
                    "Pillow", "PyInstaller", "altgraph", "MiSans"):
        assert any(keyword in n for n in names), f"缺少组件：{keyword}"
    for item in items:
        assert item.name and item.version and item.spdx and item.copyright
        assert item.homepage.startswith("http")
        assert len(item.license_text) > 100
    # MiSans 条目包含完整协议与出处
    misans = next(i for i in items if "MiSans" in i.name)
    assert "小米" in misans.license_text and "hyperos.mi.com" in misans.homepage
    # 版本解析：已安装包应返回真实版本
    psd = next(i for i in items if "psd-tools" in i.name)
    assert psd.version == "1.18.0"

    # 对话框：组件列表与详情联动
    dialog = LicenseDialog()
    assert dialog.component_list.count() == len(items)
    dialog.component_list.setCurrentRow(1)
    app.processEvents()
    detail = dialog.detail_view.toPlainText()
    assert "许可证" in detail and "psd-tools" in detail
    dialog.close()

    print("PASS test_license_page")


def test_full_workflow() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = _copy_fixtures(root / "chapter01")

        sm = SettingsManager(root / "settings.json")
        window = MainWindow(sm)
        window.resize(1200, 800)
        window.show()
        app.processEvents()

        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window.open_folder(folder)
        _wait_for_task(window)
        app.processEvents()

        assert window.task is not None
        assert window.task.task_type == "folder"
        assert window._current_file == "001.psd"
        assert window._current_index == 0  # 第一个未监制图层
        # 第一个图层是 bg（整幅画布）→ 自动缩放为画布比例、中心在画布中心
        assert 0.5 < window.viewer.camera.zoom < 1.0
        assert abs(window.viewer.camera.center_x - 200.0) < 1.0
        assert abs(window.viewer.camera.center_y - 300.0) < 1.0

        doc = window.current_doc
        ids = [info.id for info in doc.layers]

        # 回归：统计面板卡片必须按富文本渲染（否则 HTML 源码会直接显示）
        for cell in window.stats_panel.total_cells.values():
            assert cell.textFormat() == Qt.TextFormat.RichText
            assert "<br/>" in cell.text()
        assert "通过" in window.stats_panel.total_cells["passed"].text()
        assert "3" in window.stats_panel.total_cells["files"].text()  # 3 个 PSD
        # 总图层 = 001 可监制 6（bg/dialogue×3/text1/text2）+ 002 两个 + 10 两个
        assert "10" in window.stats_panel.total_cells["layers"].text()

        # 回归：按钮动态显示当前绑定（需求 §30），重绑定后文案跟随更新
        assert "Enter" in window.issue_panel.pass_btn.text()
        assert "/" in window.issue_panel.fail_btn.text()
        assert "Ctrl+Return" in window.issue_panel.custom_btn.text()
        assert "R" in window.issue_panel.add_btn.text()
        assert "居中错误" in window.issue_panel.add_btn.toolTip()
        assert "Enter" in window.hint_label.text() and "↑/↓" in window.hint_label.text()
        window.settings.keybindings["pass_layer"] = "Ctrl+P"
        window.settings.keybindings["fail_layer"] = "F2"
        window._rebuild_shortcuts()
        assert "Ctrl+P" in window.issue_panel.pass_btn.text()
        assert "F2" in window.issue_panel.fail_btn.text()
        window.settings.keybindings["pass_layer"] = "Return"
        window.settings.keybindings["fail_layer"] = "/"
        window._rebuild_shortcuts()
        assert "Enter" in window.issue_panel.pass_btn.text()
        assert "/" in window.issue_panel.fail_btn.text()

        # 预加载状态标签：两阶段独立显示
        # （图像预加载=阶段A / 图层预热=阶段B / 双完成后切换零等待）
        assert window.preload_label.text() != "", "预加载标签应有内容"
        deadline = time.time() + 30
        while (
            window.preload_label.text() != "图像预加载完成"
            or window.warmup_label.text() != "图层预热完成"
        ) and time.time() < deadline:
            app.processEvents()
            time.sleep(0.02)
        assert window.preload_label.text() == "图像预加载完成"
        assert window.warmup_label.text() == "图层预热完成"
        # 完成后，邻域文件的目标图层像素必须已预热（否则切换会卡 UI 线程）
        for rel, doc in window._docs.items():
            if rel == window._current_file:
                continue
            index = window._choose_layer_index(rel, restore=False)
            if index is None:
                continue
            lid = window._layer_ids_by_file[rel][index]
            info = doc.layer_by_id(lid)
            assert info is not None and info.has_visual_bounds(), (
                f"预加载完成后 {rel} 目标图层未预热"
            )
        # 当前文件全部图层视觉边界已预热（←→ 图层切换零等待）
        assert all(
            info.has_visual_bounds() for info in window.current_doc.layers
        )

        # Enter → 通过并跳到下一个未监制
        window.mark_pass()
        app.processEvents()
        assert window.task.status_of("001.psd", ids[0]) == PASSED
        assert window._current_index == 1
        # 切换到 dialogue_01（120x60）→ 视觉中心定位 + 按比例缩放（需求 §17、§20）
        assert abs(window.viewer.camera.center_x - 100.0) < 1.0
        assert abs(window.viewer.camera.center_y - 90.0) < 1.0
        assert window.viewer.camera.zoom > 2.0

        # / → 未通过，停留在当前图层
        window.mark_fail()
        app.processEvents()
        assert window.task.status_of("001.psd", ids[1]) == FAILED
        assert window._current_index == 1

        # 方式 B：拖框 → 问题对话框（patched）→ 问题入库 + 红框世界坐标
        with patch.object(
            IssueDialog, "exec", return_value=IssueDialog.DialogCode.Accepted
        ), patch.object(
            IssueDialog, "result_values", return_value=("字体选择错误", "这里应使用 Bold")
        ):
            window._on_rect_drawn(40, 60, 120, 60)
        assert len(window.task.issues) == 1
        issue = window.task.issues[0]
        assert issue.issue_no == 1
        assert issue.rect == (40.0, 60.0, 120.0, 60.0)
        assert len(window.viewer._issues) == 1  # Overlay 已挂到 Viewer

        # 方式 A：快捷键类型 → pending → 拖框 → 问题入库（需求 §35、§37）
        window._on_issue_key("漏字")
        assert window.viewer.pending_type == "漏字"
        with patch.object(
            IssueDialog, "exec", return_value=IssueDialog.DialogCode.Accepted
        ), patch.object(
            IssueDialog, "result_values", return_value=("漏字", "")
        ):
            window._on_issue_drawn("漏字", 60, 240, 140, 60)
        assert len(window.task.issues) == 2
        assert window.task.issues[1].type == "漏字"

        # Enter on FAILED → 保持未通过，跳到下一个未监制（需求 §38）
        window.mark_pass()
        assert window.task.status_of("001.psd", ids[1]) == FAILED
        assert window._current_index == 2

        # ← → 图层导航
        window.prev_layer()
        assert window._current_index == 1
        window.next_layer()
        assert window._current_index == 2

        # Esc 取消 pending 批注操作（需求 §30）
        window._on_issue_key("错字")
        assert window.viewer.pending_type == "错字"
        window.cancel_operation()
        assert window.viewer.pending_type is None

        # ↑↓ PSD 导航（异步切换：预加载命中走快路径，未命中经后台线程）
        window.next_psd()
        _wait_for_file(window, "002.psd")
        # 切换后 Viewer 已有后台预热好的显示图（首帧免转换）
        assert any(key[1] == "merged" for key in window.viewer._qimages)
        window.prev_psd()
        _wait_for_file(window, "001.psd")

        # Space 自动对比：merged ↔ bg 闪切
        window.toggle_compare()
        assert window._compare.is_running
        seen = set()
        for _ in range(10):
            seen.add(window.viewer.source)
            app.processEvents()
            time.sleep(0.12)
        assert "bg" in seen and "merged" in seen
        window.toggle_compare()
        assert not window._compare.is_running
        assert window.viewer.source == "merged"  # 停止后恢复 Original（需求 §26）

        # 保存 + 磁盘校验
        window.save_task()
        progress = persistence.progress_path_for_folder(folder)
        assert progress.exists()
        loaded = persistence.load_task(progress)
        assert loaded.schema_version == 1
        assert loaded.status_of("001.psd", ids[0]) == PASSED
        assert loaded.status_of("001.psd", ids[1]) == FAILED
        assert len(loaded.issues) == 2
        window.close()
        app.processEvents()

        # ---- 重启恢复（需求 §6：自动恢复进度，无需手动加载） ----
        sm2 = SettingsManager(root / "settings2.json")
        window2 = MainWindow(sm2)
        window2.resize(1200, 800)
        window2.show()
        app.processEvents()
        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window2.open_folder(folder)
        _wait_for_task(window2)
        app.processEvents()

        assert window2.task.task_id == loaded.task_id  # 恢复的是同一个任务
        assert window2._current_file == "001.psd"
        assert window2._current_index == 2  # 上次工作位置
        assert window2.task.status_of("001.psd", ids[0]) == PASSED
        assert window2.task.status_of("001.psd", ids[1]) == FAILED
        assert len(window2.task.issues_for("001.psd", ids[1])) == 2

        # 生成返修单（非交互）：后台线程 + 进度框，避免大批量任务冻结界面
        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window2._generate_report(interactive=False)
            assert window2._report_worker is not None, "返修单生成未走后台线程"
            assert window2._report_dialog is not None, "未显示返修单进度框"
            _wait_for_report(window2)
        out = folder / "chapter01.pdf"
        assert out.exists() and out.stat().st_size > 1000
        with open(out, "rb") as f:
            assert f.read(5) == b"%PDF-"

        window2.close()
        app.processEvents()

    print("PASS test_full_workflow")


def test_compare_controller_unit() -> None:
    """CompareController：频率换算、间隔设置、手动切换、打断。"""
    from mangaproof.compare.controller import (
        BG_ONLY,
        DEFAULT_SPEED_HZ,
        ORIGINAL,
        CompareController,
        hz_to_interval_ms,
    )

    # 档位 → 每张停留时长（均为整数毫秒）
    assert hz_to_interval_ms(1) == 1000
    assert hz_to_interval_ms(2) == 500
    assert hz_to_interval_ms(4) == 250
    assert hz_to_interval_ms(5) == 200
    assert hz_to_interval_ms(8) == 125

    c = CompareController()
    assert c.interval_ms == hz_to_interval_ms(DEFAULT_SPEED_HZ)
    c.set_interval_ms(200)
    assert c.interval_ms == 200
    c.set_interval_ms(0)   # 下限钳制
    assert c.interval_ms == 1

    # 手动挡：swap_once 不启动定时器
    assert c.display_state == ORIGINAL
    c.swap_once()
    assert c.display_state == BG_ONLY and not c.is_running
    c.swap_once()
    assert c.display_state == ORIGINAL

    # interrupt：手动挡下强制回原图
    c.swap_once()
    c.interrupt()
    assert c.display_state == ORIGINAL and not c.is_running

    # interrupt：自动挡运行中 → 停止并回原图
    c.set_interval_ms(250)
    c.start()
    assert c.is_running
    c.interrupt()
    assert not c.is_running and c.display_state == ORIGINAL

    print("PASS test_compare_controller_unit")


def test_settings_compare_options() -> None:
    """设置对话框：对比模式/速度档位、手动挡灰显速度、应用与恢复默认。"""
    from mangaproof.config.settings import Settings
    from mangaproof.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(Settings())
    # 默认：自动模式，速度=正常 4 次/秒
    assert dialog.compare_mode_combo.currentData() == "auto"
    assert dialog.compare_speed_combo.isEnabled()
    assert dialog.compare_speed_combo.currentData() == 4
    assert "4 次/秒（每张 250ms）" in dialog.compare_speed_combo.currentText()

    # 切手动挡 → 速度灰显
    dialog.compare_mode_combo.setCurrentIndex(
        dialog.compare_mode_combo.findData("manual")
    )
    assert not dialog.compare_speed_combo.isEnabled()

    s = Settings()
    dialog.apply_to(s)
    assert s.compare_mode == "manual"

    # 自动挡选 8 次/秒 → 应用到设置
    dialog.compare_mode_combo.setCurrentIndex(
        dialog.compare_mode_combo.findData("auto")
    )
    assert dialog.compare_speed_combo.isEnabled()
    dialog.compare_speed_combo.setCurrentIndex(
        dialog.compare_speed_combo.findData(8)
    )
    s2 = Settings()
    dialog.apply_to(s2)
    assert s2.compare_mode == "auto" and s2.compare_speed_hz == 8

    # 恢复默认 → 自动 + 4 次/秒
    dialog._reset_defaults()
    s3 = Settings()
    dialog.apply_to(s3)
    assert s3.compare_mode == "auto" and s3.compare_speed_hz == 4

    # 自定义速度（档位外数值）也能回显选中
    dialog.compare_speed_combo.setCurrentIndex(
        dialog.compare_speed_combo.findData(8)
    )
    s_custom = Settings()
    s_custom.compare_speed_hz = 3
    dialog2 = SettingsDialog(s_custom)
    assert dialog2.compare_speed_combo.currentData() == 3
    s_custom_out = Settings()
    dialog2.apply_to(s_custom_out)
    assert s_custom_out.compare_speed_hz == 3

    print("PASS test_settings_compare_options")


def test_compare_manual_mode_workflow() -> None:
    """手动挡：Space/按钮按一下切一次；Esc、通过等操作打断并强制回原图。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = _copy_fixtures(root / "chapter01")

        sm = SettingsManager(root / "settings.json")
        sm.settings.compare_mode = "manual"
        window = MainWindow(sm)
        window.resize(1200, 800)
        window.show()
        app.processEvents()

        # 工具栏按钮文案随模式变化
        assert window.action_compare.text() == "对比切换 (Space)"

        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window.open_folder(folder)
        _wait_for_task(window)
        app.processEvents()

        # 等待背景图预热（对比依赖 bg 图）
        deadline = time.time() + 30
        while window.current_doc.bg_image() is None and time.time() < deadline:
            app.processEvents()
            time.sleep(0.02)
        assert window.current_doc.bg_image() is not None

        # 快捷键：按一下切一次，无运行状态
        assert window.viewer.source == "merged"
        window.toggle_compare()
        assert window.viewer.source == "bg"
        assert not window._compare.is_running
        window.toggle_compare()
        assert window.viewer.source == "merged"

        # 工具栏按钮点击：切换一次且不保持勾选
        window.action_compare.setChecked(True)
        app.processEvents()
        assert window.viewer.source == "bg"
        assert not window.action_compare.isChecked()

        # 打断：Esc 回原图
        window.cancel_operation()
        assert window.viewer.source == "merged"

        # 打断：通过当前图层（mark_pass）回原图
        window.toggle_compare()
        assert window.viewer.source == "bg"
        window.mark_pass()
        assert window.viewer.source == "merged"

        # 切回自动挡：按钮文案与 Space 行为恢复自动闪切
        window.settings.compare_mode = "auto"
        window._apply_compare_settings()
        assert window.action_compare.text() == "自动对比 (Space)"
        window.toggle_compare()
        assert window._compare.is_running
        window.toggle_compare()
        assert not window._compare.is_running
        assert window.viewer.source == "merged"

        window.close()
        app.processEvents()

    print("PASS test_compare_manual_mode_workflow")


def test_settings_keybindings_subdialog() -> None:
    """快捷键子对话框：独立编辑/恢复默认；主对话框 OK 才写回。"""
    from PySide6.QtGui import QKeySequence

    from mangaproof.config.settings import (
        DEFAULT_ISSUE_TYPES,
        DEFAULT_KEYBINDINGS,
        DEFAULT_WHEEL_MODE,
        Settings,
    )
    from mangaproof.ui.settings_dialog import KeybindingsDialog, SettingsDialog

    s = Settings()
    # 未打开子对话框 → 主对话框 apply 不动快捷键
    dlg = SettingsDialog(s)
    dlg.apply_to(s)
    assert s.keybindings["prev_psd"] == "Up"

    # 子对话框编辑并应用
    kb = KeybindingsDialog(s)
    kb._core_edits["prev_psd"].setKeySequence(QKeySequence("F1"))
    kb._issue_edits[0].setKeySequence(QKeySequence(""))
    kb.apply_to(s)
    assert s.keybindings["prev_psd"] == "F1"
    assert s.issue_types[0]["key"] == ""

    # 子对话框内恢复默认快捷键
    kb._reset_defaults()
    kb.apply_to(s)
    assert s.keybindings["prev_psd"] == DEFAULT_KEYBINDINGS["prev_psd"]
    assert s.issue_types[0]["key"] == DEFAULT_ISSUE_TYPES[0]["key"]
    assert s.custom_comment_key == DEFAULT_KEYBINDINGS["custom_comment"]

    # 主对话框"恢复默认设置"→ apply 后快捷键回默认
    s2 = Settings()
    s2.keybindings["prev_psd"] = "F2"
    dlg2 = SettingsDialog(s2)
    dlg2._reset_defaults()
    dlg2.apply_to(s2)
    assert s2.keybindings["prev_psd"] == DEFAULT_KEYBINDINGS["prev_psd"]
    assert s2.custom_comment_key == DEFAULT_KEYBINDINGS["custom_comment"]

    # 主对话框接受过子对话框 → apply 写回子对话框修改
    s3 = Settings()
    dlg3 = SettingsDialog(s3)
    kb3 = KeybindingsDialog(s3)
    kb3._core_edits["next_psd"].setKeySequence(QKeySequence("F3"))
    dlg3._kb_dialog = kb3
    dlg3.apply_to(s3)
    assert s3.keybindings["next_psd"] == "F3"

    # 主对话框 Cancel（不 apply）→ 设置不变
    s4 = Settings()
    dlg4 = SettingsDialog(s4)
    dlg4._kb_dialog = kb3
    assert s4.keybindings["next_psd"] == DEFAULT_KEYBINDINGS["next_psd"]

    # 滚轮模式下拉框：默认上下移动；切换缩放后 apply 写回；恢复默认复位
    s5 = Settings()
    assert s5.wheel_mode == DEFAULT_WHEEL_MODE == "pan"
    dlg5 = SettingsDialog(s5)
    zidx = dlg5.wheel_mode_combo.findData("zoom")
    assert zidx >= 0
    dlg5.wheel_mode_combo.setCurrentIndex(zidx)
    dlg5.apply_to(s5)
    assert s5.wheel_mode == "zoom"
    dlg5._reset_defaults()
    dlg5.apply_to(s5)
    assert s5.wheel_mode == DEFAULT_WHEEL_MODE

    print("PASS test_settings_keybindings_subdialog")


def test_report_dialog_hide_clean_option() -> None:
    """生成对话框：总览隐藏干净页默认勾选；改选后写回设置并持久化。"""
    from mangaproof.config.settings import Settings
    from mangaproof.ui.dialogs import ReportDialog

    assert Settings().report_hide_clean_files is True, "默认勾选"
    dialog = ReportDialog("n", "task", False, None, hide_clean_files=True)
    assert dialog.hide_clean_files() is True
    dialog.hide_clean_check.setChecked(False)
    assert dialog.hide_clean_files() is False

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = _copy_fixtures(root / "chapter01")
        sm = SettingsManager(root / "settings.json")
        window = MainWindow(sm)
        window.resize(1200, 800)
        window.show()
        app.processEvents()
        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window.open_folder(folder)
        _wait_for_task(window)
        ids = window._layer_ids_by_file["001.psd"]
        window.task.set_status("001.psd", ids[1], FAILED)
        window.task.add_issue("001.psd", ids[1], "dialogue_01", "漏字", "", (10, 10, 40, 40))

        def accept_and_uncheck(dialog_self):
            assert dialog_self.hide_clean_check.isChecked(), "对话框应预填设置值"
            dialog_self.hide_clean_check.setChecked(False)
            return ReportDialog.DialogCode.Accepted

        out = folder / "chapter01.pdf"
        with patch.object(ReportDialog, "exec", accept_and_uncheck), patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window._generate_report(interactive=True)
            assert window._report_worker is not None
            _wait_for_report(window)

        assert out.exists() and out.stat().st_size > 1000
        assert window.settings.report_hide_clean_files is False, "选择应写回设置"
        assert SettingsManager(root / "settings.json").settings.report_hide_clean_files is False

        window.close()
        app.processEvents()

    print("PASS test_report_dialog_hide_clean_option")


def _open_and_prepare_completion(window: MainWindow):
    """打开夹具并把除最后一个图层外的所有图层标记通过，返回 (rel, layer_id)。"""
    ids = window._layer_ids_by_file
    pending = [(rel, lid) for rel, lids in ids.items() for lid in lids]
    last_rel, last_lid = pending[-1]
    for rel, lid in pending[:-1]:
        window.task.set_status(rel, lid, PASSED)
    window._switch_file(last_rel)
    _wait_for_file(window, last_rel)
    index = window._layer_ids_by_file[last_rel].index(last_lid)
    window._select_layer_internal(index)
    window._refresh_all_panels()
    assert window._all_reviewed() is False
    return last_rel, last_lid


def test_completion_auto_report_once() -> None:
    """全部监制完成后按设置自动生成返修单：默认开、只触发一次、改动后可再次触发。"""
    from mangaproof.config.settings import Settings

    assert Settings().generate_pdf_on_complete is True, "默认要自动生成"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = _copy_fixtures(root / "chapter01")
        window = MainWindow(SettingsManager(root / "settings.json"))
        window.resize(1200, 800)
        window.show()
        app.processEvents()
        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window.open_folder(folder)
        _wait_for_task(window)
        _open_and_prepare_completion(window)

        calls: list = []
        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ) as info, patch.object(
            window, "_generate_report",
            side_effect=lambda interactive: calls.append(interactive),
        ):
            # Enter（mark_pass）完成最后一个图层 → 提示一次 + 自动生成一次（非交互）
            window.mark_pass()
            app.processEvents()
            assert info.call_count == 1, info.call_count
            assert calls == [False], calls

            # 再按 Enter：不重复弹窗、不重复生成
            window.mark_pass()
            app.processEvents()
            assert info.call_count == 1 and calls == [False]

            # 内容又变了（补问题）→ 复位，再完成时重新生成
            window._commit_new_issue("漏字", "补一条", (10, 10, 40, 40))
            window.mark_pass()
            app.processEvents()
            assert info.call_count == 2 and calls == [False, False]

            # 设置关闭 → 只提示完成，不生成
            window.settings.generate_pdf_on_complete = False
            window._commit_new_issue("漏字", "再来一条", (20, 20, 40, 40))
            window.mark_pass()
            app.processEvents()
            assert info.call_count == 3 and calls == [False, False]

        window.close()
        app.processEvents()

    print("PASS test_completion_auto_report_once")


def test_completion_paths_from_panel_and_fail() -> None:
    """完成监制的各条路径：面板「通过」按钮同样触发；「/」只提示不打断标注。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = _copy_fixtures(root / "chapter01")
        window = MainWindow(SettingsManager(root / "settings.json"))
        window.resize(1200, 800)
        window.show()
        app.processEvents()
        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window.open_folder(folder)
        _wait_for_task(window)

        def titles(mock) -> list:
            return [c.args[1] for c in mock.call_args_list if len(c.args) > 1]

        calls: list = []
        with patch.object(window, "_generate_report",
                          side_effect=lambda interactive: calls.append(interactive)):
            # 1) 最后一个图层用「/」标记未通过 → 不弹完成提示（继续拖框批注），
            #    只给状态栏提示；随后 Enter 才完成
            _open_and_prepare_completion(window)
            with patch.object(
                QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
            ) as info:
                window.mark_fail()
                app.processEvents()
                assert "监制完成" not in titles(info), "未通过时不应立即弹完成提示"
                assert "所有图层已检查" in window.statusBar().currentMessage()
                window.mark_pass()
                app.processEvents()
                assert "监制完成" in titles(info), titles(info)
                assert calls == [False], calls

            # 2) 面板「✓ 通过」按钮完成最后一个图层 → 同样触发完成 + 自动生成
            window._on_status_change_requested(UNREVIEWED)   # 复位该图层
            assert not window._all_reviewed()
            with patch.object(
                QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
            ) as info2:
                window._on_status_change_requested(PASSED)
                app.processEvents()
                assert "监制完成" in titles(info2), "面板通过按钮也应触发完成提示"
                assert calls == [False, False], calls

        window.close()
        app.processEvents()

    print("PASS test_completion_paths_from_panel_and_fail")


def test_issue_scope_setting_and_viewer() -> None:
    """问题红框显示范围：默认「当前页全部问题」，可切换为「仅当前图层」。"""
    from mangaproof.config.settings import DEFAULT_ISSUE_SCOPE, Settings
    from mangaproof.ui.settings_dialog import SettingsDialog

    # 设置项默认值与下拉框读写/复位
    s = Settings()
    assert s.issue_scope == DEFAULT_ISSUE_SCOPE == "page"
    dlg = SettingsDialog(s)
    assert dlg.issue_scope_combo.currentData() == "page"
    lidx = dlg.issue_scope_combo.findData("layer")
    assert lidx >= 0
    dlg.issue_scope_combo.setCurrentIndex(lidx)
    dlg.apply_to(s)
    assert s.issue_scope == "layer"
    dlg._reset_defaults()
    dlg.apply_to(s)
    assert s.issue_scope == DEFAULT_ISSUE_SCOPE

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = _copy_fixtures(root / "chapter01")
        sm = SettingsManager(root / "settings.json")
        window = MainWindow(sm)
        window.resize(1200, 800)
        window.show()
        app.processEvents()
        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window.open_folder(folder)
        _wait_for_task(window)

        # 在 001.psd 的 0/1/2 三个图层各放一个问题（跨图层）
        ids = window._layer_ids_by_file["001.psd"]
        for index in (0, 1, 2):
            window.task.add_issue("001.psd", ids[index], f"layer_{index}",
                                  "漏字", "", (10, 10, 40, 40))
        window._select_layer_internal(1)
        window._refresh_all_panels()
        window._refresh_viewer_issues()
        assert len(window.viewer._issues) == 3, "默认应显示当前页全部问题"

        # 翻到别的图层（同页）：整页范围下红框集合不变
        window._select_layer_internal(2)
        window._refresh_viewer_issues()
        assert len(window.viewer._issues) == 3

        # 切换为「仅当前图层」：只显示当前图层的问题，切图层后随之变化
        window.settings.issue_scope = "layer"
        window._refresh_viewer_issues()
        assert [i.layer_id for i in window.viewer._issues] == [ids[2]]
        window._select_layer_internal(0)
        window._refresh_viewer_issues()
        assert [i.layer_id for i in window.viewer._issues] == [ids[0]]

        # 切回整页范围：恢复显示全部
        window.settings.issue_scope = "page"
        window._refresh_viewer_issues()
        assert len(window.viewer._issues) == 3

        # 翻到没有问题的问题页 → 空；范围设置不影响问题数据
        window._switch_file("10.psd")
        _wait_for_file(window, "10.psd")
        app.processEvents()
        assert window.viewer._issues == []
        assert len(window.task.issues) == 3

        # 设置持久化（保存后重新读取）
        sm.save()
        assert SettingsManager(root / "settings.json").settings.issue_scope == "page"

        window.close()
        app.processEvents()

    print("PASS test_issue_scope_setting_and_viewer")


def _send_wheel(
    viewer: QWidget,
    angle: tuple[int, int] = (0, 0),
    pixel: tuple[int, int] = (0, 0),
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    """构造真实 QWheelEvent 并同步派发。"""
    w, h = viewer.width(), viewer.height()
    ev = QWheelEvent(
        QPointF(w / 2.0, h / 2.0),  # pos
        QPointF(w / 2.0, h / 2.0),  # globalPos
        QPoint(*pixel),             # pixelDelta
        QPoint(*angle),             # angleDelta
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.NoScrollPhase,
        False,                      # inverted
    )
    QApplication.sendEvent(viewer, ev)


def test_wheel_modes() -> None:
    """滚轮交互：裸滚轮默认上下平移/可设置切换为缩放；Ctrl=缩放；
    Alt=左右平移；触控板双指滚（pixelDelta）恒为双轴平移。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = _copy_fixtures(root / "chapter01")
        sm = SettingsManager(root / "settings.json")
        window = MainWindow(sm)
        window.resize(1200, 800)
        window.show()
        app.processEvents()

        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window.open_folder(folder)
        _wait_for_task(window)
        app.processEvents()

        viewer = window.viewer
        cam = viewer.camera
        assert viewer._doc is not None

        cx0, cy0, zoom0 = cam.center_x, cam.center_y, cam.zoom

        # 默认：裸鼠标滚轮 → 上下平移，不缩放
        _send_wheel(viewer, angle=(0, 120))
        assert cam.zoom == zoom0
        assert cam.center_x == cx0
        assert cam.center_y < cy0  # 滚轮向上 → 视图向上

        # Ctrl+滚轮 → 缩放（锚点=视口中心 → 相机中心不动）
        cx1, cy1, zoom1 = cam.center_x, cam.center_y, cam.zoom
        _send_wheel(viewer, angle=(0, 120), modifiers=Qt.KeyboardModifier.ControlModifier)
        assert cam.zoom == pytest.approx(zoom1 * 1.25)
        assert cam.center_x == cx1 and cam.center_y == cy1

        # Alt+滚轮 → 左右平移（纵向刻度映射为横向）
        cx2, cy2, zoom2 = cam.center_x, cam.center_y, cam.zoom
        _send_wheel(viewer, angle=(0, 120), modifiers=Qt.KeyboardModifier.AltModifier)
        assert cam.zoom == zoom2
        assert cam.center_x < cx2
        assert cam.center_y == cy2

        # 设置切换：裸滚轮 → 缩放
        viewer.set_wheel_mode("zoom")
        cx3, cy3, zoom3 = cam.center_x, cam.center_y, cam.zoom
        _send_wheel(viewer, angle=(0, 120))
        assert cam.zoom == pytest.approx(zoom3 * 1.25)
        assert cam.center_x == cx3 and cam.center_y == cy3

        # 触控板双指滚（pixelDelta）：无论 wheel_mode 如何，恒为双轴平移
        cx4, cy4, zoom4 = cam.center_x, cam.center_y, cam.zoom
        _send_wheel(viewer, pixel=(100, 60))
        assert cam.zoom == zoom4
        assert cam.center_x < cx4 and cam.center_y < cy4

        # 恢复默认模式后，裸滚轮再次为上下平移
        viewer.set_wheel_mode("pan")
        cx5, cy5, zoom5 = cam.center_x, cam.center_y, cam.zoom
        _send_wheel(viewer, angle=(0, -120))
        assert cam.zoom == zoom5
        assert cam.center_x == cx5 and cam.center_y > cy5

        window.close()

    print("PASS test_wheel_modes")


def test_layer_outline_geometry_and_setting() -> None:
    """当前图层蓝色虚线边界框：几何按世界坐标 LTRB 映射 + 可关闭。

    历史缺陷：layer_visual_bounds() 返回 (left, top, right, bottom)，
    Viewer 却按 (x, y, w, h) 解包，右下角被画到 (left+right, top+bottom)——
    左上角正确、右下角严重外扩（bg 层因 left=top=0 恰好无误差而掩盖了问题）。
    """
    from mangaproof.camera.camera import Camera
    from mangaproof.camera.centering import layer_visual_bounds
    from mangaproof.config.settings import DEFAULT_SHOW_LAYER_OUTLINE, Settings
    from mangaproof.ui.settings_dialog import SettingsDialog
    from mangaproof.ui.viewer_widget import outline_screen_rect

    # 1) 纯几何：LTRB 的两个角点分别映射（zoom=2, center=(200,300), 视口 800x600）
    cam = Camera(center_x=200.0, center_y=300.0, zoom=2.0)
    rect = outline_screen_rect(cam, (100, 200, 300, 400), 800, 600)
    assert rect is not None
    x0, y0 = cam.world_to_screen(100, 200, 800, 600)
    x1, y1 = cam.world_to_screen(300, 400, 800, 600)
    assert (rect.left(), rect.top()) == (x0, y0)
    assert (rect.right(), rect.bottom()) == (x1, y1)
    assert (rect.width(), rect.height()) == (400.0, 400.0)   # (300-100)*2
    # 旧误解（x,y,w,h）会得到 (x0, y0, 300*2, 400*2)——右下角恰好翻倍
    assert rect.right() != x0 + 300 * 2 and rect.bottom() != y0 + 400 * 2
    # 退化输入不绘制
    assert outline_screen_rect(cam, None, 800, 600) is None
    assert outline_screen_rect(cam, (100, 200, 100, 400), 800, 600) is None
    assert outline_screen_rect(cam, (100, 200, 300, 200), 800, 600) is None

    # 2) 设置项：默认显示，对话框读写与复位
    s = Settings()
    assert s.show_layer_outline is DEFAULT_SHOW_LAYER_OUTLINE is True
    dlg = SettingsDialog(s)
    assert dlg.layer_outline_check.isChecked() is True
    dlg.layer_outline_check.setChecked(False)
    dlg.apply_to(s)
    assert s.show_layer_outline is False
    dlg._reset_defaults()
    dlg.apply_to(s)
    assert s.show_layer_outline is DEFAULT_SHOW_LAYER_OUTLINE

    # 3) 端到端：真实窗口 → 虚线框随图层/设置更新
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = _copy_fixtures(root / "chapter01")
        sm = SettingsManager(root / "settings.json")
        window = MainWindow(sm)
        window.resize(1200, 800)
        window.show()
        app.processEvents()
        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window.open_folder(folder)
        _wait_for_task(window)
        _wait_for_file(window, "001.psd")
        app.processEvents()

        # 取一个 left/top 明显不为 0 的图层（对话气泡）
        doc = window.current_doc
        index = next(
            i for i, info in enumerate(doc.layers)
            if (vb := layer_visual_bounds(info)) and vb[0] > 0 and vb[1] > 0
        )
        info = doc.layers[index]
        window._select_layer_internal(index)
        app.processEvents()

        expected = layer_visual_bounds(info)
        assert window.viewer.layer_outline == expected
        # 世界坐标下右下角必须是 right/bottom，而不是 left+right/top+bottom
        left, top, right, bottom = expected
        assert window.viewer.layer_outline[2] == right < left + right
        assert window.viewer.layer_outline[3] == bottom < top + bottom

        # 绘制矩形：与视觉边界两端点对齐，且不超出画布
        vw, vh = window.viewer.width(), window.viewer.height()
        drawn = outline_screen_rect(window.viewer.camera, expected, vw, vh)
        assert drawn is not None
        cw, ch = doc.size
        canvas = outline_screen_rect(window.viewer.camera, (0, 0, cw, ch), vw, vh)
        assert canvas is not None
        assert drawn.width() <= canvas.width() + 1
        assert drawn.height() <= canvas.height() + 1

        # 关闭开关：立即不再绘制（定位行为不受影响）
        window.settings.show_layer_outline = False
        window._refresh_viewer_outline()
        assert window.viewer.layer_outline is None
        assert window.viewer.camera.zoom > 0
        # 切图层仍保持关闭
        window._select_layer_internal((index + 1) % len(doc.layers))
        assert window.viewer.layer_outline is None

        # 重新打开：当前图层虚线框恢复
        window.settings.show_layer_outline = True
        window._refresh_viewer_outline()
        current = doc.layers[window._current_index]
        assert window.viewer.layer_outline == layer_visual_bounds(current)

        # 设置持久化
        sm.save()
        assert (
            SettingsManager(root / "settings.json").settings.show_layer_outline is True
        )

        window.close()
        app.processEvents()

    print("PASS test_layer_outline_geometry_and_setting")


def test_layer_outline_paint_path() -> None:
    """真实 paintEvent 像素校验：蓝虚线框画在视觉边界上，右下角不外扩。

    历史缺陷在绘制环节（把 LTRB 当 x,y,w,h），只看 _layer_outline 数据
    是抓不到的：这里把纯灰画布渲染出来，用蓝色像素的包围盒反推实际画的框。
    """
    import numpy as np
    from PySide6.QtGui import QImage

    from mangaproof.ui.viewer_widget import ViewerWidget

    class _FakeDoc:
        """仅够 Viewer 绘制用：纯灰 merged 画布，无 bg。"""

        def __init__(self, w: int, h: int):
            self._w, self._h = w, h
            self._arr = np.full((h, w, 4), 128, dtype=np.uint8)

        def merged_np(self):
            return self._arr

        def bg_image(self):
            return None

    viewer = ViewerWidget()
    viewer.set_document(_FakeDoc(400, 600))
    viewer.resize(800, 600)
    # 视口 800x600、center=(200,300)、zoom=1 → 世界坐标 1:1 映射到屏幕
    viewer.camera.center_on(200.0, 300.0)
    viewer.camera.zoom = 1.0
    viewer.set_layer_outline((100, 200, 300, 400))
    app.processEvents()

    img: QImage = viewer.grab().toImage().convertToFormat(
        QImage.Format.Format_RGB32
    )
    assert (img.width(), img.height()) == (800, 600), (img.width(), img.height())
    buf = np.frombuffer(img.constBits(), dtype=np.uint8)
    buf = buf.reshape(img.height(), img.bytesPerLine() // 4, 4)[:, : img.width(), :]
    r, g, b = buf[:, :, 2], buf[:, :, 1], buf[:, :, 0]   # RGB32 内存序为 BGRA
    blue = (b > r + 40) & (b > 120)
    assert blue.any(), "未画出蓝色虚线框"
    ys, xs = np.nonzero(blue)
    bbox = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))

    def near(a, b_, tol=2):
        return abs(a - b_) <= tol

    # 期望：世界 (100,200)-(300,400) → 屏幕 (300,200)-(500,400)
    assert near(bbox[0], 300) and near(bbox[1], 200), bbox
    assert near(bbox[2], 500) and near(bbox[3], 400), bbox
    # 旧误解会画到 (300,200)-(600,600)：右下角必须明显更小
    assert bbox[2] < 550 and bbox[3] < 450, bbox

    # 关闭显示（outline=None）后不再有蓝色像素
    viewer.set_layer_outline(None)
    app.processEvents()
    img = viewer.grab().toImage().convertToFormat(QImage.Format.Format_RGB32)
    buf = np.frombuffer(img.constBits(), dtype=np.uint8)
    buf = buf.reshape(img.height(), img.bytesPerLine() // 4, 4)[:, : img.width(), :]
    r, g, b = buf[:, :, 2], buf[:, :, 1], buf[:, :, 0]
    assert not ((b > r + 40) & (b > 120)).any(), "关闭后仍有蓝色虚线框"

    print("PASS test_layer_outline_paint_path")


def test_settings_dialog_scroll_area() -> None:
    """设置对话框：分组可滚动、底部按钮常驻；下拉框聚焦与否都不响应滚轮。

    历史问题：分组全部平铺，minimumSizeHint 高达 717px，768p 笔记本上
    按钮会被挤出屏幕且无法缩小。
    """
    from mangaproof.config.settings import Settings
    from mangaproof.ui.settings_dialog import SettingsDialog
    from mangaproof.ui.widgets import NoWheelComboBox

    dlg = SettingsDialog(Settings())
    dlg.resize(560, 480)
    dlg.show()
    app.processEvents()

    # 1) 结构：分组在滚动区内，底部按钮在滚动区外（任何高度都可见可点）
    sa = dlg.scroll_area
    body = sa.widget()
    assert body is not None
    for group_child in (
        dlg.ratio_combo, dlg.issue_scope_combo, dlg.layer_outline_check,
        dlg.memory_policy_combo, dlg.report_hide_clean_check, dlg.kb_button,
    ):
        assert body.isAncestorOf(group_child), group_child
    assert not body.isAncestorOf(dlg.button_box)
    assert not sa.isAncestorOf(dlg.button_box)

    # 2) 小屏可用：可缩到 480px 以内，且内容溢出时出现滚动条
    assert dlg.minimumSizeHint().height() < 480, dlg.minimumSizeHint()
    assert dlg.height() == 480, dlg.size()
    vbar = sa.verticalScrollBar()
    assert vbar.maximum() > 0, "内容高于视口时应有滚动条"
    assert dlg.button_box.geometry().bottom() <= dlg.height() + 1
    dlg.resize(560, 380)
    app.processEvents()
    assert dlg.height() == 380, dlg.size()
    assert dlg.button_box.geometry().bottom() <= dlg.height() + 1

    # 3) 滚到底部：最后一组（快捷键与滚轮）可达
    vbar.setValue(vbar.maximum())
    app.processEvents()
    vp = sa.viewport()
    assert 0 <= dlg.wheel_mode_combo.mapTo(vp, QPoint(0, 0)).y() < vp.height()

    # 4) 下拉框不响应滚轮：聚焦与否都不改值（原生 QComboBox 会直接改值）
    def wheel_event(dy: int = -120) -> QWheelEvent:
        p = QPointF(10.0, 10.0)
        return QWheelEvent(
            p, p, QPoint(0, 0), QPoint(0, dy),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )

    assert isinstance(dlg.ratio_combo, NoWheelComboBox)
    combo = dlg.issue_scope_combo
    for focused in (False, True):
        if focused:
            combo.setFocus()
            app.processEvents()
        else:
            combo.clearFocus()
            app.processEvents()
        assert combo.hasFocus() is focused
        before = combo.currentIndex()
        for dy in (-120, 120):          # 两个方向都不动
            ev = wheel_event(dy)
            QApplication.sendEvent(combo, ev)
            assert not ev.isAccepted(), f"应忽略滚轮（focused={focused}）"
            assert combo.currentIndex() == before, (focused, dy)

    # 对照：原生 QComboBox 未聚焦也会被滚轮改值（说明该保护确有作用）
    from PySide6.QtWidgets import QComboBox as _QComboBox

    plain = _QComboBox()
    plain.addItems(["a", "b", "c"])
    plain.clearFocus()
    ev = wheel_event()
    QApplication.sendEvent(plain, ev)
    assert ev.isAccepted() and plain.currentIndex() == 1

    # 键盘仍可正常改值（禁用滚轮不等于禁用该控件）
    combo.setFocus()
    app.processEvents()
    before = combo.currentIndex()
    QApplication.sendEvent(
        combo,
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier),
    )
    app.processEvents()
    assert combo.currentIndex() != before, "键盘方向键应仍可改值"

    dlg.close()
    app.processEvents()

    print("PASS test_settings_dialog_scroll_area")


def test_no_wheel_combo_app_wide() -> None:
    """全应用下拉框统一禁用滚轮改值：问题类型 / 返修单选项 / 主界面显示比例。"""
    from mangaproof.ui.dialogs import IssueDialog, ReportDialog
    from mangaproof.ui.widgets import NoWheelComboBox

    def wheel(combo, dy: int = -120) -> QWheelEvent:
        p = QPointF(10.0, 10.0)
        ev = QWheelEvent(
            p, p, QPoint(0, 0), QPoint(0, dy),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )
        QApplication.sendEvent(combo, ev)
        return ev

    def assert_wheel_inert(combo, focused: bool) -> None:
        if focused:
            combo.window().raise_()
            combo.window().activateWindow()
            app.processEvents()
            combo.setFocus()
        else:
            combo.clearFocus()
        app.processEvents()
        assert combo.hasFocus() is focused
        before = combo.currentIndex()
        for dy in (-120, 120):
            assert not wheel(combo, dy).isAccepted(), (type(combo).__name__, focused, dy)
            assert combo.currentIndex() == before, (type(combo).__name__, focused, dy)

    issue_dlg = IssueDialog(["漏字", "错字", "其他"])
    report_dlg = ReportDialog("返修单", "chapter01", False)
    issue_dlg.show()
    report_dlg.show()
    app.processEvents()
    assert isinstance(issue_dlg.type_combo, NoWheelComboBox)
    assert isinstance(report_dlg.image_format_combo, NoWheelComboBox)
    assert isinstance(report_dlg.quality_combo, NoWheelComboBox)
    for combo in (issue_dlg.type_combo, report_dlg.image_format_combo):
        assert_wheel_inert(combo, focused=False)
        assert_wheel_inert(combo, focused=True)
    issue_dlg.close()
    report_dlg.close()

    with tempfile.TemporaryDirectory() as tmp:
        window = MainWindow(SettingsManager(Path(tmp) / "settings.json"))
        window.show()
        app.processEvents()
        ratio = window.ratio_combo   # 工具栏「显示比例」
        assert isinstance(ratio, NoWheelComboBox)
        ratio.setEnabled(True)       # 未开任务时该控件是禁用的，这里只测焦点行为
        saved = window.settings.layer_display_ratio
        assert_wheel_inert(ratio, focused=False)
        assert_wheel_inert(ratio, focused=True)
        assert window.settings.layer_display_ratio == saved, "滚轮不得改动设置值"
        window.close()
        app.processEvents()

    print("PASS test_no_wheel_combo_app_wide")


def test_all_default_shortcuts_fire() -> None:
    """快捷键全量体检：默认键位全部真的绑上、互不冲突、按下就对得上动作。

    历史缺陷：①「红框模式 R」与问题类型「漏字 R」撞车 → Qt 判歧义，
    两个都不触发（用户按 R 完全没反应）；②工具栏上印着 Ctrl+O /
    Ctrl+Shift+O / Ctrl+R 的按钮其实只写了文本、没绑快捷键。
    """
    from collections import defaultdict

    from PySide6.QtGui import QKeySequence
    from PySide6.QtTest import QTest

    from mangaproof.config.settings import (
        DEFAULT_ISSUE_TYPES,
        DEFAULT_KEYBINDINGS,
        shortcut_conflicts,
    )

    # 1) 默认配置自身零冲突
    assert DEFAULT_KEYBINDINGS["redraw_mode"] == "R", "红框模式应保持 R"
    assert shortcut_conflicts(DEFAULT_KEYBINDINGS, DEFAULT_ISSUE_TYPES) == {}

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = _copy_fixtures(root / "chapter01")
        window = MainWindow(SettingsManager(root / "settings.json"))
        window.resize(1200, 800)
        window.show()
        app.processEvents()
        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window.open_folder(folder)
        _wait_for_task(window)
        window.activateWindow()
        app.processEvents()

        # 2) 结构：每个默认键位都必须绑上，且没有重复序列
        groups: dict = defaultdict(list)
        for sc in window._shortcuts:
            groups[sc.key().toString()].append(sc)
        duplicates = {k: len(v) for k, v in groups.items() if len(v) > 1}
        assert duplicates == {}, f"存在同键多绑（Qt 会判歧义）: {duplicates}"

        expected = set(DEFAULT_KEYBINDINGS.values()) | {
            t["key"] for t in DEFAULT_ISSUE_TYPES if t["key"]
        }
        missing = expected - set(groups)
        assert missing == set(), f"这些默认快捷键没有绑定：{sorted(missing)}"
        # 按钮上印出来的快捷键必须真的可用（不能只印不绑）
        for action in ("open_psd", "open_folder", "generate_report", "save_task"):
            seq = DEFAULT_KEYBINDINGS[action]
            assert seq in groups, f"按钮标注了 {seq}（{action}）却没有绑定"

        # 3) 逐个真按：只有对应序列触发 activated，绝不出现歧义
        fired: list = []
        for sc in window._shortcuts:
            seq = sc.key().toString()
            sc.activated.connect(lambda s=seq: fired.append(s))
            sc.activatedAmbiguously.connect(lambda s=seq: fired.append(f"{s}(歧义)"))
        opened: list = []
        with patch.object(mw.IssueDialog, "exec", lambda self: 0), \
             patch.object(mw.ReportDialog, "exec", lambda self: 0), \
             patch.object(
                 QFileDialog, "getOpenFileName",
                 lambda *a, **k: (opened.append("psd"), ("", ""))[1],
             ), \
             patch.object(
                 QFileDialog, "getExistingDirectory",
                 lambda *a, **k: (opened.append("folder"), "")[1],
             ):
            # 先测不触发异步切换的键，导航键最后单独测
            order = [s for s in groups if s not in ("Up", "Down")]
            for seq in order:
                fired.clear()
                combo = QKeySequence(seq)[0]
                QTest.keyClick(window, combo.key(), combo.keyboardModifiers())
                app.processEvents()
                assert fired == [seq], f"{seq} 应恰好触发自身，实际 {fired}"
            window._compare.interrupt()
            # 导航键：Up/Down 切换 PSD（异步），逐个等待落定
            for seq in ("Down", "Up"):
                fired.clear()
                combo = QKeySequence(seq)[0]
                QTest.keyClick(window, combo.key(), combo.keyboardModifiers())
                app.processEvents()
                assert fired == [seq], f"{seq} 应恰好触发自身，实际 {fired}"
                _wait_for_file(window, window._current_file)
            window._compare.interrupt()

        window.close()
        app.processEvents()

    print("PASS test_all_default_shortcuts_fire")


def test_shortcut_conflict_detection_and_fixes() -> None:
    """冲突检测（配置期 + 运行期提示）与旧配置自动修复。"""
    import json

    from PySide6.QtGui import QKeySequence
    from PySide6.QtTest import QTest

    from mangaproof.config.settings import (
        DEFAULT_ISSUE_TYPES,
        DEFAULT_KEYBINDINGS,
        Settings,
        shortcut_conflicts,
    )
    from mangaproof.ui.settings_dialog import KeybindingsDialog

    # 1) 冲突检测：同一序列绑两个动作能被查出来，且大小写/空格归一化
    kb = dict(DEFAULT_KEYBINDINGS)
    types = [dict(t) for t in DEFAULT_ISSUE_TYPES]
    assert shortcut_conflicts(kb, types) == {}
    kb["redraw_mode"] = "R"
    for item in types:
        if item["name"] == "漏字":
            item["key"] = "r"          # 小写也应视为冲突
    conflicts = shortcut_conflicts(kb, types)
    assert list(conflicts) == ["R"], conflicts
    assert ("核心快捷键", "红框模式") in conflicts["R"]
    assert ("问题类型", "漏字") in conflicts["R"]
    # 空绑定（键位留空 = 不绑）不算冲突
    for item in types:
        if item["name"] == "漏字":
            item["key"] = ""
    assert shortcut_conflicts(kb, types) == {}

    # 2) 旧版 settings.json（漏字=R 撞红框模式）载入时自动让位到 P
    with tempfile.TemporaryDirectory() as tmp:
        legacy = Path(tmp) / "legacy.json"
        legacy.write_text(json.dumps({
            "keybindings": {"redraw_mode": "R"},
            "issue_types": [{"name": "漏字", "key": "R"}, {"name": "错字", "key": "T"}],
        }, ensure_ascii=False), encoding="utf-8")
        s = SettingsManager(legacy).settings
        assert s.issue_types[0] == {"name": "漏字", "key": "P"}
        assert s.keybindings["redraw_mode"] == "R"
        assert s.shortcut_conflicts() == {}

        # 用户已自行改绑（redraw_mode 不是 R）→ 尊重用户，不动
        custom = Path(tmp) / "custom.json"
        custom.write_text(json.dumps({
            "keybindings": {"redraw_mode": "F8"},
            "issue_types": [{"name": "漏字", "key": "R"}],
        }, ensure_ascii=False), encoding="utf-8")
        s2 = SettingsManager(custom).settings
        assert s2.issue_types[0]["key"] == "R", "用户自定配置不应被擅自改写"
        assert s2.keybindings["redraw_mode"] == "F8"

    # 3) 快捷键子对话框：即时提示冲突，且冲突时不允许保存
    s3 = Settings()
    kb_dlg = KeybindingsDialog(s3)
    kb_dlg.show()
    app.processEvents()
    assert kb_dlg.conflicts() == {}
    assert kb_dlg.conflict_label.isVisible() is False
    row = next(
        i for i, item in enumerate(s3.issue_types) if item["name"] == "漏字"
    )
    kb_dlg._issue_edits[row].setKeySequence(QKeySequence("R"))
    app.processEvents()
    assert list(kb_dlg.conflicts()) == ["R"]
    assert kb_dlg.conflict_label.isVisible() is True
    assert "漏字" in kb_dlg.conflict_label.text()
    with patch.object(
        QMessageBox, "warning", return_value=QMessageBox.StandardButton.Ok
    ) as warn:
        kb_dlg.accept()
        assert warn.called, "冲突时应弹窗拦截"
    assert kb_dlg.result() != KeybindingsDialog.DialogCode.Accepted
    # 改回不冲突的键 → 可正常保存
    kb_dlg._issue_edits[row].setKeySequence(QKeySequence("P"))
    app.processEvents()
    assert kb_dlg.conflicts() == {}
    assert kb_dlg.conflict_label.isVisible() is False
    kb_dlg.accept()
    assert kb_dlg.result() == KeybindingsDialog.DialogCode.Accepted
    # 「恢复默认快捷键」不产生冲突
    kb_dlg._reset_defaults()
    assert kb_dlg.conflicts() == {}

    # 4) 运行期：真按下冲突键时明确提示撞在哪（而不是静默无反应）
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = _copy_fixtures(root / "chapter01")
        window = MainWindow(SettingsManager(root / "settings.json"))
        window.show()
        app.processEvents()
        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window.open_folder(folder)
        _wait_for_task(window)
        window.activateWindow()
        app.processEvents()

        for item in window.settings.issue_types:
            if item["name"] == "漏字":
                item["key"] = "R"
        window.settings.keybindings["redraw_mode"] = "R"
        window._rebuild_shortcuts()
        app.processEvents()
        fired: list = []
        for sc in window._shortcuts:
            seq = sc.key().toString()
            sc.activated.connect(lambda s=seq: fired.append(s))
            sc.activatedAmbiguously.connect(lambda s=seq: fired.append(f"{s}(歧义)"))
        QTest.keyClick(window, Qt.Key.Key_R)
        app.processEvents()
        assert "R(歧义)" in fired, fired
        assert "R" not in [f for f in fired if f != "R(歧义)"], "冲突键不应触发动作"
        msg = window.statusBar().currentMessage()
        assert "红框模式" in msg and "漏字" in msg, msg
        # 冲突配置重建后仍应提示（便于用户发现）
        window.close()
        app.processEvents()

    print("PASS test_shortcut_conflict_detection_and_fixes")


def test_shortcut_actions_take_effect() -> None:
    """按下的快捷键要真的产生对应动作（不只是"触发了信号"）。"""
    from PySide6.QtTest import QTest

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = _copy_fixtures(root / "chapter01")
        window = MainWindow(SettingsManager(root / "settings.json"))
        window.resize(1200, 800)
        window.show()
        app.processEvents()
        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window.open_folder(folder)
        _wait_for_task(window)
        window.activateWindow()
        app.processEvents()

        calls: list = []
        with patch.object(
            mw.IssueDialog, "exec",
            lambda self: (calls.append(("issue", self.type_combo.currentText())), 0)[1],
        ), patch.object(
            mw.ReportDialog, "exec",
            lambda self: (calls.append(("report", self.hide_clean_files())), 0)[1],
        ), patch.object(
            QFileDialog, "getOpenFileName",
            lambda *a, **k: (calls.append(("open_psd", None)), ("", ""))[1],
        ), patch.object(
            QFileDialog, "getExistingDirectory",
            lambda *a, **k: (calls.append(("open_folder", None)), "")[1],
        ):
            # R：进入拖框（红框）模式
            assert window.viewer.redraw_mode is False
            QTest.keyClick(window, Qt.Key.Key_R)
            app.processEvents()
            assert window.viewer.redraw_mode is True, "R 应进入红框模式"
            QTest.keyClick(window, Qt.Key.Key_R)
            app.processEvents()
            assert window.viewer.redraw_mode is False, "R 再按一次应退出"

            # P：漏字（原 R 撞车后挪到 P）
            QTest.keyClick(window, Qt.Key.Key_P)
            app.processEvents()
            assert window.viewer.pending_type == "漏字"

            # Esc：取消待创建的问题
            QTest.keyClick(window, Qt.Key.Key_Escape)
            app.processEvents()
            assert window.viewer.pending_type is None

            # Ctrl+Enter：自定义批注对话框
            calls.clear()
            QTest.keyClick(window, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
            app.processEvents()
            assert calls and calls[0][0] == "issue", calls

            # 工具栏上标注的三个快捷键必须真的打开对应对话框
            calls.clear()
            QTest.keyClick(window, Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier)
            app.processEvents()
            assert calls == [("open_psd", None)], calls

            calls.clear()
            QTest.keyClick(
                window, Qt.Key.Key_O,
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
            )
            app.processEvents()
            assert calls == [("open_folder", None)], calls

            calls.clear()
            QTest.keyClick(window, Qt.Key.Key_R, Qt.KeyboardModifier.ControlModifier)
            app.processEvents()
            assert [c[0] for c in calls] == ["report"], calls

        # Ctrl+S：保存任务（写入进度文件 + 状态栏「已保存」）
        window._mark_dirty()
        assert window.save_label.text() == "未保存"
        QTest.keyClick(window, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        app.processEvents()
        assert window.save_label.text().startswith("已保存"), window.save_label.text()
        progress = persistence.progress_path_for_folder(folder)
        assert progress.exists(), progress

        window.close()
        app.processEvents()

    print("PASS test_shortcut_actions_take_effect")


def _drag_rect(viewer, wx0: float, wy0: float, wx1: float, wy1: float) -> None:
    """在 Viewer 上模拟一次真实拖框（世界坐标 → 屏幕坐标 → 鼠标事件）。"""
    p0 = viewer.camera.world_to_screen(wx0, wy0, viewer.width(), viewer.height())
    p1 = viewer.camera.world_to_screen(wx1, wy1, viewer.width(), viewer.height())
    for kind, pt in (
        (QEvent.Type.MouseButtonPress, p0),
        (QEvent.Type.MouseMove, p1),
        (QEvent.Type.MouseButtonRelease, p1),
    ):
        ev = QMouseEvent(
            kind, QPointF(*pt), QPointF(*pt),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(viewer, ev)
        app.processEvents()


def test_one_shot_annotation_and_continuous_toggle() -> None:
    """默认"标完一个即退出"，右侧「连续标注」按钮可切回连续标注。

    覆盖：红框模式（R / 按钮）与问题类型快捷键两条路径；按钮勾选状态可见；
    开关持久化到 settings.json。
    """
    from PySide6.QtTest import QTest

    from mangaproof.config.settings import (
        DEFAULT_CONTINUOUS_ANNOTATION,
        Settings,
    )
    from mangaproof.ui.issue_panel import IssuePanel

    # 1) 默认值与按钮外观：可勾选、默认关、状态一眼可见（文字 + 高亮样式）
    assert DEFAULT_CONTINUOUS_ANNOTATION is False
    assert Settings().continuous_annotation is False
    panel = IssuePanel()
    panel.resize(300, 400)
    panel.show()
    app.processEvents()
    assert panel.continuous_btn.isCheckable() is True
    assert panel.continuous() is False
    assert panel.continuous_btn.text() == "连续标注"
    assert "关闭" in panel.continuous_btn.toolTip()
    # 与「添加问题」同一行、位于其右侧（左按钮 / 右开关）
    assert panel.continuous_btn.parentWidget() is panel.add_btn.parentWidget()
    assert panel.add_btn.geometry().right() <= panel.continuous_btn.geometry().left()
    assert "添加问题" in panel.add_btn.text()
    panel.set_continuous(True)
    assert panel.continuous() is True
    assert panel.continuous_btn.text().startswith("✓")
    assert "开启" in panel.continuous_btn.toolTip()
    assert ":checked" in panel.continuous_btn.styleSheet(), "勾选态需要高亮样式"
    panel.close()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = _copy_fixtures(root / "chapter01")
        sm = SettingsManager(root / "settings.json")
        window = MainWindow(sm)
        window.resize(1200, 800)
        window.show()
        app.processEvents()
        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window.open_folder(folder)
        _wait_for_task(window)
        window.activateWindow()
        app.processEvents()
        viewer = window.viewer
        assert viewer.redraw_mode is False
        assert window.issue_panel.continuous() is False

        accepted = patch.object(
            mw.IssueDialog, "exec", return_value=mw.IssueDialog.DialogCode.Accepted
        )
        values = patch.object(
            mw.IssueDialog, "result_values", return_value=("漏字", "")
        )
        with accepted, values:
            # 2) 红框模式（R 快捷键）：标完一个自动退出
            QTest.keyClick(window, Qt.Key.Key_R)
            app.processEvents()
            assert viewer.redraw_mode is True
            assert "自动退出" in window.issue_panel.hint_label.text()
            _drag_rect(viewer, 50, 50, 150, 110)
            assert len(window.task.issues) == 1
            assert viewer.redraw_mode is False, "默认标完一个应自动退出"
            assert window.issue_panel.hint_label.text() == ""

            # 3) 「添加问题」按钮：同样一标一退
            window.issue_panel.add_btn.click()
            app.processEvents()
            assert viewer.redraw_mode is True
            _drag_rect(viewer, 60, 60, 160, 120)
            assert len(window.task.issues) == 2
            assert viewer.redraw_mode is False

            # 4) 问题类型快捷键：一标一退（类型不再保持）
            QTest.keyClick(window, Qt.Key.Key_P)      # 漏字
            app.processEvents()
            assert viewer.pending_type == "漏字"
            assert "自动退出" in window.issue_panel.hint_label.text()
            _drag_rect(viewer, 70, 70, 170, 130)
            assert len(window.task.issues) == 3
            assert viewer.pending_type is None
            assert viewer.redraw_mode is False

            # 5) 打开「连续标注」：两条路径都保持待标状态
            window.issue_panel.continuous_btn.click()
            app.processEvents()
            assert window.issue_panel.continuous() is True
            assert window.settings.continuous_annotation is True

            QTest.keyClick(window, Qt.Key.Key_R)
            app.processEvents()
            assert "连续标注中" in window.issue_panel.hint_label.text()
            _drag_rect(viewer, 80, 80, 180, 140)
            assert len(window.task.issues) == 4
            assert viewer.redraw_mode is True, "连续标注应保持红框模式"
            _drag_rect(viewer, 90, 90, 190, 150)
            assert len(window.task.issues) == 5

            QTest.keyClick(window, Qt.Key.Key_Escape)
            app.processEvents()
            assert viewer.redraw_mode is False

            QTest.keyClick(window, Qt.Key.Key_P)      # 漏字
            app.processEvents()
            assert viewer.pending_type == "漏字"
            _drag_rect(viewer, 100, 100, 200, 160)
            assert len(window.task.issues) == 6
            assert viewer.pending_type == "漏字", "连续标注应保持同一类型"
            _drag_rect(viewer, 110, 110, 210, 170)
            assert len(window.task.issues) == 7
            assert window.task.issues[6].type == "漏字"
            QTest.keyClick(window, Qt.Key.Key_Escape)
            app.processEvents()
            assert viewer.pending_type is None

            # 6) 关掉开关：恢复一标一退
            window.issue_panel.continuous_btn.click()
            app.processEvents()
            assert window.settings.continuous_annotation is False
            QTest.keyClick(window, Qt.Key.Key_R)
            app.processEvents()
            _drag_rect(viewer, 120, 120, 220, 180)
            assert len(window.task.issues) == 8
            assert viewer.redraw_mode is False

        # 7) 持久化：开关状态写入 settings.json，重启后按钮直接是勾选态
        window.issue_panel.continuous_btn.click()
        app.processEvents()
        sm.save()
        assert (
            SettingsManager(root / "settings.json").settings.continuous_annotation is True
        )
        reborn = MainWindow(SettingsManager(root / "settings.json"))
        app.processEvents()
        assert reborn.settings.continuous_annotation is True
        assert reborn.issue_panel.continuous() is True, "启动时应恢复勾选态"
        assert reborn.issue_panel.continuous_btn.text().startswith("✓")
        reborn.close()
        app.processEvents()

        window.close()
        app.processEvents()

    print("PASS test_one_shot_annotation_and_continuous_toggle")


def test_issue_panel_long_layer_name() -> None:
    """当前图层问题面板：超长图层名单行省略显示（不撑宽、不换行）。"""
    from mangaproof.ui.issue_panel import IssuePanel

    panel = IssuePanel()
    long_name = "同一段文字内容被用作图层名" * 50
    panel.set_current(long_name, UNREVIEWED, [])
    assert panel.layer_name_label.text() == f"图层：{long_name}"
    assert panel.layer_name_label.wordWrap() is False
    assert panel.layer_name_label.sizePolicy().horizontalPolicy() == (
        QSizePolicy.Policy.Ignored
    )
    assert panel.layer_name_label.toolTip() == f"图层：{long_name}"

    print("PASS test_issue_panel_long_layer_name")


if __name__ == "__main__":
    import traceback

    failed = 0
    for name, fn in [
        (k, v) for k, v in sorted(globals().items()) if k.startswith("test_")
    ]:
        try:
            fn()
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    sys.exit(1 if failed else 0)
