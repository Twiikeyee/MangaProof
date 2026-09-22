# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""安装器状态机（需求 §45/§46/§53~§64/§78，调研报告 §5.4/§11.4）。

严格按 §78 的顺序推进::

    INIT → VALIDATE → BACKUP_DATA → VERIFY_PACKAGE → RENAME_OLD → EXTRACT
         → RESTORE_DATA → LAUNCH_NEW → WAIT_SUCCESS → (SUCCESS → CLEANUP
                                                        / 否则 ROLLBACK)

三条不容妥协的规则：

1. **破坏性操作前完成全部校验**（§53）：``MangaProof → MangaProof.old`` 之前必须
   已经过了"包存在 + SHA-256 + 结构 + 主程序存在"。
2. **等待成功标记最长 60 秒**（§59），并且新版**提前退出且没有标记时立即回滚**，
   不等满 60 秒（§60）。标记必须 token + version 双匹配（§59/§61）。
3. **任何判为失败的分支都必须完成回滚**（§62/§63）：:meth:`Installer.run` 的
   异常出口统一走 :func:`updater.rollback.rollback`；没做过替换时回滚是无操作，
   因此"校验失败不触碰安装目录"是天然成立的。

为可测试性，进程/时间/提权都是可注入的接缝（:class:`Runtime`）：单测不会真的
起 GUI、不会真的提权，也不会等满 60 秒。
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Callable, Sequence

from mangaproof.config.user_data import validate_rules

from updater import archive, backup, privilege, rollback, state as state_mod
from updater import verify as verify_mod
from updater.ui import LoggingReporter, NullReporter, Reporter

log = logging.getLogger("mangaproof.updater.installer")

#: 等待新版写入成功标记的上限（需求 §59：最长 60 秒）
SUCCESS_TIMEOUT = 60.0
#: 等待主程序退出的上限（§46：主程序确认安装器启动成功后主动退出）
PARENT_EXIT_TIMEOUT = 30.0
#: 轮询间隔
POLL_INTERVAL = 0.25
#: 安装器临时目录名前缀（需求 §40/§36/§37：TEMP|CACHE/MangaProof-update-installer）
INSTALLER_TEMP_PREFIX = "MangaProof-update-installer"


class ExitCode(IntEnum):
    """安装器退出码（0 = 成功；其余非 0，并在 stderr/日志给出可读原因）。"""

    OK = 0
    USAGE = 2            # 参数错误（argparse 同码）
    VALIDATION = 10      # 参数/环境/权限校验失败
    BACKUP = 20          # 备份用户数据失败
    VERIFY = 30          # 更新包校验失败（§53）
    REPLACE = 40         # RENAME_OLD 失败（§54）
    EXTRACT = 50         # 解压失败或解压后主程序不存在（§52）
    RESTORE = 60         # 恢复用户数据失败（§56）
    LAUNCH = 70          # 新版无法启动（§62）
    SUCCESS_TIMEOUT = 80 # 新版提前退出 / 60 秒内没有成功标记（§59/§60）
    ROLLBACK_FAILED = 81 # 回滚本身失败（§63，最严重）
    CANCELLED = 82       # 用户在提权对话框中取消（§42）
    INTERNAL = 90        # 未预期的内部异常


class InstallerFailure(Exception):
    """安装流程中的可预期失败（携带退出码与可读原因）。"""

    def __init__(self, code: ExitCode, message: str):
        self.code = code
        super().__init__(message)


@dataclass
class InstallerOptions:
    """CLI 参数的规范化结果（全部是绝对路径，需求 §45：不得依赖 cwd）。"""

    install_dir: Path
    package: Path
    data_backup: Path
    success_marker: Path
    version: str
    token: str
    platform: str
    sha256: str = ""
    parent_pid: int = 0
    status_file: Path | None = None
    cli: bool = False
    #: 重跑自身（提权）用的完整 argv；由 main.py 填入
    rerun_argv: tuple[str, ...] = ()

    @property
    def state_path(self) -> Path:
        """安装器状态文件路径（**全流程只用一个**）。

        主程序侧约定：状态文件叫 ``installer-state.json``，放在 ``--status-file``
        的**同目录**里。因此 :func:`resolve_state_path` 的规则是：

        - 传的是 ``…/installer-state.json`` → 用它；
        - 传的是同目录下的其它名字，但那个文件**已存在**（主程序真的在用它）→ 用它；
        - 否则 → ``<--status-file 的目录>/installer-state.json``；
        - 完全没传 ``--status-file`` → ``<--success-marker 的目录>/installer-state.json``。
        """
        return resolve_state_path(self.status_file, self.success_marker)


class ProcessOps:
    """启动/探测新版进程（可注入：单测不真的起进程）。"""

    def __init__(self) -> None:
        self._procs: dict[int, subprocess.Popen] = {}

    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: int | None = None,
        group: int | None = None,
    ) -> int:
        kwargs: dict = {
            "cwd": cwd,
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":  # pragma: no cover - 平台分支
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        else:
            kwargs["start_new_session"] = True
            if user is not None:
                kwargs["user"] = user
            if group is not None:
                kwargs["group"] = group
        proc = subprocess.Popen([str(a) for a in argv], **kwargs)  # noqa: S603
        self._procs[proc.pid] = proc
        return proc.pid

    def alive(self, pid: int) -> bool:
        proc = self._procs.get(pid)
        if proc is not None:
            return proc.poll() is None
        return _probe_pid(pid)

    def terminate(self, pid: int) -> None:
        proc = self._procs.get(pid)
        if proc is not None and proc.poll() is None:
            try:
                if os.name != "nt" and proc.pid == os.getpgid(proc.pid):
                    # 我们自己 start_new_session 拉起的进程：连同它的子进程一起结束
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
            except OSError:  # pragma: no cover
                pass
            try:  # 回收，避免留下僵尸进程（否则"进程是否还在"会一直为真）
                proc.wait(timeout=2.0)
            except (subprocess.TimeoutExpired, OSError):  # pragma: no cover
                pass
            return
        if pid <= 0:  # pragma: no cover
            return
        try:
            if os.name == "nt":  # pragma: no cover - 平台分支
                import ctypes

                handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)
                if handle:
                    ctypes.windll.kernel32.TerminateProcess(handle, 1)
                    ctypes.windll.kernel32.CloseHandle(handle)
            else:
                os.kill(pid, 15)
        except Exception:  # pragma: no cover - 尽力而为
            pass


def _probe_pid(pid: int) -> bool:
    """进程是否还在（默认实现：POSIX 用 ``kill(pid, 0)``）。"""
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - 平台分支
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        except Exception:  # pragma: no cover
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:  # pragma: no cover
        return False
    return True


@dataclass
class Runtime:
    """可注入的运行时接缝（默认都是真实实现）。"""

    reporter: Reporter = field(default_factory=NullReporter)
    store: state_mod.StateStore | None = None
    ops: ProcessOps = field(default_factory=ProcessOps)
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.monotonic
    elevate: Callable[..., int] | None = None
    is_admin: Callable[[], bool] = privilege.is_admin
    needs_elevation: Callable[[Path], bool] = privilege.needs_elevation
    remove_tree: Callable[..., bool] = rollback.remove_tree
    success_timeout: float = SUCCESS_TIMEOUT
    parent_timeout: float = PARENT_EXIT_TIMEOUT
    poll_interval: float = POLL_INTERVAL
    retries: int = 5
    retry_delay: float = 0.2
    user_kwargs: dict[str, int] = field(default_factory=dict)
    allow_elevation: bool = True

    @property
    def reporter_or_default(self) -> Reporter:
        return self.reporter if self.reporter is not None else NullReporter()


def resolve_state_path(status_file: Path | str | None, success_marker: Path | str) -> Path:
    """状态文件路径解析（跨进程契约：文件名固定为 ``installer-state.json``）。

    安装器只读写这一个状态文件（不另造名字）；详见
    :attr:`InstallerOptions.state_path`。
    """
    if status_file is None:
        return Path(success_marker).parent / state_mod.STATE_FILE_NAME
    given = Path(status_file)
    if given.name == state_mod.STATE_FILE_NAME:
        return given
    if given.exists():
        # 主程序确实在用这个文件（例如它自己初始化过）→ 尊重现状，不另起炉灶
        log.warning(
            "状态文件名不是约定的 %s，但该文件已存在，按现状使用：%s",
            state_mod.STATE_FILE_NAME, given,
        )
        return given
    return given.parent / state_mod.STATE_FILE_NAME


def rerun_argv_from_process() -> tuple[str, ...]:
    """本安装器的重跑命令行（提权时用；frozen 与源码运行两种情况）。"""
    if getattr(sys, "frozen", False):  # PyInstaller onefile
        return (sys.executable, *sys.argv[1:])
    return (sys.executable, *sys.argv)


def build_elevated_argv(options: InstallerOptions) -> list[str]:
    """提权重跑自己的命令行：去掉 ``--cli`` 再补上（提权只跑 CLI 逻辑）。"""
    base = list(options.rerun_argv) or [sys.executable]
    filtered = [arg for arg in base if arg != "--cli"]
    if not options.rerun_argv:  # 退化情况：按参数重建（单测/嵌入调用）
        filtered = [sys.executable, "--install-dir", str(options.install_dir),
                    "--package", str(options.package),
                    "--data-backup", str(options.data_backup),
                    "--success-marker", str(options.success_marker),
                    "--version", options.version, "--token", options.token,
                    "--platform", options.platform,
                    "--sha256", options.sha256]
        if options.status_file is not None:
            filtered += ["--status-file", str(options.status_file)]
    return [*filtered, "--cli"]


class Installer:
    """执行一次完整安装（一次更新 = 一个实例）。"""

    def __init__(self, options: InstallerOptions, runtime: Runtime | None = None) -> None:
        self.options = options
        self.rt = runtime or Runtime()
        self.reporter = self.rt.reporter_or_default
        self.state = state_mod.UpdateState(
            token=options.token,
            version=options.version,
            install_dir=str(options.install_dir),
            old_dir=str(rollback.old_dir_for(options.install_dir)),
            package=str(options.package),
            data_backup=str(options.data_backup),
            success_marker=str(options.success_marker),
            status_file=str(options.state_path),
            platform=options.platform,
            parent_pid=int(options.parent_pid),
        )
        self.store = self.rt.store or state_mod.StateStore(options.state_path)
        self._elevation_required = False
        self._last_marker_reason = ""

    # ------------------------------------------------------------------ #
    # 状态推进
    # ------------------------------------------------------------------ #
    def _phase(self, phase: str, message: str = "") -> None:
        self.reporter.phase(phase)
        self.store.transition(self.state, phase, message=message)
        if message:
            self.reporter.log(message)

    def _item(self, rel: str, action: str = "") -> None:
        self.reporter.item(rel, action)

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def run(self) -> int:
        try:
            self._init()
            self._validate()
            if self._elevation_required and self.rt.allow_elevation:
                return self._reexec_elevated()
            self._wait_parent()
            self._backup_data()
            self._verify_package()
            self._rename_old()
            self._extract()
            self._restore_data()
            self._launch_new()
            self._wait_success()
            self._cleanup()
        except InstallerFailure as exc:
            return self._handle_failure(exc)
        except BaseException as exc:  # 兜底：任何异常都必须走回滚
            log.error("安装器内部异常：%s", exc, exc_info=True)
            return self._handle_failure(
                InstallerFailure(ExitCode.INTERNAL, f"安装器内部错误：{exc}")
            )
        self._phase("SUCCESS", "更新成功")
        self.reporter.finish(True, "")
        return int(ExitCode.OK)

    # -- INIT ----------------------------------------------------------- #
    def _require_absolute_paths(self) -> None:
        """INIT 之前就要挡住相对路径：否则后面的探测会相对 **cwd** 发生（§45）。"""
        candidates = [
            ("--install-dir", self.options.install_dir),
            ("--package", self.options.package),
            ("--data-backup", self.options.data_backup),
            ("--success-marker", self.options.success_marker),
        ]
        if self.options.status_file is not None:
            candidates.append(("--status-file", self.options.status_file))
        problems = [
            f"{label} 必须是绝对路径（不得依赖 cwd）：{path}"
            for label, path in candidates
            if not Path(path).is_absolute()
        ]
        if problems:
            raise InstallerFailure(ExitCode.VALIDATION, "；".join(problems))

    def _init(self) -> None:
        self._require_absolute_paths()
        self._phase("INIT")
        opts = self.options
        try:
            opts.data_backup.parent.mkdir(parents=True, exist_ok=True)
            opts.success_marker.parent.mkdir(parents=True, exist_ok=True)
            if opts.status_file is not None:
                opts.status_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise InstallerFailure(
                ExitCode.VALIDATION, f"无法创建安装器工作目录：{exc}"
            ) from exc
        self.store.save(self.state)
        self.reporter.log(
            f"安装目录：{opts.install_dir}（平台 {opts.platform}，目标版本 {opts.version}）"
        )
        self._recover_previous_run()

    def _recover_previous_run(self) -> None:
        """§64：发现 ``.old`` 等残留时优先恢复到旧版本状态。"""
        old = Path(self.state.old_dir)
        if not old.exists():
            return
        self.reporter.log(f"检测到残留的 {old.name}，正在检查上一次更新是否完成…")
        try:
            result = rollback.recover_interrupted(
                self.state,
                on_item=lambda rel: self._item(rel, "恢复"),
                on_progress=self.reporter.progress,
                sleep=self.rt.sleep,
                retries=self.rt.retries,
                retry_delay=self.rt.retry_delay,
            )
        except rollback.RollbackError as exc:
            raise InstallerFailure(ExitCode.ROLLBACK_FAILED, str(exc)) from exc
        self.reporter.log(result.message)
        if result.action == "broken":
            raise InstallerFailure(ExitCode.VALIDATION, result.message)
        if result.action == "restored":
            # 安装目录刚刚被换回旧版本：本次更新作废，要求重新发起（状态一致优先）
            raise InstallerFailure(
                ExitCode.VALIDATION,
                f"{result.message}；请重新发起一次更新（需求 §64）",
            )
        self.store.save(self.state)

    # -- VALIDATE ------------------------------------------------------- #
    def _validate(self) -> None:
        self._phase("VALIDATE")
        opts = self.options
        problems: list[str] = []
        for label, path in (
            ("--install-dir", opts.install_dir),
            ("--package", opts.package),
            ("--data-backup", opts.data_backup),
            ("--success-marker", opts.success_marker),
        ):
            if not Path(path).is_absolute():
                problems.append(f"{label} 必须是绝对路径：{path}（需求 §45）")
        if opts.platform not in archive.PLATFORMS:
            problems.append(f"--platform 只能是 {list(archive.PLATFORMS)}：{opts.platform!r}")
        if not str(opts.version).strip():
            problems.append("--version 不能为空（成功标记要按版本校验，需求 §59）")
        if not str(opts.token).strip():
            problems.append("--token 不能为空（成功标记要按 token 校验，需求 §59）")
        if opts.sha256:
            from mangaproof.update.checksum import normalize_sha256

            try:
                normalize_sha256(opts.sha256)
            except ValueError as exc:
                problems.append(f"--sha256 不合法：{exc}")
        if not opts.install_dir.is_dir():
            problems.append(f"安装目录不存在：{opts.install_dir}（需求 §38）")
        if not opts.package.is_file():
            problems.append(f"更新包不存在：{opts.package}（需求 §53）")
        try:
            validate_rules()
        except Exception as exc:  # InvalidRule
            problems.append(f"用户数据白名单非法：{exc}（需求 §47）")
        if problems:
            raise InstallerFailure(ExitCode.VALIDATION, "；".join(problems))

        main_rel = archive.install_main_rel(opts.platform)
        if not (opts.install_dir / main_rel).is_file():
            self.reporter.log(
                f"警告：现有安装目录内未找到 {main_rel}（仍按 §54 整体替换）"
            )
        if not opts.sha256:
            self.reporter.log("警告：未提供 --sha256，本次不做哈希校验（需求 §53）")

        self._elevation_required = bool(self.rt.needs_elevation(opts.install_dir))
        if self._elevation_required:
            self.reporter.log(
                f"安装目录父目录不可写：{opts.install_dir.parent}（需求 §39）"
            )
        self.store.save(self.state)
        self.reporter.log("参数校验通过")

    def _reexec_elevated(self) -> int:
        """需要提权时：用平台原生机制重跑自己（``--cli``），本进程不再动文件。"""
        argv = build_elevated_argv(self.options)
        self.reporter.log("需要管理员权限，正在请求平台授权（仅用于文件操作）…")
        self.store.transition(
            self.state, "VALIDATE", message="等待平台授权（提权只执行文件操作）"
        )
        elevate = self.rt.elevate or privilege.run_elevated
        try:
            code = int(elevate(argv, platform=self.options.platform))
        except privilege.PrivilegeCancelled as exc:
            self.reporter.finish(False, str(exc))
            return int(ExitCode.CANCELLED)
        except privilege.PrivilegeUnavailable as exc:
            self.reporter.log(str(exc))
            self.reporter.finish(False, str(exc))
            return int(ExitCode.VALIDATION)
        except privilege.PrivilegeError as exc:
            self.reporter.log(str(exc))
            self.reporter.finish(False, str(exc))
            return int(ExitCode.VALIDATION)
        self.reporter.finish(code == 0, "" if code == 0 else f"提权安装退出码 {code}")
        return code

    # -- 等待主程序退出（§46） ------------------------------------------ #
    def _wait_parent(self) -> None:
        pid = int(self.options.parent_pid)
        if pid <= 0:
            return
        deadline = self.rt.now() + self.rt.parent_timeout
        self._item(f"主程序 pid={pid}", "等待退出")
        self.reporter.progress(0, -1)
        while self.rt.ops.alive(pid):
            if self.rt.now() >= deadline:
                raise InstallerFailure(
                    ExitCode.VALIDATION,
                    f"等待主程序退出超时（pid={pid}，{self.rt.parent_timeout:.0f} 秒）",
                )
            self.rt.sleep(self.rt.poll_interval)
        self.reporter.log(f"主程序（pid={pid}）已退出，开始安装")

    # -- BACKUP_DATA（§49） --------------------------------------------- #
    def _backup_data(self) -> None:
        self._phase("BACKUP_DATA")
        try:
            report = backup.backup_user_data(
                self.options.install_dir,
                self.options.data_backup,
                on_item=lambda rel: self._item(rel, "备份"),
                on_progress=self.reporter.progress,
            )
        except backup.BackupError as exc:
            raise InstallerFailure(ExitCode.BACKUP, str(exc)) from exc
        self.reporter.log(f"已备份用户数据 {report.copied} 项（复制而非移动，需求 §49）")
        if report.skipped:
            self.reporter.log("白名单中不存在的项：" + "、".join(report.skipped))

    # -- VERIFY_PACKAGE（§53） ------------------------------------------ #
    def _verify_package(self) -> None:
        self._phase("VERIFY_PACKAGE")
        try:
            result = verify_mod.verify_package(
                self.options.package,
                platform=self.options.platform,
                expected_sha256=self.options.sha256,
                on_item=lambda rel: self._item(rel, "校验"),
                on_progress=self.reporter.progress,
            )
        except verify_mod.VerifyError as exc:
            raise InstallerFailure(ExitCode.VERIFY, str(exc)) from exc
        self.state.package_sha256 = result.sha256
        self.store.save(self.state)
        self.reporter.log(
            f"更新包校验通过：{result.package.name}"
            f"（{result.size} 字节，{result.info.member_count} 个条目）"
        )

    # -- RENAME_OLD（§54/§55） ------------------------------------------ #
    def _rename_old(self) -> None:
        self._phase("RENAME_OLD")
        install = self.options.install_dir
        old = Path(self.state.old_dir)
        if old.exists():
            self._item(old.name, "删除残留")
            if not self.rt.remove_tree(
                old, retries=self.rt.retries, delay=self.rt.retry_delay,
                sleep=self.rt.sleep,
            ):
                raise InstallerFailure(
                    ExitCode.REPLACE, f"无法删除残留的旧版本目录：{old}"
                )
        self._item(f"{install.name} → {old.name}", "重命名")
        self.reporter.progress(0, -1)
        try:
            os.replace(install, old)
        except OSError as exc:
            raise InstallerFailure(
                ExitCode.REPLACE, f"重命名安装目录失败：{install} → {old}（{exc}）"
            ) from exc
        self.state.renamed_old = True
        # 立刻落盘：此后若安装器被杀，靠这个标记才能判断"更新未完成"（§64）
        self.store.save(self.state)

    # -- EXTRACT（§51/§52/§54） ----------------------------------------- #
    def _extract(self) -> None:
        self._phase("EXTRACT")
        install = self.options.install_dir
        parent = install.parent
        # 解压目标必须先清空：残留目录（上次失败/用户留下的同名目录）会被"合并"，
        # 里面的软链接还能把后续解压引到目标目录之外（需求 §52 的目标目录逃逸）。
        target_root = parent / archive.app_root_name(self.options.platform)
        if target_root.exists() or target_root.is_symlink():
            self._item(target_root.name, "清理解压目标残留")
            if not self.rt.remove_tree(
                target_root, retries=self.rt.retries, delay=self.rt.retry_delay,
                sleep=self.rt.sleep,
            ):
                raise InstallerFailure(
                    ExitCode.EXTRACT, f"无法清理解压目标残留目录：{target_root}"
                )
        try:
            result = archive.safe_extract(
                self.options.package,
                parent,
                platform=self.options.platform,
                on_item=lambda rel: self._item(rel, "解压"),
                on_progress=self.reporter.progress,
            )
        except archive.ArchiveError as exc:
            raise InstallerFailure(ExitCode.EXTRACT, str(exc)) from exc
        extracted_root = parent / result.info.root_dir_name
        if extracted_root != install:
            # 归档顶层名与安装目录名不一致（用户重命名过安装目录）：改名就位
            self._item(f"{extracted_root.name} → {install.name}", "改名")
            try:
                if install.exists():  # 理论上已被 RENAME_OLD 腾空
                    self.rt.remove_tree(install, retries=1, sleep=self.rt.sleep)
                os.replace(extracted_root, install)
            except OSError as exc:
                raise InstallerFailure(
                    ExitCode.EXTRACT,
                    f"新版本就位失败：{extracted_root} → {install}（{exc}）",
                ) from exc
        try:
            entry = verify_mod.verify_installed(
                install,
                self.options.platform,
                on_item=lambda rel: self._item(rel, "确认主程序"),
            )
        except verify_mod.VerifyError as exc:
            raise InstallerFailure(ExitCode.EXTRACT, str(exc)) from exc
        self.state.child_pid = 0
        self.store.save(self.state)
        self.reporter.log(
            f"解压完成：{result.extracted} 个条目（跳过 {len(result.skipped)} 个），"
            f"主程序 {entry.name}"
        )

    # -- RESTORE_DATA（§56） -------------------------------------------- #
    def _restore_data(self) -> None:
        self._phase("RESTORE_DATA")
        try:
            report = backup.restore_user_data(
                self.options.data_backup,
                self.options.install_dir,
                on_item=lambda rel: self._item(rel, "恢复"),
                on_progress=self.reporter.progress,
            )
        except backup.BackupError as exc:
            raise InstallerFailure(ExitCode.RESTORE, str(exc)) from exc
        self.reporter.log(f"已恢复用户数据 {report.copied} 项（需求 §56）")

    # -- LAUNCH_NEW（§57） ---------------------------------------------- #
    def _launch_argv(self) -> tuple[list[str], str | None]:
        """新版启动命令行（**跨进程契约**，与主程序侧一致）。

        Windows/Linux：``[新版可执行文件, --update-token … --success-marker …
        --update-version …]``；macOS：``/usr/bin/open -n -a <MangaProof.app> --args …``。
        """
        opts = self.options
        args = [
            state_mod.ARG_TOKEN, opts.token,
            state_mod.ARG_MARKER, str(opts.success_marker),
            state_mod.ARG_VERSION, opts.version,
        ]
        if opts.platform == "macos":
            return (
                ["/usr/bin/open", "-n", "-a", str(opts.install_dir), "--args", *args],
                None,
            )
        entry = opts.install_dir / archive.install_main_rel(opts.platform)
        return ([str(entry), *args], str(opts.install_dir))

    def _launch_new(self) -> None:
        self._phase("LAUNCH_NEW")
        opts = self.options
        marker = Path(opts.success_marker)
        if marker.exists():
            # 清掉可能残留的标记：否则上一次更新的标记会被误判成本次成功（§59）
            self.reporter.log(f"清除残留的成功标记：{marker}")
            state_mod.remove_marker(marker)
            self.state.token = opts.token  # 保证随状态落盘的 token 是本次的
        argv, cwd = self._launch_argv()
        env = privilege.strip_pyinstaller_env()
        self._item(os.path.basename(argv[0]), "启动新版本")
        self.reporter.progress(0, -1)
        user_kwargs = dict(self.rt.user_kwargs)
        try:
            pid = self.rt.ops.spawn(argv, cwd=cwd, env=env, **user_kwargs)
        except OSError as exc:
            if user_kwargs:  # 降权启动失败：退回普通启动（不要因此判更新失败）
                self.reporter.log(f"以降权身份启动新版失败（{exc}），改为普通启动")
                try:
                    pid = self.rt.ops.spawn(argv, cwd=cwd, env=env)
                except OSError as exc2:
                    raise InstallerFailure(
                        ExitCode.LAUNCH, f"新版本无法启动：{argv[0]}（{exc2}）（需求 §62）"
                    ) from exc2
            else:
                raise InstallerFailure(
                    ExitCode.LAUNCH, f"新版本无法启动：{argv[0]}（{exc}）（需求 §62）"
                ) from exc
        self.state.child_pid = int(pid)
        self.store.save(self.state)
        self.reporter.log(f"新版本已启动（pid={pid}），等待成功标记（§59）")

    # -- WAIT_SUCCESS（§59/§60/§61） ------------------------------------ #
    def _wait_success(self) -> None:
        self._phase("WAIT_SUCCESS")
        opts = self.options
        marker = Path(opts.success_marker)
        pid = int(self.state.child_pid)
        deadline = self.rt.now() + self.rt.success_timeout
        self._item(
            f"等待新版本写入成功标记（最长 {self.rt.success_timeout:.0f} 秒）", "等待"
        )
        while True:
            check = state_mod.check_marker(marker, opts.token, opts.version)
            if check.valid:
                self.reporter.log(f"收到有效成功标记：{marker}")
                return
            if check.exists and check.reason != self._last_marker_reason:
                self._last_marker_reason = check.reason
                self.reporter.log(f"暂不接受成功标记：{check.reason}")
            if pid > 0 and not self.rt.ops.alive(pid):
                # 新版已退出：退出前可能刚写完标记，最后再确认一次（§60）
                final = state_mod.check_marker(marker, opts.token, opts.version)
                if final.valid:
                    self.reporter.log(f"收到有效成功标记：{marker}")
                    return
                self._item(f"新版本（pid={pid}）", "已退出但未写成功标记")
                raise InstallerFailure(
                    ExitCode.SUCCESS_TIMEOUT,
                    "新版本已退出但没有写入成功标记，立即回滚（需求 §60）",
                )
            if self.rt.now() >= deadline:
                if pid > 0:
                    self.rt.ops.terminate(pid)  # 超时：尽力结束挂住的新版
                raise InstallerFailure(
                    ExitCode.SUCCESS_TIMEOUT,
                    f"等待成功标记超时（{self.rt.success_timeout:.0f} 秒），"
                    "更新失败（需求 §59/§62）",
                )
            self.rt.sleep(self.rt.poll_interval)

    # -- CLEANUP（§61） ------------------------------------------------- #
    def _cleanup(self) -> None:
        self._phase("CLEANUP")
        opts = self.options
        # 顺序照 §61：删 .old → 删数据备份 → 删更新包 → 删安装器临时目录 → 删标记
        old = Path(self.state.old_dir)
        self._item(old.name, "删除旧版本")
        if not self.rt.remove_tree(
            old, retries=self.rt.retries, delay=self.rt.retry_delay, sleep=self.rt.sleep
        ):
            self.reporter.log(f"警告：旧版本目录删除失败，可手动删除：{old}")
        self._item(Path(opts.data_backup).name, "删除数据备份")
        if not backup.remove_backup_dir(opts.data_backup):
            self.reporter.log(f"警告：数据备份目录删除失败：{opts.data_backup}")
        self._item(opts.package.name, "删除更新包")
        try:
            Path(opts.package).unlink(missing_ok=True)
        except OSError as exc:
            self.reporter.log(f"警告：更新包删除失败：{opts.package}（{exc}）")
        self._cleanup_installer_temp()
        self._item(Path(opts.success_marker).name, "删除成功标记")
        state_mod.remove_marker(opts.success_marker)

    def _cleanup_installer_temp(self) -> None:
        """删除安装器临时目录（§40/§61）。

        安装器自己是那个目录里的 onefile 程序，Windows 上删不掉正在运行的 exe
        （调研报告 §5.1），所以这里**只尽力而为**，失败仅记日志。
        """
        if not getattr(sys, "frozen", False):
            return
        exe = Path(sys.executable)
        temp_dir = exe.parent
        if not temp_dir.name.startswith(INSTALLER_TEMP_PREFIX):
            return
        self._item(temp_dir.name, "删除安装器临时目录")
        self.rt.remove_tree(
            exe, retries=2, delay=self.rt.retry_delay, sleep=self.rt.sleep
        )
        self.rt.remove_tree(
            temp_dir, retries=2, delay=self.rt.retry_delay, sleep=self.rt.sleep
        )

    # -- 失败与回滚（§62/§63） ------------------------------------------ #
    def _handle_failure(self, failure: InstallerFailure) -> int:
        message = str(failure)
        self.reporter.log(f"失败（退出码 {int(failure.code)}）：{message}")
        self.state.error = message
        self.state.rollback_reason = message
        self._phase("ROLLBACK", "正在回滚到旧版本…")
        code = int(failure.code)
        if not Path(self.state.install_dir).is_absolute():
            # 参数阶段就失败了：绝不能拿相对路径去"回滚"（会相对 cwd 乱动文件）
            self.reporter.log("安装参数非法，未触碰任何文件，无需回滚")
            self._phase("FAILED", message)
            self.reporter.finish(False, message)
            return code
        try:
            result = rollback.rollback(
                self.state,
                on_item=lambda rel: self._item(rel, "回滚"),
                on_progress=self.reporter.progress,
                sleep=self.rt.sleep,
                retries=self.rt.retries,
                retry_delay=self.rt.retry_delay,
            )
        except rollback.RollbackError as exc:
            self.reporter.log(f"回滚失败：{exc}")
            self.state.error = f"{message}；回滚失败：{exc}"
            self._phase("FAILED", "回滚失败，安装目录可能不完整")
            self.reporter.finish(False, self.state.error)
            return int(ExitCode.ROLLBACK_FAILED)
        self.reporter.log(f"回滚结果：{result.reason}")
        for action in result.actions:
            self.reporter.log(f"  · {action}")
        self._phase("FAILED", message)
        self.reporter.finish(False, message)
        return code


def run_install(
    options: InstallerOptions,
    *,
    reporter: Reporter | None = None,
    runtime: Runtime | None = None,
) -> int:
    """便捷入口：跑一次安装并返回退出码。"""
    rt = runtime or Runtime()
    if reporter is not None:
        rt.reporter = reporter
    return Installer(options, rt).run()


def default_reporter() -> Reporter:
    """控制台降级用的 reporter（GUI 由 ``ui.run_with_ui`` 提供）。"""
    from updater.ui import ConsoleReporter, MultiReporter

    return MultiReporter(ConsoleReporter(), LoggingReporter())


__all__ = [
    "INSTALLER_TEMP_PREFIX",
    "PARENT_EXIT_TIMEOUT",
    "POLL_INTERVAL",
    "SUCCESS_TIMEOUT",
    "ExitCode",
    "Installer",
    "InstallerFailure",
    "InstallerOptions",
    "ProcessOps",
    "Runtime",
    "build_elevated_argv",
    "default_reporter",
    "rerun_argv_from_process",
    "resolve_state_path",
    "run_install",
]
