"""选择器门面：桌面分支**必须与改造前逐参数一致**，只有 Android 多一个选项。

这是本轮改动的核心约束——"桌面端逻辑不变"。所以这里断言的是**调用参数本身**
（而不是只看返回值）：桌面不得传任何 options，Android 必须传 `DontUseNativeDialog`
（Qt 在 Android 上默认走原生 SAF 对话框，那条路会死锁，见 picker.py 模块文档）。
"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication, QFileDialog

from mangaproof.storage import picker

app = QApplication.instance() or QApplication([])


def _capture(monkeypatch, return_value):
    """把 QFileDialog 的两个静态方法换成记录参数的桩。

    注意两者返回形状不同：`getOpenFileName` 返回 `(path, selected_filter)`，
    `getExistingDirectory` 返回 `path`。
    """
    calls: list[tuple] = []

    def fake_open(*args, **kwargs):
        calls.append(("open", args, kwargs))
        return (return_value, "")

    def fake_dir(*args, **kwargs):
        calls.append(("folder", args, kwargs))
        return return_value

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(fake_open))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(fake_dir))
    return calls


# --------------------------------------------------------------------- 桌面

def test_desktop_passes_no_options(monkeypatch):
    """桌面：与改造前完全一致 —— 标题/初始目录/过滤器老三样，options 为空。"""
    monkeypatch.setattr(picker, "is_android_strict", lambda: False)
    calls = _capture(monkeypatch, "/tmp/x.psd")
    assert picker.pick_psd_file(None) == "/tmp/x.psd"
    kind, args, kwargs = calls[-1]
    assert kind == "open"
    assert args[1] == "打开单个 PSD"
    assert args[2] == ""                                   # 桌面初始目录不变
    assert args[3] == "PSD/PSB 文件 (*.psd *.psb)"
    assert kwargs["options"] == QFileDialog.Option(0)      # 不带任何选项


def test_desktop_folder_passes_no_options(monkeypatch):
    monkeypatch.setattr(picker, "is_android_strict", lambda: False)
    calls = _capture(monkeypatch, "/tmp/dir")
    assert picker.pick_folder(None) == "/tmp/dir"
    kind, args, kwargs = calls[-1]
    assert kind == "folder"
    assert args[1] == "打开漫画文件夹"
    assert args[2] == ""
    assert kwargs["options"] == QFileDialog.Option(0)      # 桌面不加 ShowDirsOnly


def test_desktop_no_android_start_dir(monkeypatch):
    """桌面不许探 Android 目录（避免任何多余的文件系统访问）。"""
    monkeypatch.setattr(picker, "is_android_strict", lambda: False)
    assert picker._android_start_dir() == ""


# --------------------------------------------------------------------- Android

def test_android_file_uses_widget_dialog(monkeypatch):
    """Android：必须带 DontUseNativeDialog，否则会走 Qt 原生 SAF 对话框并死锁。"""
    monkeypatch.setattr(picker, "is_android_strict", lambda: True)
    monkeypatch.setattr(picker, "_android_start_dir", lambda: "/storage/emulated/0/Download")
    calls = _capture(monkeypatch, "/storage/emulated/0/a.psd")
    assert picker.pick_psd_file(None) == "/storage/emulated/0/a.psd"
    kind, args, kwargs = calls[-1]
    assert kind == "open"
    assert args[2] == "/storage/emulated/0/Download"      # Android 有可用起始目录
    assert kwargs["options"] & QFileDialog.Option.DontUseNativeDialog


def test_android_folder_uses_widget_dialog_with_dirs_only(monkeypatch):
    monkeypatch.setattr(picker, "is_android_strict", lambda: True)
    monkeypatch.setattr(picker, "_android_start_dir", lambda: "")
    calls = _capture(monkeypatch, "/storage/emulated/0/manga")
    assert picker.pick_folder(None) == "/storage/emulated/0/manga"
    _kind, _args, kwargs = calls[-1]
    opts = kwargs["options"]
    assert opts & QFileDialog.Option.DontUseNativeDialog
    assert opts & QFileDialog.Option.ShowDirsOnly


def test_android_start_dir_falls_back_to_empty_when_nothing_exists(monkeypatch, tmp_path):
    """候选目录都不存在时返回空串，交给 Qt 默认行为（不硬塞一个不存在的路径）。"""
    monkeypatch.setattr(picker, "is_android_strict", lambda: True)
    monkeypatch.setattr(picker, "Path", lambda p: tmp_path / "nope")
    assert picker._android_start_dir() == ""


# --------------------------------------------------------------------- 取消语义

def test_cancel_returns_empty_string(monkeypatch):
    """取消的返回值语义与 QFileDialog 一致（空串），调用方据此不打开任务。"""
    for android in (False, True):
        monkeypatch.setattr(picker, "is_android_strict", lambda a=android: a)
        _capture(monkeypatch, "")
        assert picker.pick_psd_file(None) == ""
        assert picker.pick_folder(None) == ""
