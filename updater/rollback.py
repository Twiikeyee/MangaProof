# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""回滚与异常中断恢复（需求 §63/§64，调研报告 §5.1/§5.4）。

回滚（§63）：失败时 ``删除新版本 → MangaProof.old 改回 MangaProof → 恢复用户数据``，
要求"旧版本能够启动、旧数据保持完整"。用户数据是**复制**出去的（§49），
所以旧目录里的数据从未被动过，恢复只是把备份再写回去一遍（幂等）。

异常中断恢复（§64）：安装器被杀 / 关机 / 断电 / 新版崩溃之后，下一次启动
只要看到 ``.old`` 残留就**优先恢复到旧版本状态**：

- 有 ``.old``、没有安装目录 → 直接改名回去；
- 两者都在 → 用**成功标记的 token+version** 判断上次是否真的成功：
  成功则清理残留 ``.old``；否则删掉可疑的新版本、把 ``.old`` 改回原名。

Windows 上"删除正在运行的 exe"会失败（§5.1：运行中的 exe 可改名、不能删除），
因此 :func:`remove_tree` 会重试，并支持 ``MoveFileExW(..., MOVEFILE_DELAY_UNTIL_REBOOT)``
兜底（需管理员）。
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from updater import OLD_SUFFIX
from updater import backup as backup_mod
from updater import state as state_mod

log = logging.getLogger("mangaproof.updater.rollback")

ItemCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int], None]

MOVEFILE_DELAY_UNTIL_REBOOT = 0x4


class RollbackError(Exception):
    """回滚失败（旧版本无法恢复）——这是最严重的情况，必须让用户看到。"""


@dataclass
class RollbackResult:
    recovered: bool
    reason: str
    actions: list[str] = field(default_factory=list)


@dataclass
class RecoveryResult:
    """异常中断恢复的结果。``action`` ∈ {none, restored, cleaned-old, broken}。"""

    action: str
    message: str
    actions: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.action in ("restored", "cleaned-old")


def old_dir_for(install_dir: Path) -> Path:
    """``MangaProof`` → ``MangaProof.old``（需求 §54/§55）。"""
    path = Path(install_dir)
    return path.with_name(path.name + OLD_SUFFIX)


def _schedule_delete_on_reboot(path: Path) -> bool:
    """Windows 兜底：登记"重启后删除"（需管理员；只在 Windows 生效）。"""
    if os.name != "nt":  # pragma: no cover - 非 Windows 分支
        return False
    try:  # 运行时导入：不让 Linux/macOS 加载 Windows 专有代码（需求 §6）
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        ok = kernel32.MoveFileExW(str(path), None, MOVEFILE_DELAY_UNTIL_REBOOT)
        return bool(ok)
    except Exception as exc:  # pragma: no cover - 依赖 Windows API
        log.warning("登记重启删除失败：%s（%s）", path, exc)
        return False


def remove_tree(
    path: Path,
    *,
    retries: int = 5,
    delay: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """删除文件/目录，带重试（Windows 上正在运行的 exe 会短暂占用）。"""
    target = Path(path)
    if not target.exists() and not target.is_symlink():
        return True
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target, onexc=_force_chmod)
            return True
        except OSError as exc:
            last_error = exc
            if attempt + 1 < max(1, retries):
                sleep(delay)
    log.warning("删除失败（已重试 %s 次）：%s（%s）", retries, target, last_error)
    if os.name == "nt" and _schedule_delete_on_reboot(target):
        log.warning("已登记重启后删除：%s", target)
        return True
    return False


def _force_chmod(func, path, exc_value) -> None:  # pragma: no cover - 只在权限异常时触发
    """``shutil.rmtree`` 的兜底：先补写权限再删（只读文件在 Windows 上很常见）。

    Python 3.12 的 ``onexc`` 回调签名是 ``(func, path, exc)``。
    """
    import stat

    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        func(path)
    except OSError:
        raise


def rollback(
    state: state_mod.UpdateState,
    *,
    on_item: ItemCallback | None = None,
    on_progress: ProgressCallback | None = None,
    sleep: Callable[[float], None] = time.sleep,
    retries: int = 5,
    retry_delay: float = 0.2,
    restore_data: bool = True,
) -> RollbackResult:
    """执行回滚（需求 §63）。未发生替换时是**无操作**（不会碰安装目录）。"""
    install = Path(state.install_dir)
    old = Path(state.old_dir) if state.old_dir else old_dir_for(install)
    actions: list[str] = []

    if not state.renamed_old and not old.exists():
        return RollbackResult(False, "未发生替换，安装目录未被修改", actions)

    # 1) 删除新版本（可能是半截的解压结果）
    if install.exists() or install.is_symlink():
        if on_item is not None:
            on_item(install.name)
        if on_progress is not None:
            on_progress(0, -1)
        if not remove_tree(
            install, retries=retries, delay=retry_delay, sleep=sleep
        ):
            # 删不掉就先挪走：绝不能让它挡住旧版本恢复（§63 的硬要求）
            parked = install.with_name(install.name + ".new-failed")
            try:
                remove_tree(parked, retries=1, delay=retry_delay, sleep=sleep)
                os.replace(install, parked)
                actions.append(f"{install.name} → {parked.name}（删除失败，已挪走）")
                log.warning("新版本目录删除失败，已改名为 %s", parked)
            except OSError as exc:
                raise RollbackError(
                    f"回滚失败：无法删除新版本目录 {install}（{exc}）；"
                    f"旧版本仍在 {old}"
                ) from exc
        else:
            actions.append(f"删除新版本：{install.name}")

    # 2) .old 改回原名
    if old.exists():
        if on_item is not None:
            on_item(f"{old.name} → {install.name}")
        if on_progress is not None:
            on_progress(1, -1)
        last_error: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                os.replace(old, install)
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                sleep(retry_delay)
        if last_error is not None:
            raise RollbackError(
                f"回滚失败：无法把 {old} 改回 {install}（{last_error}）"
            ) from last_error
        actions.append(f"{old.name} → {install.name}")

    # 3) 恢复用户数据（复制出去的原件从未被动过，这一步是幂等的保险）
    backup_dir = Path(state.data_backup) if state.data_backup else None
    if restore_data and backup_dir is not None and backup_dir.is_dir() and install.exists():
        try:
            report = backup_mod.restore_user_data(
                backup_dir, install, on_item=on_item, on_progress=on_progress
            )
            actions.append(f"恢复用户数据：{report.copied} 项")
        except backup_mod.BackupError as exc:
            raise RollbackError(f"回滚时恢复用户数据失败：{exc}") from exc

    reason = "已恢复到旧版本" if install.exists() else "回滚后未找到旧版本"
    return RollbackResult(True, reason, actions)


def recover_interrupted(
    state: state_mod.UpdateState,
    *,
    on_item: ItemCallback | None = None,
    on_progress: ProgressCallback | None = None,
    sleep: Callable[[float], None] = time.sleep,
    retries: int = 5,
    retry_delay: float = 0.2,
) -> RecoveryResult:
    """§64：下一次启动时检查 ``.old`` 等残留，优先恢复到旧版本状态。"""
    install = Path(state.install_dir)
    old = Path(state.old_dir) if state.old_dir else old_dir_for(install)
    actions: list[str] = []

    if not old.exists():
        if not install.exists():
            return RecoveryResult(
                "broken", f"安装目录不存在且没有旧版本备份：{install}", actions
            )
        return RecoveryResult("none", "没有发现未完成的更新残留", actions)

    if not install.exists():
        os.replace(old, install)
        actions.append(f"{old.name} → {install.name}")
        return RecoveryResult(
            "restored",
            f"上次更新未完成：已把 {old.name} 改回 {install.name}",
            actions,
        )

    # 安装目录与 .old 同时存在：用成功标记判断上次到底成没成功
    marker_ok = False
    if state.success_marker and state.token:
        check = state_mod.check_marker(
            Path(state.success_marker), state.token, state.version
        )
        marker_ok = check.valid
    if marker_ok:
        if remove_tree(old, retries=retries, delay=retry_delay, sleep=sleep):
            actions.append(f"清理残留：{old.name}")
        return RecoveryResult(
            "cleaned-old", f"上次更新已成功（成功标记校验通过），已清理 {old.name}", actions
        )

    if on_item is not None:
        on_item(install.name)
    if on_progress is not None:
        on_progress(0, -1)
    if not remove_tree(install, retries=retries, delay=retry_delay, sleep=sleep):
        raise RollbackError(
            f"上次更新未完成，但无法删除新版本目录：{install}；旧版本仍在 {old}"
        )
    actions.append(f"删除未完成的新版本：{install.name}")
    os.replace(old, install)
    actions.append(f"{old.name} → {install.name}")

    backup_dir = Path(state.data_backup) if state.data_backup else None
    if backup_dir is not None and backup_dir.is_dir():
        try:
            backup_mod.restore_user_data(
                backup_dir, install, on_item=on_item, on_progress=on_progress
            )
            actions.append("恢复用户数据")
        except backup_mod.BackupError as exc:
            log.warning("恢复用户数据失败（旧目录内的数据未被改动过）：%s", exc)
    return RecoveryResult(
        "restored",
        f"上次更新未完成：已删掉未完成的新版本并把 {old.name} 恢复为 {install.name}",
        actions,
    )


__all__ = [
    "MOVEFILE_DELAY_UNTIL_REBOOT",
    "RecoveryResult",
    "RollbackError",
    "RollbackResult",
    "old_dir_for",
    "recover_interrupted",
    "remove_tree",
    "rollback",
]
