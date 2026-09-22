# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""用户数据备份与恢复（需求 §47~§49、§56，调研报告 §5.4）。

规则只有一个来源：:data:`mangaproof.config.user_data.USER_DATA_RULES`（需求
§47/§48：白名单是软件逻辑，安装器**不需要**跟着改）。本模块只负责"按规则复制"。

三条硬约束：

1. **复制，不是移动**（需求 §49）：备份失败/回滚时原目录必须完好无损。
2. **临时名 + :func:`os.replace`**（调研报告 §5.4）：恢复/备份都可重复执行，
   第二次执行结果与第一次相同（幂等），且不会留下半截文件。
3. **每条操作都要能上报具体文件/目录的相对路径**：安装器 UI 必须显示
   "正在复制：logs/2026-09-22.log"（调研报告 §11.4）。
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

from mangaproof.config.user_data import (
    USER_DATA_RULES,
    TYPE_DIRECTORY,
    TYPE_FILE,
    absolute_targets,
    validate_rules,
)

log = logging.getLogger("mangaproof.updater.backup")

ItemCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int], None]

BACKUP = "backup"
RESTORE = "restore"


class BackupError(Exception):
    """备份/恢复失败（需求 §62：复制失败 → 进入 rollback）。"""


@dataclass(frozen=True)
class CopyEntry:
    """一条待复制条目（相对路径用 posix，便于 UI 展示与跨平台日志）。"""

    rel: str
    source: Path
    target: Path
    kind: str  # file | directory


@dataclass
class BackupReport:
    """复制结果（``items`` 供日志/UI 回放，``skipped`` 是白名单里不存在的项）。"""

    direction: str
    copied: int = 0
    bytes: int = 0
    skipped: tuple[str, ...] = ()
    items: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return self.copied


def _is_within(child: Path, parent: Path) -> bool:
    try:
        real_child = os.path.realpath(child)
        real_parent = os.path.realpath(parent)
        return os.path.commonpath([real_child, real_parent]) == real_parent
    except (OSError, ValueError):
        return False


def _iter_dir_files(root: Path) -> list[str]:
    """目录内所有文件（相对 posix 路径，排序稳定）；**不跟随软链接目录**。"""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not (Path(dirpath) / name).is_symlink()
        )
        for name in sorted(filenames):
            rel = Path(dirpath, name).relative_to(root).as_posix()
            found.append(rel)
    return found


def plan_copy(
    source_root: Path,
    target_root: Path,
    *,
    rules: Sequence[dict[str, str]] = USER_DATA_RULES,
) -> tuple[list[CopyEntry], list[str]]:
    """按白名单生成复制计划：``(条目列表, 源里不存在的项)``。

    目录本身也是一条条目（这样空目录 ``logs/`` 也能被备份/恢复），
    目录内的每个文件各占一条（UI 的计数因此是"具体文件数"）。
    """
    validate_rules(rules)
    source = Path(source_root)
    target = Path(target_root)
    if _is_within(target, source) or _is_within(source, target):
        raise BackupError(
            f"备份目录与安装目录不能互相包含：{source} / {target}（需求 §49）"
        )
    entries: list[CopyEntry] = []
    missing: list[str] = []
    for abs_path, kind in absolute_targets(source, rules):
        rel = PurePosixPath(
            abs_path.relative_to(source).as_posix()
        ).as_posix()
        if kind == TYPE_FILE:
            if not abs_path.is_file():
                missing.append(rel)
                continue
            entries.append(
                CopyEntry(rel, abs_path, target.joinpath(*PurePosixPath(rel).parts), TYPE_FILE)
            )
        elif kind == TYPE_DIRECTORY:
            if not abs_path.is_dir():
                missing.append(rel)
                continue
            entries.append(
                CopyEntry(
                    rel, abs_path, target.joinpath(*PurePosixPath(rel).parts), TYPE_DIRECTORY
                )
            )
            for sub in _iter_dir_files(abs_path):
                sub_rel = f"{rel}/{sub}"
                entries.append(
                    CopyEntry(
                        sub_rel,
                        abs_path / sub,
                        target.joinpath(*PurePosixPath(sub_rel).parts),
                        TYPE_FILE,
                    )
                )
        else:  # pragma: no cover - validate_rules 已挡住
            raise BackupError(f"未知的白名单条目类型：{kind!r}（规则 {rel!r}）")
    return entries, missing


def _copy_file(src: Path, dst: Path) -> int:
    """复制单文件：同目录临时文件 → :func:`os.replace`（幂等、无半截文件）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(src, tmp)  # copy2：保留 mtime/权限位
        os.replace(tmp, dst)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"复制失败：{src} → {dst}（{exc}）（需求 §62）") from exc
    try:
        return int(src.stat().st_size)
    except OSError:
        return 0


def _ensure_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"创建目录失败：{path}（{exc}）") from exc


def copy_whitelist(
    source_root: Path,
    target_root: Path,
    *,
    direction: str,
    rules: Sequence[dict[str, str]] = USER_DATA_RULES,
    on_item: ItemCallback | None = None,
    on_progress: ProgressCallback | None = None,
) -> BackupReport:
    """按白名单把 ``source_root`` 里的用户数据复制到 ``target_root``。"""
    entries, missing = plan_copy(source_root, target_root, rules=rules)
    total = len(entries)
    if on_progress is not None:
        on_progress(0, total)
    copied = 0
    size = 0
    items: list[str] = []
    for index, entry in enumerate(entries, start=1):
        if on_item is not None:
            on_item(entry.rel)
        if entry.kind == TYPE_DIRECTORY:
            _ensure_dir(entry.target)
        else:
            size += _copy_file(entry.source, entry.target)
        copied += 1
        items.append(entry.rel)
        if on_progress is not None:
            on_progress(index, total)
    for rel in missing:
        log.info("白名单条目不存在，跳过：%s", rel)
    return BackupReport(
        direction=direction,
        copied=copied,
        bytes=size,
        skipped=tuple(missing),
        items=tuple(items),
    )


def backup_user_data(
    install_dir: Path,
    backup_dir: Path,
    *,
    rules: Sequence[dict[str, str]] = USER_DATA_RULES,
    on_item: ItemCallback | None = None,
    on_progress: ProgressCallback | None = None,
) -> BackupReport:
    """更新前备份（需求 §49：复制而非移动）。"""
    _ensure_dir(Path(backup_dir))
    return copy_whitelist(
        Path(install_dir),
        Path(backup_dir),
        direction=BACKUP,
        rules=rules,
        on_item=on_item,
        on_progress=on_progress,
    )


def restore_user_data(
    backup_dir: Path,
    install_dir: Path,
    *,
    rules: Sequence[dict[str, str]] = USER_DATA_RULES,
    on_item: ItemCallback | None = None,
    on_progress: ProgressCallback | None = None,
) -> BackupReport:
    """新版解压后恢复用户数据（需求 §56：恢复完成才启动新版）。"""
    _ensure_dir(Path(install_dir))
    return copy_whitelist(
        Path(backup_dir),
        Path(install_dir),
        direction=RESTORE,
        rules=rules,
        on_item=on_item,
        on_progress=on_progress,
    )


def remove_backup_dir(backup_dir: Path) -> bool:
    """删除数据备份目录（需求 §61 清理清单）；失败不抛错，交由调用方记日志。"""
    path = Path(backup_dir)
    if not path.exists():
        return False
    try:
        shutil.rmtree(path)
        return True
    except OSError as exc:
        log.warning("数据备份目录删除失败：%s（%s）", path, exc)
        return False


def whitelist_summary(rules: Sequence[dict[str, str]] = USER_DATA_RULES) -> str:
    """白名单的一行摘要（日志用；需求 §77 要求记录安装器状态与原因）。"""
    return ", ".join(f"{r['path']}({r['type']})" for r in rules)


__all__ = [
    "BACKUP",
    "RESTORE",
    "BackupError",
    "BackupReport",
    "CopyEntry",
    "backup_user_data",
    "copy_whitelist",
    "plan_copy",
    "remove_backup_dir",
    "restore_user_data",
    "whitelist_summary",
]
