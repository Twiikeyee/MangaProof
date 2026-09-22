# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""安装器的"改名 / 删除"原语单测（需求 §54/§63，Windows 共享冲突）。

为什么单独钉住这两个原语：Windows 在更新收尾阶段最常见的失败不是校验，
而是**文件占用**——杀毒/索引器扫到一半、句柄刚关闭还没完全释放、或者
"被改名的目录正是某个进程的 cwd"。实测故障：

    [WinError 32] 另一个程序正在使用此文件，进程无法访问。
    'S:\\MangaProof' -> 'S:\\MangaProof.old'

删除路径本来就有重试（:func:`remove_tree`），改名路径却是"一次定生死"。
这里保证两条路径的重试语义一致，并且失败时能把最后一次异常如实抛给调用方。
"""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from updater import rollback  # noqa: E402


class FlakyReplace:
    """前 ``fail_times`` 次抛共享冲突，之后成功（模拟杀毒软件短暂占用）。"""

    def __init__(self, fail_times: int, error: OSError | None = None) -> None:
        self.fail_times = fail_times
        self.calls: list[tuple[Path, Path]] = []
        self.error = error or OSError(errno.EACCES, "另一个程序正在使用此文件")

    def __call__(self, source, target) -> None:
        self.calls.append((Path(source), Path(target)))
        if len(self.calls) <= self.fail_times:
            raise self.error


def test_replace_path_retries_until_success():
    """瞬时占用必须在重试里被吃掉（不能第一次失败就判更新失败）。"""
    fail = FlakyReplace(fail_times=2)
    slept: list[float] = []
    rollback.replace_path(
        Path("S:/MangaProof"), Path("S:/MangaProof.old"),
        retries=5, delay=0.1, sleep=slept.append, replace=fail,
    )
    assert len(fail.calls) == 3, "前两次失败后应继续重试"
    assert slept, "重试之间必须真的等待（给占用方释放的时间）"


def test_replace_path_raises_last_error_after_exhausting_retries():
    """重试耗尽后抛出**最后一次**异常，调用方据此判 ExitCode.REPLACE。"""
    fail = FlakyReplace(fail_times=99)
    with pytest.raises(OSError) as excinfo:
        rollback.replace_path(
            Path("a"), Path("b"), retries=3, delay=0.0,
            sleep=lambda _s: None, replace=fail,
        )
    assert len(fail.calls) == 3
    assert "另一个程序正在使用此文件" in str(excinfo.value)


def test_replace_path_does_not_sleep_after_last_attempt():
    """最后一次失败后不再等待（别让用户白等）。"""
    fail = FlakyReplace(fail_times=99)
    slept: list[float] = []
    with pytest.raises(OSError):
        rollback.replace_path(
            Path("a"), Path("b"), retries=2, delay=0.5,
            sleep=slept.append, replace=fail,
        )
    assert len(slept) == 1, "2 次尝试之间只等 1 次"


def test_replace_path_retries_at_least_once():
    """retries=0/负数也要真的尝试一次（不能一次都不试就抛）。"""
    fail = FlakyReplace(fail_times=99)
    with pytest.raises(OSError):
        rollback.replace_path(
            Path("a"), Path("b"), retries=0, delay=0.0,
            sleep=lambda _s: None, replace=fail,
        )
    assert len(fail.calls) == 1


def test_replace_path_really_moves_a_directory(tmp_path: Path):
    """默认实现就是真的改名（目录也要能改）。"""
    src = tmp_path / "MangaProof"
    src.mkdir()
    (src / "MangaProof.exe").write_bytes(b"x")
    dst = tmp_path / "MangaProof.old"
    rollback.replace_path(src, dst, retries=1, delay=0.0)
    assert not src.exists()
    assert (dst / "MangaProof.exe").read_bytes() == b"x"


def test_remove_tree_reports_failure_after_retries(tmp_path: Path, monkeypatch):
    """删不掉时返回 False（调用方据此走"挪走/重启后删"的兜底）。"""
    target = tmp_path / "locked"
    target.mkdir()
    (target / "f").write_bytes(b"x")

    def boom(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "占用中")

    monkeypatch.setattr(rollback.shutil, "rmtree", boom)
    monkeypatch.setattr(rollback, "_schedule_delete_on_reboot", lambda _p: False)
    assert rollback.remove_tree(target, retries=2, delay=0.0) is False
    assert target.is_dir(), "失败时不得假装删掉了"
