# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""主程序侧的安装器准备与启动（需求 §40~§46）。

流程：

.. code-block:: text

    程序目录里的 MangaProof-update-installer
        ↓ 复制（需求 §40：必须从临时目录运行，不能直接从旧程序目录运行）
    临时目录 MangaProof-update-installer/
        ↓ 确认存在 + 可执行（需求 §41）
    启动安装器（需要提权时走平台原生授权，需求 §42）
        ↓ 确认进程创建成功
    主程序退出（需求 §46）

安装器与主程序**同级同目录**（需求 1.2.1；构建侧由 PyInstaller 的
``EXECUTABLE`` 条目保证，见调研报告 §11.3），因此这里按
"程序目录 / 安装器文件名"定位它；找不到就明确报错，不猜路径。
"""

from __future__ import annotations

import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from mangaproof.update import platform_dirs
from mangaproof.update.errors import InstallerError

log = logging.getLogger("mangaproof.update.installer")


@dataclass(frozen=True)
class InstallerInvocation:
    """一次安装器调用的完整参数（需求 §45）。"""

    exe: Path                 # 临时目录里那份安装器
    args: list[str]           # 传给安装器的参数
    token: str                # 一次性 token（新版写 marker 时回填，安装器校验）
    elevate: bool             # 是否需要提权
    success_marker: Path
    data_backup: Path
    status_file: Path

    def command_line(self) -> str:
        return " ".join([str(self.exe), *self.args])


def find_installer() -> Path:
    """在**安装目录**里找安装器（与主程序可执行文件同级）。"""
    module = _platform()
    base = module.install_dir()
    name = platform_dirs.installer_filename()
    candidate = base / name
    if candidate.is_file():
        return candidate

    # macOS：PyInstaller BUILT 的 .app 里安装器在 Contents/MacOS/ 下，
    # 而 install_dir() 返回的是 .app 本身 —— 这里补一次 Contents/MacOS 查找。
    macos_candidate = base / "Contents" / "MacOS" / name
    if macos_candidate.is_file():
        return macos_candidate

    raise InstallerError(
        "找不到更新安装器：\n"
        f"  期望位置：{candidate}\n"
        "该文件应随程序一同发布（与主程序可执行文件同级）；"
        "若你使用的是自行裁剪的安装包，请重新下载完整发行包。"
    )


def prepare_invocation(
    *,
    package: Path,
    version: str,
    sha256: str | None = None,
    parent_pid: int | None = None,
) -> InstallerInvocation:
    """把安装器复制到临时目录并组装参数（需求 §40/§41/§45）。

    不做任何破坏性操作：只复制、只组装命令行。
    """
    module = _platform()
    source = find_installer()

    target_dir = platform_dirs.installer_dir()
    target = target_dir / source.name
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        raise InstallerError(f"复制安装器到临时目录失败：{exc}") from exc

    if not target.is_file():
        raise InstallerError(f"安装器复制后不存在：{target}")
    if module.__name__.endswith(("linux", "macos")):
        # 需求 §41：Linux/macOS 启动前确认可执行，必要时 chmod +x
        try:
            if not target.stat().st_mode & 0o111:
                target.chmod(target.stat().st_mode | 0o111)
        except OSError as exc:
            log.warning("补可执行位失败（继续尝试启动）：%s", exc)

    token = uuid.uuid4().hex
    marker = platform_dirs.success_marker_path()
    backup = platform_dirs.data_backup_dir()
    status_file = platform_dirs.state_file_path()
    install_dir = module.install_dir()

    args = [
        "--install-dir", str(install_dir),
        "--package", str(Path(package)),
        "--data-backup", str(backup),
        "--success-marker", str(marker),
        "--version", str(version),
        "--sha256", str(sha256 or ""),
        "--token", token,
        "--status-file", str(status_file),
        "--platform", platform_dirs.PLATFORM_KEY,
    ]
    if parent_pid:
        args += ["--parent-pid", str(parent_pid)]

    elevate = bool(module.needs_elevation())
    log.info(
        "安装器已就绪：%s（提权=%s，安装目录=%s，token=%s）",
        target.name, "是" if elevate else "否", install_dir, token[:8],
    )
    return InstallerInvocation(
        exe=target, args=args, token=token, elevate=elevate,
        success_marker=marker, data_backup=backup, status_file=status_file,
    )


def launch(invocation: InstallerInvocation) -> int:
    """启动安装器并确认进程创建成功（需求 §46）。

    失败时抛 :class:`InstallerError` —— 调用方必须**保持主程序运行**并提示用户，
    绝不能"以为启动了"就把自己关掉。
    """
    module = _platform()
    try:
        pid = module.launch_installer(
            invocation.exe, invocation.args, elevate=invocation.elevate
        )
    except PermissionError as exc:
        raise InstallerError(f"用户取消了授权，更新未开始。\n{exc}") from exc
    except OSError as exc:
        raise InstallerError(f"启动安装器失败：{exc}") from exc

    log.info(
        "安装器已启动（pid=%s，提权=%s，token=%s）",
        pid or "未知", invocation.elevate, invocation.token[:8],
    )
    return int(pid or 0)


def write_success_marker(
    *, token: str, version: str, pid: int | None = None
) -> Path:
    """新版主程序写成功标记（需求 §57/§58）。

    - 位置：**更新临时目录**（不是程序目录，需求 §58：不需要再改程序目录、
      也不受程序目录权限影响）；
    - 内容：``{"token": …, "version": …, "pid": …, "ts": …}`` ——
      带 token/version 才能让安装器区分"本次更新成功"与"上次的残留 marker"
      （只判文件存在是不幂等的）。
    """
    import json
    import os
    import time

    marker = platform_dirs.success_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "token": token,
        "version": version,
        "pid": int(pid if pid is not None else os.getpid()),
        "ts": time.time(),
    }
    tmp = marker.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(marker)          # 原子落盘：安装器可能正在轮询这个文件
    log.info("已写入更新成功标记：%s（版本 %s）", marker, version)
    return marker


def read_success_marker() -> dict | None:
    """读取成功标记（安装器侧与"异常中断恢复"都会用）。"""
    import json

    marker = platform_dirs.success_marker_path()
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _platform():
    from mangaproof.update import platform as platform_pkg

    return platform_pkg.current()


__all__ = [
    "InstallerInvocation",
    "find_installer",
    "prepare_invocation",
    "launch",
    "write_success_marker",
    "read_success_marker",
]
