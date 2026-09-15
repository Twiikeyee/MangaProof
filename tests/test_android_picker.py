"""Android 原生选择器（共享文件协议）的本地可测部分。

真机行为（系统选择器 UI）无法在桌面复现，但**协议的两端契约**可以：Java 侧的
动作等价于"往 `result.txt` 原子写一行结果"，测试里用 QTimer 延时模拟它，
从而验证 Python 侧的写出命令、轮询、解析、超时与清理。

对应实现：`mangaproof/storage/android_picker.py`、
`packaging/android/java/com/mangaproof/picker/PickerActivity.java`。
"""

from __future__ import annotations

import os

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QFileDialog

from mangaproof.storage import android_picker, picker

#: QEventLoop / QTimer 必须在有 QCoreApplication 实例的进程里使用。
#: **刻意不在模块级创建**：在 pytest 收集阶段抢先创建 QApplication 会破坏
#: test_gui_smoke 里依赖窗口激活/焦点的用例（现象是 combo.hasFocus() 恒为 False），
#: 已在本地用对照实验确认；改为用例执行时按需获取。


@pytest.fixture()
def qt_app():
    """确保有 QApplication（用例执行时才创建/复用）。"""
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def picker_dir(tmp_path, monkeypatch, qt_app):
    """把协议目录指到临时目录（等价于 Java 侧发布 MANGAPROOF_PICKER_DIR）。"""
    monkeypatch.setenv("MANGAPROOF_PICKER_DIR", str(tmp_path))
    return tmp_path


def _answer_after(tmp_path, text: str, delay_ms: int = 0, name: str = "result.txt") -> None:
    """模拟 Java 侧：延时后原子写出结果（先写 .tmp 再 rename）。"""
    def _write() -> None:
        tmp = tmp_path / "result.tmp"
        tmp.write_text(text + "\n", encoding="utf-8")
        os.replace(tmp, tmp_path / name)

    QTimer.singleShot(delay_ms, _write)


# --------------------------------------------------------------------- 目录

def test_picker_dir_prefers_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MANGAPROOF_PICKER_DIR", str(tmp_path))
    assert android_picker.picker_dir() == tmp_path


def test_picker_dir_creates_when_missing(tmp_path, monkeypatch):
    target = tmp_path / "not-yet"
    monkeypatch.setenv("MANGAPROOF_PICKER_DIR", str(target))
    assert android_picker.picker_dir() == target
    assert target.is_dir()


# --------------------------------------------------------------------- 成功路径

def test_pick_folder_writes_command_and_reads_result(picker_dir):
    """命令内容/文件名必须与 Java 侧约定一致（FOLDER <token>）。"""
    _answer_after(picker_dir, "OK\t/storage/emulated/0/Download/manga\t"
                              "content://com.android.externalstorage.documents/tree/primary%3ADownload%2Fmanga")
    result = android_picker.pick("folder", timeout_ms=5000)

    assert result["status"] == android_picker.RESULT_OK
    assert result["path"] == "/storage/emulated/0/Download/manga"
    assert result["uri"].startswith("content://com.android.externalstorage.documents/tree/")
    # 结果文件已被消费（避免下一轮读到旧结果）
    assert not (picker_dir / "result.txt").exists()
    # 命令文件已被 Java"消费"（模拟端没删，这里只验证内容格式）
    text = (picker_dir / "cmd.txt").read_text(encoding="utf-8").strip()
    kind, token = text.split()
    assert kind == "FOLDER"
    assert token and len(token) == 8


def test_pick_file_command_uses_file_keyword(picker_dir):
    _answer_after(picker_dir, "OK\t/storage/emulated/0/a.psd\tcontent://x/1")
    result = android_picker.pick("file", timeout_ms=5000)
    assert result["status"] == android_picker.RESULT_OK
    assert result["path"] == "/storage/emulated/0/a.psd"
    assert (picker_dir / "cmd.txt").read_text(encoding="utf-8").strip().startswith("FILE ")


def test_pick_reads_result_that_arrives_immediately(picker_dir):
    """结果在第一轮轮询前就绪（Java 侧极快返回）也要能读到。

    不能"事先把 result.txt 放好"——`pick()` 会先清掉上一轮的残留结果，这正是
    "不把旧结果当成本次结果"的保护（下一条测试单独验证该保护）。
    """
    _answer_after(picker_dir, "OK\t/tmp/x\tcontent://y", delay_ms=0)
    result = android_picker.pick("folder", timeout_ms=2000)
    assert result["path"] == "/tmp/x"


def test_pick_discards_stale_result(picker_dir):
    """上一轮遗留的结果文件不能被当成本次结果。"""
    (picker_dir / "result.txt").write_text("OK\t/旧的\tcontent://old\n", encoding="utf-8")
    _answer_after(picker_dir, "CANCEL", delay_ms=10)
    result = android_picker.pick("folder", timeout_ms=5000)
    assert result["status"] == android_picker.RESULT_CANCEL
    assert result["path"] == ""


# --------------------------------------------------------------------- 取消/失败

def test_pick_cancel(picker_dir):
    _answer_after(picker_dir, "CANCEL")
    result = android_picker.pick("folder", timeout_ms=5000)
    assert result["status"] == android_picker.RESULT_CANCEL
    assert result["path"] == ""


def test_pick_error_keeps_message(picker_dir):
    _answer_after(picker_dir, "ERROR\t云盘不支持")
    result = android_picker.pick("folder", timeout_ms=5000)
    assert result["status"] == android_picker.RESULT_ERROR
    assert result["message"] == "云盘不支持"


def test_pick_empty_path_is_error(picker_dir):
    _answer_after(picker_dir, "OK\t\tcontent://y")
    result = android_picker.pick("folder", timeout_ms=5000)
    assert result["status"] == android_picker.RESULT_ERROR


def test_pick_garbage_is_error(picker_dir):
    _answer_after(picker_dir, "完全不是协议内容")
    result = android_picker.pick("folder", timeout_ms=5000)
    assert result["status"] == android_picker.RESULT_ERROR


def test_pick_rejects_while_previous_command_pending(picker_dir):
    """上一次选择没结束（cmd.txt 还在）时不能叠加第二次请求。"""
    (picker_dir / "cmd.txt").write_text("FOLDER deadbeef\n", encoding="utf-8")
    result = android_picker.pick("folder", timeout_ms=1000)
    assert result["status"] == android_picker.RESULT_ERROR
    assert "尚未结束" in result["message"]


def test_pick_timeout_returns_and_cleans_command(picker_dir):
    """超时必须返回（绝不永久卡住），并清掉命令避免稍后又弹一次选择器。"""
    result = android_picker.pick("folder", timeout_ms=400)
    assert result["status"] == android_picker.RESULT_ERROR
    assert "超时" in result["message"]
    assert not (picker_dir / "cmd.txt").exists()


def test_pick_rejects_unknown_kind(picker_dir, qt_app):
    with pytest.raises(ValueError):
        android_picker.pick("nonsense")


# --------------------------------------------------------------------- 解析

@pytest.mark.parametrize(
    "line,status,path,message_part",
    [
        ("OK\t/storage/emulated/0/a\tcontent://x", "OK", "/storage/emulated/0/a", ""),
        ("CANCEL", "CANCEL", "", ""),
        ("ERROR\t炸了", "ERROR", "", "炸了"),
        ("OK\t", "ERROR", "", "空路径"),
        ("", "ERROR", "", "无法识别"),
    ],
)
def test_parse_protocol(line, status, path, message_part):
    parsed = android_picker._parse(line)
    assert parsed["status"] == status
    assert parsed["path"] == path
    if message_part:
        assert message_part in parsed["message"]


def test_probe_reports_directory(picker_dir, qt_app):
    info = android_picker.probe()
    assert info["dir_ok"] is True
    assert info["cmd_pending"] is False
    assert info["dir_env"] == str(picker_dir)


# --------------------------------------------------------------------- 门面分流

def test_facade_desktop_uses_qfiledialog(monkeypatch, qt_app):
    """桌面分支必须**仍然是原来的 QFileDialog 调用**（行为零改动）。"""
    monkeypatch.setattr(picker, "is_android_strict", lambda: False)
    calls: list = []

    def fake_get_open(*args, **kwargs):
        calls.append(("psd", args, kwargs))
        return ("/tmp/chosen.psd", "PSD/PSB 文件 (*.psd *.psb)")

    def fake_get_dir(*args, **kwargs):
        calls.append(("folder", args, kwargs))
        return "/tmp/chosen_dir"

    monkeypatch.setattr(QFileDialog, "getOpenFileName", fake_get_open)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", fake_get_dir)

    assert picker.pick_psd_file(None) == "/tmp/chosen.psd"
    assert picker.pick_folder(None) == "/tmp/chosen_dir"
    assert [c[0] for c in calls] == ["psd", "folder"]


def test_facade_desktop_cancel_returns_empty(monkeypatch, qt_app):
    monkeypatch.setattr(picker, "is_android_strict", lambda: False)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "")
    assert picker.pick_psd_file(None) == ""
    assert picker.pick_folder(None) == ""


def test_facade_android_goes_through_android_picker(monkeypatch, qt_app):
    """Android 分支必须走 android_picker，且**不**触碰 QFileDialog。"""
    monkeypatch.setattr(picker, "is_android_strict", lambda: True)
    used: list = []

    def fake_pick(kind, **kwargs):
        used.append(kind)
        return {"status": android_picker.RESULT_OK, "path": "/storage/emulated/0/漫画",
                "uri": "content://x", "message": ""}

    monkeypatch.setattr(android_picker, "pick", fake_pick)

    def explode(*args, **kwargs):
        raise AssertionError("Android 分支不应调用 QFileDialog")

    monkeypatch.setattr(QFileDialog, "getOpenFileName", explode)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", explode)

    assert picker.pick_folder(None) == "/storage/emulated/0/漫画"
    assert picker.pick_psd_file(None) == "/storage/emulated/0/漫画"
    assert used == ["folder", "file"]


def test_facade_android_cancel_returns_empty(monkeypatch, qt_app):
    monkeypatch.setattr(picker, "is_android_strict", lambda: True)
    monkeypatch.setattr(
        android_picker, "pick",
        lambda kind, **kwargs: {"status": android_picker.RESULT_CANCEL,
                                "path": "", "uri": "", "message": ""},
    )
    assert picker.pick_folder(None) == ""
    assert picker.pick_psd_file(None) == ""


def test_facade_android_channel_failure_is_not_fatal(monkeypatch, qt_app):
    """通道异常（例如 APK 里缺类）不能把应用搞崩：返回空串 + 记录日志。"""
    monkeypatch.setattr(picker, "is_android_strict", lambda: True)

    def boom(kind, **kwargs):
        raise android_picker.AndroidPickerError("通道坏了")

    monkeypatch.setattr(android_picker, "pick", boom)
    # parent=None 时不弹窗，只返回空串
    assert picker.pick_folder(None) == ""
    assert picker.pick_psd_file(None) == ""
