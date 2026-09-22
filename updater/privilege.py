# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""平台专有权限检测与提权（需求 §39/§42，调研报告 §5.1~§5.3）。

两条原则，改代码前先读：

1. **安装器本体不提权启动**（需求 §42「主程序本身不重启为管理员」的同一条思路）：
   安装器先以普通身份跑；只有在 :func:`needs_elevation` 判定"安装目录父目录写不进去"
   时，才把**安装逻辑**交给平台原生授权机制去执行一个 ``--cli`` 子进程
   （调研报告 §5.2：pkexec 只能跑 CLI 逻辑，默认会清洗 ``DISPLAY``/``XAUTHORITY``，
   不能用来启动 GUI）。提权只用于文件操作，启动新版用非提权方式。
2. **平台专有代码运行时导入**（需求 §6）：``ctypes.windll`` 之类只在真的跑到
   那个分支时才 import，Linux/macOS 上不会因为导入本模块而加载 Windows 专有代码。

各平台机制与返回码语义（全部来自调研报告 §5）：

===========  ============================================================  ==================================
平台         机制                                                          取消 / 不可用的判定
===========  ============================================================  ==================================
Windows      ``ShellExecuteExW`` + ``"runas"``                             ``GetLastError()==1223`` = 用户取消
Linux        ``pkexec --disable-internal-agent`` → ``sudo -A`` + askpass     ``126`` = 取消（停止降级）；``127`` = 机制不可用（降级）
macOS        ``osascript -e 'do shell script … with administrator privileges'``  ``-128`` / ``User canceled.`` = 用户取消
===========  ============================================================  ==================================

Windows 用 ``ShellExecuteW`` 拿不到进程句柄与准确错误码，必须用 ``ShellExecuteExW``
+ ``SEE_MASK_NOCLOSEPROCESS|SEE_MASK_NOASYNC``，调用前 ``CoInitializeEx``（§5.1）。
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

log = logging.getLogger("mangaproof.updater.privilege")

#: Windows UAC 取消（ERROR_CANCELLED）
ERROR_CANCELLED = 1223
#: Linux：用户取消（pkexec/sudo 的约定返回码）
LINUX_CANCELLED = 126
#: Linux：机制不可用（才允许降级）
LINUX_UNAVAILABLE = 127
#: macOS：AppleScript 用户取消
MACOS_CANCELLED = -128

#: Linux 上的 sudo 图形化问密码助手（按可用性顺序）
ASKPASS_CANDIDATES = ("zenity", "kdialog", "ssh-askpass", "lxqt-openssh-askpass")

#: sudo 的 ``-A`` 需要 ``SUDO_ASKPASS`` 才能弹窗；zenity/kdialog 需要不同参数
_ASKPASS_ARGS: dict[str, str] = {
    "zenity": "--password",
    "kdialog": "--password",
}

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class PrivilegeError(Exception):
    """提权相关的通用失败。"""


class PrivilegeCancelled(PrivilegeError):
    """用户在授权对话框里点了取消：**不重试、不降级**（§5.1/§5.2）。"""


class PrivilegeUnavailable(PrivilegeError):
    """当前环境没有可用的提权机制：可以降级为"提示用户手动执行"。"""


@dataclass
class ElevationPlan:
    """一次提权的具体执行计划（便于日志与单测断言）。"""

    kind: str  # pkexec | sudo-askpass | windows-runas | macos-osascript | manual
    argv: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""

    @property
    def usable(self) -> bool:
        return self.kind != "manual"


# --------------------------------------------------------------------------- #
# 身份与写权限检测
# --------------------------------------------------------------------------- #


def current_platform() -> str:
    """当前平台的 ``--platform`` 取值。"""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def is_admin() -> bool:
    """当前进程是否已具备管理员/root 权限。"""
    if os.name == "nt":
        try:  # 运行时导入 Windows 专有模块（需求 §6）
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - 依赖 Windows API
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:  # pragma: no cover - 非 POSIX
        return False


def ensure_safe_cwd(install_dir: Path, *, cwd: Path | None = None) -> Path:
    """把安装器的当前工作目录挪出安装目录（Windows 上必须，需求 §54）。

    Windows **不允许重命名"某个正在运行的进程的当前目录"**。安装器要做的第一件
    破坏性操作就是把 ``S:\\MangaProof`` 改名成 ``S:\\MangaProof.old``；如果它是被
    从安装目录启动的主程序拉起来的（用户双击 exe 时 cwd 就是安装目录），它会继承
    这个 cwd，于是改名必然失败：::

        [WinError 32] 另一个程序正在使用此文件，进程无法访问。
        'S:\\MangaProof' -> 'S:\\MangaProof.old'

    报错里的"另一个程序"就是安装器自己 —— **主程序确实已经退出**，日志里那句
    "主程序已退出"是对的，问题不在退出检测。新版主程序已不再把安装目录当 cwd
    传下去（见 ``update/platform/windows.py`` 的 ``safe_working_dir``），但用户
    可能拿到的是**旧版主程序**拉起的**新版安装器**，所以这里再兜一道。

    挪到哪儿：安装目录的父目录（那层不会被改名，子目录被占用不影响父目录改名）；
    父目录不可用时退回系统临时目录。

    :returns: 最终生效的工作目录（没改动时就是原 cwd）。
    """
    current = Path(cwd) if cwd is not None else Path.cwd()
    target_dir = Path(install_dir)
    try:
        inside = current == target_dir or target_dir in current.parents
    except OSError:  # pragma: no cover - 路径异常时按"需要挪"处理
        inside = True
    if not inside:
        return current

    for candidate in (target_dir.parent, Path(tempfile.gettempdir())):
        try:
            if candidate.is_dir():
                os.chdir(candidate)
                log.info(
                    "安装器的 cwd 原本在安装目录内（%s），已挪到 %s"
                    "（否则 Windows 无法重命名安装目录）",
                    current, candidate,
                )
                return candidate
        except OSError as exc:  # pragma: no cover - 目录不可用
            log.warning("切换工作目录到 %s 失败：%s", candidate, exc)
    log.warning("无法把安装器的 cwd 挪出安装目录：%s", current)
    return current


def elevation_probe_path(install_dir: Path) -> Path:
    """§39 规定的探测路径：``<父目录>/<安装目录名>-upgrade-test``。

    例：安装目录 ``~/example/MangaProof`` → 探测 ``~/example/MangaProof-upgrade-test``。
    """
    path = Path(install_dir)
    return path.parent / f"{path.name}-upgrade-test"


def can_write_directory(directory: Path) -> bool:
    """真的建一个目录+文件再删掉，而不是只看 ``os.access``（需求 §39 明令禁止）。"""
    probe_dir = Path(directory) / f".mangaproof-write-probe-{os.getpid()}-{int(time.time()*1000) % 100000}"
    probe_file = probe_dir / "probe"
    try:
        probe_dir.mkdir(parents=True, exist_ok=False)
        probe_file.write_bytes(b"")
        return True
    except OSError:
        return False
    finally:
        try:
            if probe_file.exists():
                probe_file.unlink()
            if probe_dir.exists():
                probe_dir.rmdir()
        except OSError:  # 残留清理失败：只记日志，不影响判定
            log.debug("写权限探测残留清理失败：%s", probe_dir)


def can_write_install_probe(install_dir: Path) -> bool:
    """按 §39 的方式探测"安装目录父目录"的写入能力（含残留清理）。"""
    probe = elevation_probe_path(install_dir)
    try:
        probe.mkdir(parents=True, exist_ok=False)
        (probe / "probe").write_bytes(b"")
        return True
    except OSError:
        return False
    finally:
        try:
            if probe.exists():
                shutil.rmtree(probe, ignore_errors=True)
        except OSError:  # pragma: no cover
            pass


def needs_elevation(install_dir: Path) -> bool:
    """是否需要提权（需求 §39：检测安装目录**父目录**的实际写入能力）。"""
    if is_admin():
        return False
    parent = Path(install_dir).parent
    if not parent.exists():
        return True
    return not can_write_install_probe(install_dir)


# --------------------------------------------------------------------------- #
# 环境处理
# --------------------------------------------------------------------------- #


def strip_pyinstaller_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """清掉 ``_PYI_*`` 并设 ``PYINSTALLER_RESET_ENVIRONMENT=1``（调研报告 §5.1 R4）。

    PyInstaller 6.22.3+ 的 onefile 会对"UAC 提权 + 继承非提权用户环境变量"的进程
    做父进程校验，不处理会直接启动失败。
    """
    source = dict(os.environ if env is None else env)
    cleaned = {k: v for k, v in source.items() if not k.startswith("_PYI_")}
    cleaned["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return cleaned


def invoking_user(
    env: Mapping[str, str] | None = None, *, euid: int | None = None
) -> tuple[int, int] | None:
    """提权前的调用者 uid/gid（``pkexec`` 给 ``PKEXEC_UID``，``sudo`` 给 ``SUDO_UID``）。

    用于以**非提权身份**启动新版主程序（调研报告 §5.1/§5.3：不要让新版继承
    管理员完整性级别）。拿不到就返回 ``None``。
    """
    if os.name == "nt":
        return None
    source = dict(os.environ if env is None else env)
    uid_text = source.get("PKEXEC_UID") or source.get("SUDO_UID") or ""
    try:
        uid = int(uid_text)
    except (TypeError, ValueError):
        return None
    if uid == 0:
        return None
    gid_text = source.get("SUDO_GID") or ""
    try:
        gid = int(gid_text)
    except (TypeError, ValueError):
        gid = uid
    return uid, gid


def child_user_kwargs(env: Mapping[str, str] | None = None) -> dict[str, int]:
    """``subprocess.Popen`` 的降权参数（仅"已提权且知道原用户"时非空）。"""
    if os.name == "nt" or not is_admin():
        return {}
    user = invoking_user(env)
    if not user:
        return {}
    uid, gid = user
    return {"user": uid, "group": gid}


# --------------------------------------------------------------------------- #
# 提权执行
# --------------------------------------------------------------------------- #


def askpass_helper(which: Callable[[str], str | None] = shutil.which) -> str | None:
    """找一个可用的图形化问密码助手（``SUDO_ASKPASS``）。"""
    for name in ASKPASS_CANDIDATES:
        found = which(name)
        if found:
            return found
    return None


def build_sudo_askpass_env(
    helper: str, env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """构造 ``SUDO_ASKPASS`` 环境（zenity/kdialog 需要额外参数）。"""
    result = dict(os.environ if env is None else env)
    args = _ASKPASS_ARGS.get(Path(helper).name)
    result["SUDO_ASKPASS"] = f"{helper} {args}" if args else helper
    return result


def manual_instructions(
    argv: Sequence[str], *, platform: str | None = None
) -> str:
    """所有机制都不可用时的兜底文案：让用户复制命令到终端执行（§5.2）。"""
    command = shlex.join([str(a) for a in argv])
    plat = platform or current_platform()
    if plat == "windows":
        return (
            "请在“以管理员身份运行”的终端（PowerShell/CMD）里执行：\n"
            f"  {command}"
        )
    if plat == "macos":
        return (
            "没有可用的图形化提权机制，请手动在终端里执行（会要求输入密码）：\n"
            f"  sudo {command}"
        )
    return (
        "没有可用的图形化提权机制，请手动在终端里执行（会要求输入密码）：\n"
        f"  sudo {command}"
    )


def build_elevation_plan(
    argv: Sequence[str],
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    env: Mapping[str, str] | None = None,
) -> ElevationPlan:
    """按平台机制生成**首要**提权计划；Linux 的降级链由 :func:`iter_linux_plans` 给出。"""
    plat = platform or current_platform()
    command = [str(a) for a in argv]
    if plat == "windows":  # pragma: no cover - 平台分支
        return ElevationPlan(
            kind="windows-runas",
            argv=command,
            env=strip_pyinstaller_env(env),
            description="Windows UAC（ShellExecuteExW + runas）",
        )
    if plat == "macos":  # pragma: no cover - 平台分支
        return ElevationPlan(
            kind="macos-osascript",
            argv=command,
            env=strip_pyinstaller_env(env),
            description="macOS 管理员授权（osascript with administrator privileges）",
        )
    plans = iter_linux_plans(command, which=which, env=env)
    if plans:
        return plans[0]
    return ElevationPlan(
        kind="manual",
        argv=command,
        env=dict(os.environ if env is None else env),
        description=manual_instructions(command, platform="linux"),
    )


def iter_linux_plans(
    argv: Sequence[str],
    *,
    which: Callable[[str], str | None] = shutil.which,
    env: Mapping[str, str] | None = None,
) -> list[ElevationPlan]:
    """Linux 降级链（调研报告 §5.2）：``pkexec`` → ``sudo -A`` + askpass → 手动。

    提权前先清 ``_PYI_*``：pkexec 会清环境，但 sudo 不会。
    """
    command = [str(a) for a in argv]
    plans: list[ElevationPlan] = []
    pkexec = which("pkexec")
    if pkexec:
        plans.append(
            ElevationPlan(
                kind="pkexec",
                argv=[pkexec, "--disable-internal-agent", *command],
                env=strip_pyinstaller_env(env),
                description="pkexec（PolicyKit 图形授权）",
            )
        )
    sudo = which("sudo")
    helper = askpass_helper(which)
    if sudo and helper:
        plans.append(
            ElevationPlan(
                kind="sudo-askpass",
                argv=[sudo, "-A", *command],
                env=build_sudo_askpass_env(helper, env),
                description=f"sudo -A（SUDO_ASKPASS={helper}）",
            )
        )
    return plans


def _default_runner(argv: Sequence[str], *, env: Mapping[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(a) for a in argv],
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
    )


def run_direct(
    argv: Sequence[str],
    *,
    runner: Runner | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """**不提权**直接执行（需求 §42 的第一条分支：无需提权就普通运行）。

    提权只用于文件操作；本函数给"不需要管理员权限"的路径用，语义就是
    ``subprocess``（同样清掉 ``_PYI_*``，避免 onefile 环境变量串味）。
    """
    cleaned = strip_pyinstaller_env(env)
    result = (runner or _default_runner)([str(a) for a in argv], env=cleaned)
    code = int(result.returncode)
    if code != 0:
        raise PrivilegeError(f"命令执行失败（退出码 {code}）：{argv[0] if argv else ''}")
    return code


def interpret_linux_returncode(code: int, *, plan_kind: str) -> None:
    """Linux 返回码语义（§5.2）：126 = 取消（不降级）；127 = 机制不可用（降级）。"""
    if code == 0:
        return
    if code == LINUX_CANCELLED:
        raise PrivilegeCancelled("用户取消了授权，已停止更新（不会再降级尝试）")
    if code == LINUX_UNAVAILABLE:
        raise PrivilegeUnavailable(f"提权机制不可用：{plan_kind}")
    raise PrivilegeError(f"提权执行失败（{plan_kind}，退出码 {code}）")


def interpret_windows_error(code: int) -> None:
    """Windows ``GetLastError`` 语义：1223 = 用户在 UAC 取消。"""
    if code == ERROR_CANCELLED:
        raise PrivilegeCancelled("用户在 UAC 提示中取消了授权")
    if code:
        raise PrivilegeError(f"提权启动失败（Windows 错误码 {code}）")


def macos_cancelled(returncode: int, stderr: str) -> bool:
    """macOS 取消判定：返回 ``-128`` 或 stderr 含 ``User canceled.``。"""
    return returncode == MACOS_CANCELLED or "User canceled" in (stderr or "")


def run_elevated(
    argv: Sequence[str],
    *,
    platform: str | None = None,
    runner: Runner | None = None,
    which: Callable[[str], str | None] = shutil.which,
    env: Mapping[str, str] | None = None,
) -> int:
    """以提权方式执行 ``argv`` 并返回其退出码。

    - 用户取消 → :class:`PrivilegeCancelled`（调用方必须停止，不再重试/降级）；
    - 机制不可用 → :class:`PrivilegeUnavailable`（调用方可提示手动执行）；
    - 其他失败 → :class:`PrivilegeError`。
    """
    plat = platform or current_platform()
    command = [str(a) for a in argv]
    if plat == "windows":  # pragma: no cover - 平台分支
        return _run_elevated_windows(command, env=env)
    if plat == "macos":  # pragma: no cover - 平台分支
        return _run_elevated_macos(command, runner=runner, env=env)

    plans = iter_linux_plans(command, which=which, env=env)
    if not plans:
        raise PrivilegeUnavailable(manual_instructions(command, platform="linux"))
    last_error: Exception | None = None
    for plan in plans:
        log.info("尝试提权：%s", plan.description)
        try:
            result = (runner or _default_runner)(plan.argv, env=plan.env)
        except OSError as exc:
            last_error = PrivilegeUnavailable(f"{plan.description} 无法执行：{exc}")
            log.warning("%s", last_error)
            continue
        try:
            interpret_linux_returncode(int(result.returncode), plan_kind=plan.kind)
            return int(result.returncode)
        except PrivilegeUnavailable as exc:  # 127：降级到下一个机制
            last_error = exc
            log.warning("%s（降级尝试下一个机制）", exc)
            continue
    if last_error is not None:
        raise PrivilegeUnavailable(
            f"{last_error}\n{manual_instructions(command, platform='linux')}"
        ) from last_error
    raise PrivilegeUnavailable(manual_instructions(command, platform="linux"))


def _run_elevated_windows(  # pragma: no cover - 依赖 Windows API
    command: Sequence[str], *, env: Mapping[str, str] | None = None
) -> int:
    """Windows：``ShellExecuteExW`` + ``runas``（§5.1）。"""
    import ctypes
    from ctypes import wintypes

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NOASYNC = 0x00000100
    SW_SHOWNORMAL = 1
    INFINITE = 0xFFFFFFFF

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    kernel32 = ctypes.windll.kernel32

    ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    params = " ".join(subprocess.list2cmdline([str(a)]) for a in command[1:])
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = str(command[0])
    info.lpParameters = params or None
    info.nShow = SW_SHOWNORMAL
    try:
        ok = shell32.ShellExecuteExW(ctypes.byref(info))
        if not ok:
            interpret_windows_error(int(kernel32.GetLastError()))
            raise PrivilegeError("提权启动失败（ShellExecuteExW 返回失败）")
        if not info.hProcess:
            raise PrivilegeError("提权进程句柄为空（无法确认是否启动成功）")
        kernel32.WaitForSingleObject(info.hProcess, INFINITE)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        kernel32.CloseHandle(info.hProcess)
        return int(code.value)
    finally:
        ole32.CoUninitialize()


def _run_elevated_macos(  # pragma: no cover - 平台分支
    command: Sequence[str],
    *,
    runner: Runner | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """macOS：``osascript -e 'do shell script "…" with administrator privileges'``。"""
    script = "do shell script {} with administrator privileges".format(
        '"' + shlex.join(command).replace("\\", "\\\\").replace('"', '\\"') + '"'
    )
    result = (runner or _default_runner)(
        ["osascript", "-e", script], env=dict(os.environ if env is None else env)
    )
    stderr = getattr(result, "stderr", "") or ""
    if macos_cancelled(int(result.returncode), stderr):
        raise PrivilegeCancelled("用户在 macOS 授权对话框中取消了授权")
    if int(result.returncode) != 0:
        raise PrivilegeError(
            f"提权执行失败（osascript，退出码 {result.returncode}）：{stderr.strip()}"
        )
    return int(result.returncode)


__all__ = [
    "ASKPASS_CANDIDATES",
    "ERROR_CANCELLED",
    "ElevationPlan",
    "LINUX_CANCELLED",
    "LINUX_UNAVAILABLE",
    "MACOS_CANCELLED",
    "PrivilegeCancelled",
    "PrivilegeError",
    "PrivilegeUnavailable",
    "askpass_helper",
    "build_elevation_plan",
    "build_sudo_askpass_env",
    "can_write_directory",
    "can_write_install_probe",
    "child_user_kwargs",
    "current_platform",
    "elevation_probe_path",
    "interpret_linux_returncode",
    "interpret_windows_error",
    "invoking_user",
    "is_admin",
    "iter_linux_plans",
    "macos_cancelled",
    "manual_instructions",
    "needs_elevation",
    "run_direct",
    "run_elevated",
    "strip_pyinstaller_env",
]
