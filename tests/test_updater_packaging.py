# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""打包与 CLI 契约单测（需求 §43/§45/§46）。

两件事必须**由测试钉住**，否则很容易在后续改动里悄悄退化：

1. ``packaging/installer.spec`` 是 **onefile + windowed + 不 exclude tkinter**
   （CI 直接按 ``--distpath dist-installer`` 构建，产物名必须对得上）；
2. CLI 入口的参数名/退出码与主程序侧约定一致，且**不依赖 cwd**。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SPEC = ROOT / "packaging" / "installer.spec"

from updater import main as updater_main  # noqa: E402
from updater.installer import ExitCode  # noqa: E402


def test_installer_spec_is_onefile_windowed_without_excluding_tkinter():
    text = SPEC.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )
    assert 'name="MangaProof-update-installer"' in code
    assert "exclude_binaries=False" in code, "onefile 必须显式 exclude_binaries=False"
    assert "console=False" in code
    assert "upx=False" in code
    assert "COLLECT(" not in code, "onefile 不能写 COLLECT"
    assert 'ROOT / "updater" / "main.py"' in code, "入口必须是 updater/main.py"
    # 绝不能 exclude tkinter（GUI 就是它）
    excludes = re.search(r"_excludes\s*=\s*\[(.*?)\]", code, re.S)
    assert excludes is not None
    assert "tkinter" not in excludes.group(1)
    # 三个零依赖模块要显式声明 hiddenimports（安装器不依赖主程序运行环境）
    for module in ("mangaproof.config.user_data", "mangaproof.utils.hashing",
                   "mangaproof.update.checksum"):
        assert module in code
    # 说明这是由 CI 按平台构建
    assert "CI" in text


def test_cli_flags_match_the_cross_process_contract():
    parser = updater_main.build_parser()
    actions = {opt: action for action in parser._actions for opt in action.option_strings}
    for flag in ("--install-dir", "--package", "--data-backup", "--success-marker",
                 "--version", "--sha256", "--parent-pid", "--token", "--platform",
                 "--cli", "--status-file"):
        assert flag in actions, f"缺少参数 {flag}（需求 §45 / 接口契约）"
    for flag in ("--install-dir", "--package", "--data-backup", "--success-marker",
                 "--version", "--token", "--platform"):
        assert actions[flag].required is True
    # 可选参数必须有不报错的默认值
    assert actions["--sha256"].default == ""
    assert actions["--parent-pid"].default == 0
    assert actions["--status-file"].default is None


def test_marker_argv_contract_constants():
    from updater import state as st

    assert st.ARG_TOKEN == "--update-token"
    assert st.ARG_MARKER == "--success-marker"
    assert st.ARG_VERSION == "--update-version"
    assert st.STATE_FILE_NAME == "installer-state.json"


def test_normalize_path_requires_absolute(tmp_path: Path, monkeypatch):
    assert updater_main.normalize_path(str(tmp_path / "a" / ".." / "b")) == tmp_path / "b"
    with pytest.raises(ValueError):
        updater_main.normalize_path("relative/path")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        updater_main.normalize_path("MangaProof")  # cwd 里有同名目录也不行（§45）


def test_parse_options_builds_absolute_paths(tmp_path: Path):
    argv = [
        "--install-dir", str(tmp_path / "MangaProof"),
        "--package", str(tmp_path / "pkg.tar.gz"),
        "--data-backup", str(tmp_path / "backup"),
        "--success-marker", str(tmp_path / "tmp" / "marker.json"),
        "--version", "1.1.0.alpha",
        "--token", "tok",
        "--platform", "linux",
        "--sha256", "a" * 64,
        "--parent-pid", "4242",
        "--cli",
    ]
    options = updater_main.parse_options(argv)
    assert options.install_dir.is_absolute()
    assert options.parent_pid == 4242
    assert options.cli is True
    assert options.state_path == tmp_path / "tmp" / "installer-state.json"


def test_main_returns_usage_code_on_bad_arguments(tmp_path: Path):
    code = updater_main.main(["--status-file", str(tmp_path / "installer-state.json")])
    assert code == int(ExitCode.USAGE)


def test_main_help_exits_zero(capsys):
    assert updater_main.main(["--help"]) == 0
    assert "--install-dir" in capsys.readouterr().out


def test_main_reports_failure_with_nonzero_code(tmp_path: Path):
    """参数齐全但安装目录不存在 → 非 0 退出码 + 可读原因（需求 §45/§62）。"""
    package = tmp_path / "pkg.tar.gz"
    package.write_bytes(b"not a real package")
    status = tmp_path / "installer-state.json"
    code = updater_main.main([
        "--install-dir", str(tmp_path / "missing-install"),
        "--package", str(package),
        "--data-backup", str(tmp_path / "backup"),
        "--success-marker", str(tmp_path / "tmp" / "marker.json"),
        "--version", "1.1.0.alpha",
        "--token", "tok-cli-test",
        "--platform", "linux",
        "--cli",
        "--status-file", str(status),
    ])
    assert code != 0
    assert status.is_file(), "状态文件必须被写出来（§64）"
    # 日志文件名按契约：--status-file 同目录下的 installer-<token>.log
    assert (tmp_path / "installer-tok-cli-test.log").is_file()


def test_setup_logging_redirects_none_streams(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(updater_main, "_LOGGER_READY", False)
    monkeypatch.setattr(updater_main, "_LOG_PATH", None)
    monkeypatch.setattr("sys.stdout", None)
    monkeypatch.setattr("sys.stderr", None)
    argv = ["--status-file", str(tmp_path / "state" / "installer-state.json"),
            "--token", "tok-log"]
    log_path = updater_main.setup_logging(argv)
    import sys

    assert log_path is not None and log_path.is_file()
    assert sys.stdout is not None and sys.stderr is not None
    print("hello")  # 不得 AttributeError
    sys.stdout.flush()
    assert "hello" in log_path.read_text(encoding="utf-8")


def test_scan_value_supports_both_forms():
    assert updater_main._scan_value(["--token", "abc"], "--token") == "abc"
    assert updater_main._scan_value(["--token=abc"], "--token") == "abc"
    assert updater_main._scan_value([], "--token") is None
    assert updater_main._safe_token("../../etc/passwd") == ".._.._etc_passwd"[:24]


def test_excepthook_records_crash(tmp_path: Path):
    status = tmp_path / "installer-state.json"
    updater_main._STATUS_PATH = status
    updater_main._record_crash("RuntimeError: boom")
    payload = updater_main.state_mod.read_json(status)
    assert payload is not None
    assert payload["phase"] == "FAILED" and payload["crashed"] is True
    assert "boom" in payload["error"]
    updater_main._STATUS_PATH = None


def test_rerun_argv_and_elevated_argv(tmp_path: Path):
    from updater.installer import InstallerOptions, build_elevated_argv

    options = InstallerOptions(
        install_dir=tmp_path / "MangaProof",
        package=tmp_path / "pkg.tar.gz",
        data_backup=tmp_path / "backup",
        success_marker=tmp_path / "marker.json",
        version="1.1.0.alpha",
        token="tok",
        platform="linux",
        rerun_argv=("/path/to/installer", "--install-dir", "/x", "--cli"),
    )
    argv = build_elevated_argv(options)
    assert argv == ["/path/to/installer", "--install-dir", "/x", "--cli"]
    assert argv.count("--cli") == 1


def test_state_file_path_follows_the_main_program_contract(tmp_path: Path):
    """状态文件固定叫 installer-state.json，且与 --status-file 同目录。"""
    from updater import state as st
    from updater.installer import InstallerOptions, resolve_state_path

    marker = tmp_path / "tmp" / "marker.json"
    canonical = tmp_path / "state" / st.STATE_FILE_NAME

    # ① 主程序按约定传 installer-state.json → 原样使用
    assert resolve_state_path(canonical, marker) == canonical
    # ② 传了别的名字且文件已存在 → 尊重现状（主程序确实在用它）
    other = tmp_path / "state" / "installer-status.json"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("{}", encoding="utf-8")
    assert resolve_state_path(other, marker) == other
    # ③ 传了别的名字但不存在 → 用同目录下的约定文件名，不另造新名字
    assert resolve_state_path(tmp_path / "state" / "whatever.json", marker) == canonical
    # ④ 完全没传 → 与 success-marker 同目录
    assert resolve_state_path(None, marker) == marker.parent / st.STATE_FILE_NAME

    options = InstallerOptions(
        install_dir=tmp_path / "MangaProof", package=tmp_path / "pkg.tar.gz",
        data_backup=tmp_path / "backup", success_marker=marker,
        version="1.1.0.alpha", token="t", platform="linux", status_file=canonical,
    )
    assert options.state_path == canonical


def test_privilege_run_direct_is_plain_subprocess():
    """不需要提权时直接普通执行（需求 §42 的第一条分支）。"""
    from updater import privilege

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    calls = []

    def runner(argv, *, env=None):
        calls.append((list(argv), dict(env or {})))
        return FakeResult()

    assert privilege.run_direct(["true"], runner=runner, env={"_PYI_X": "1", "PATH": "/usr/bin"}) == 0
    argv, env = calls[0]
    assert argv == ["true"]
    assert "_PYI_X" not in env and env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"

    class Fail(FakeResult):
        returncode = 3

    with pytest.raises(privilege.PrivilegeError):
        privilege.run_direct(["false"], runner=lambda *a, **k: Fail(), env={})
