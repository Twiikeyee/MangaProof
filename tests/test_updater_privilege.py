# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""权限检测与提权链单测（需求 §39/§42，调研报告 §5.1~§5.3）。

**不真的提权**：所有执行路径都注入 fake runner / fake ``which``，只断言：

- §39 的写权限探测方式（建→删→清残留），且不用 ``os.access`` 下结论；
- Linux 降级链顺序与返回码语义（``126`` 取消=停止降级；``127`` 不可用=降级）；
- Windows ``1223`` / macOS ``-128`` 的取消判定；
- 提权前清理 ``_PYI_*``（调研报告 §5.1 R4）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from updater import privilege


class FakeResult:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


class FakeRunner:
    """按顺序返回预设返回码，并记录每次调用的 argv/env。"""

    def __init__(self, codes: list[int]) -> None:
        self.codes = list(codes)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, *, env=None, **_kwargs):
        self.calls.append(([str(a) for a in argv], dict(env or {})))
        code = self.codes.pop(0) if self.codes else 0
        return FakeResult(code)


class FakeWhich:
    def __init__(self, available: dict[str, str]) -> None:
        self.available = available

    def __call__(self, name: str) -> str | None:
        return self.available.get(name)


# --------------------------------------------------------------------------- #
# §39 写权限探测
# --------------------------------------------------------------------------- #


def test_elevation_probe_path_matches_requirement(tmp_path: Path):
    install = tmp_path / "example" / "MangaProof"
    assert privilege.elevation_probe_path(install) == tmp_path / "example" / "MangaProof-upgrade-test"


def test_can_write_install_probe_cleans_up_and_detects_writable(tmp_path: Path):
    install = tmp_path / "MangaProof"
    install.mkdir()
    assert privilege.can_write_install_probe(install) is True
    assert not privilege.elevation_probe_path(install).exists(), "探测残留必须清掉（需求 §39）"


def test_can_write_install_probe_detects_unwritable(tmp_path: Path):
    install = tmp_path / "MangaProof"
    install.mkdir()
    blocked = tmp_path / "MangaProof-upgrade-test"
    # 用一个同名**文件**占位，mkdir 必然失败 → 探测判定不可写
    blocked.write_text("occupied", encoding="utf-8")
    assert privilege.can_write_install_probe(install) is False
    assert blocked.is_file(), "失败时也不能破坏现场，只清理自己创建的残留"


def test_needs_elevation_uses_write_probe_not_os_access(tmp_path: Path, monkeypatch):
    install = tmp_path / "MangaProof"
    install.mkdir()
    monkeypatch.setattr(privilege, "is_admin", lambda: False)
    assert privilege.needs_elevation(install) is False

    monkeypatch.setattr(privilege, "can_write_install_probe", lambda _p: False)
    assert privilege.needs_elevation(install) is True


def test_needs_elevation_is_false_when_already_admin(tmp_path: Path, monkeypatch):
    install = tmp_path / "MangaProof"
    install.mkdir()
    monkeypatch.setattr(privilege, "is_admin", lambda: True)
    monkeypatch.setattr(privilege, "can_write_install_probe", lambda _p: False)
    assert privilege.needs_elevation(install) is False


def test_needs_elevation_when_parent_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(privilege, "is_admin", lambda: False)
    assert privilege.needs_elevation(tmp_path / "missing" / "MangaProof") is True


def test_can_write_directory_is_real_write(tmp_path: Path):
    assert privilege.can_write_directory(tmp_path) is True
    assert list(tmp_path.iterdir()) == [], "探测目录/文件必须被清理"


# --------------------------------------------------------------------------- #
# Linux 降级链
# --------------------------------------------------------------------------- #


def test_linux_plan_order_pkexec_then_sudo_askpass():
    which = FakeWhich({"pkexec": "/usr/bin/pkexec", "sudo": "/usr/bin/sudo",
                       "zenity": "/usr/bin/zenity"})
    plans = privilege.iter_linux_plans(["installer", "--cli"], which=which)
    assert [p.kind for p in plans] == ["pkexec", "sudo-askpass"]
    assert plans[0].argv[:3] == ["/usr/bin/pkexec", "--disable-internal-agent", "installer"]
    assert plans[1].argv[:2] == ["/usr/bin/sudo", "-A"]
    assert "zenity" in plans[1].env["SUDO_ASKPASS"]
    assert plans[1].env["SUDO_ASKPASS"].endswith("--password")


@pytest.mark.parametrize("helper", ["zenity", "kdialog", "ssh-askpass"])
def test_askpass_helper_candidates(helper: str):
    which = FakeWhich({helper: f"/usr/bin/{helper}"})
    assert privilege.askpass_helper(which) == f"/usr/bin/{helper}"


def test_no_mechanism_gives_manual_instructions():
    with pytest.raises(privilege.PrivilegeUnavailable) as exc:
        privilege.run_elevated(["installer", "--cli"], platform="linux",
                              which=FakeWhich({}), runner=FakeRunner([]))
    text = str(exc.value)
    assert "终端" in text and "sudo installer --cli" in text


def test_run_elevated_success_returns_zero():
    which = FakeWhich({"pkexec": "/usr/bin/pkexec"})
    runner = FakeRunner([0])
    code = privilege.run_elevated(["installer"], platform="linux", which=which, runner=runner)
    assert code == 0
    assert len(runner.calls) == 1


def test_linux_126_means_user_cancelled_and_stops_downgrade():
    """``126`` = 用户取消：**不重试、不降级**（调研报告 §5.2）。"""
    which = FakeWhich({"pkexec": "/usr/bin/pkexec", "sudo": "/usr/bin/sudo",
                       "zenity": "/usr/bin/zenity"})
    runner = FakeRunner([126, 0])
    with pytest.raises(privilege.PrivilegeCancelled):
        privilege.run_elevated(["installer"], platform="linux", which=which, runner=runner)
    assert len(runner.calls) == 1, "取消后绝不能再试下一个机制"


def test_linux_127_means_unavailable_and_downgrades():
    which = FakeWhich({"pkexec": "/usr/bin/pkexec", "sudo": "/usr/bin/sudo",
                       "zenity": "/usr/bin/zenity"})
    runner = FakeRunner([127, 0])
    code = privilege.run_elevated(["installer"], platform="linux", which=which, runner=runner)
    assert code == 0
    assert len(runner.calls) == 2
    assert runner.calls[0][0][0].endswith("pkexec")
    assert runner.calls[1][0][:2] == ["/usr/bin/sudo", "-A"]


def test_linux_all_unavailable_falls_back_to_manual():
    which = FakeWhich({"pkexec": "/usr/bin/pkexec", "sudo": "/usr/bin/sudo",
                       "zenity": "/usr/bin/zenity"})
    runner = FakeRunner([127, 127])
    with pytest.raises(privilege.PrivilegeUnavailable) as exc:
        privilege.run_elevated(["installer"], platform="linux", which=which, runner=runner)
    assert "终端" in str(exc.value)
    assert len(runner.calls) == 2


def test_linux_other_returncode_is_error():
    which = FakeWhich({"pkexec": "/usr/bin/pkexec"})
    with pytest.raises(privilege.PrivilegeError):
        privilege.run_elevated(["installer"], platform="linux", which=which,
                              runner=FakeRunner([1]))


def test_linux_runner_oserror_downgrades():
    which = FakeWhich({"pkexec": "/usr/bin/pkexec", "sudo": "/usr/bin/sudo",
                       "zenity": "/usr/bin/zenity"})
    codes = [0]

    def runner(argv, *, env=None, **_kw):
        if "pkexec" in argv[0]:
            raise FileNotFoundError("pkexec 不见了")
        return FakeResult(codes.pop(0) if codes else 0)

    code = privilege.run_elevated(["installer"], platform="linux", which=which, runner=runner)
    assert code == 0


# --------------------------------------------------------------------------- #
# Windows / macOS 语义（纯函数，不依赖真实平台）
# --------------------------------------------------------------------------- #


def test_windows_error_1223_is_user_cancel():
    with pytest.raises(privilege.PrivilegeCancelled):
        privilege.interpret_windows_error(privilege.ERROR_CANCELLED)
    assert privilege.ERROR_CANCELLED == 1223
    with pytest.raises(privilege.PrivilegeError):
        privilege.interpret_windows_error(5)
    privilege.interpret_windows_error(0)  # 无错误：什么都不做


@pytest.mark.parametrize(
    "code,stderr,expect_cancel",
    [
        (-128, "", True),
        (1, "execution error: User canceled. (-128)", True),
        (1, "some other failure", False),
        (0, "", False),
    ],
)
def test_macos_cancel_detection(code: int, stderr: str, expect_cancel: bool):
    assert privilege.macos_cancelled(code, stderr) is expect_cancel


def test_macos_plan_uses_osascript_style_description():
    plan = privilege.build_elevation_plan(["installer"], platform="macos",
                                         which=FakeWhich({}))
    assert plan.kind == "macos-osascript"
    assert plan.argv == ["installer"]


def test_windows_plan_uses_runas():
    plan = privilege.build_elevation_plan(["installer"], platform="windows",
                                         which=FakeWhich({}))
    assert plan.kind == "windows-runas"


# --------------------------------------------------------------------------- #
# 环境处理
# --------------------------------------------------------------------------- #


def test_strip_pyinstaller_env():
    env = {"_PYI_ARCHIVE_FILE": "/tmp/x", "_PYI_APPLICATION_HOME_DIR": "/tmp",
           "PATH": "/usr/bin", "PYINSTALLER_RESET_ENVIRONMENT": "0"}
    cleaned = privilege.strip_pyinstaller_env(env)
    assert "PATH" in cleaned
    assert not any(key.startswith("_PYI_") for key in cleaned)
    assert cleaned["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


@pytest.mark.skipif(os.name == "nt", reason="uid/gid 概念只存在于 POSIX")
def test_invoking_user_from_pkexec_and_sudo():
    assert privilege.invoking_user({"PKEXEC_UID": "1000"}) == (1000, 1000)
    assert privilege.invoking_user({"SUDO_UID": "1001", "SUDO_GID": "1002"}) == (1001, 1002)
    assert privilege.invoking_user({"PKEXEC_UID": "0"}) is None
    assert privilege.invoking_user({}) is None
    assert privilege.invoking_user({"PKEXEC_UID": "abc"}) is None


@pytest.mark.skipif(os.name == "nt", reason="Popen(user=...) 只支持 POSIX")
def test_child_user_kwargs_only_when_elevated(monkeypatch):
    monkeypatch.setattr(privilege, "is_admin", lambda: True)
    kwargs = privilege.child_user_kwargs({"PKEXEC_UID": "1000", "SUDO_GID": "1000"})
    assert kwargs == {"user": 1000, "group": 1000}

    monkeypatch.setattr(privilege, "is_admin", lambda: False)
    assert privilege.child_user_kwargs({"PKEXEC_UID": "1000"}) == {}
    monkeypatch.setattr(privilege, "is_admin", lambda: True)
    assert privilege.child_user_kwargs({}) == {}


def test_manual_instructions_include_command():
    text = privilege.manual_instructions(["MangaProof-update-installer", "--cli"],
                                        platform="linux")
    assert "MangaProof-update-installer" in text and "--cli" in text
    win = privilege.manual_instructions(["x.exe"], platform="windows")
    assert "管理员" in win


def test_current_platform_is_known():
    assert privilege.current_platform() in ("windows", "linux", "macos")


def test_default_runner_signature_is_subprocess_compatible():
    """默认 runner 必须能把 argv/env 交给 subprocess（不真的执行提权命令）。"""
    result = privilege._default_runner(["/bin/sh", "-c", "exit 7"], env=dict(os.environ))
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 7
