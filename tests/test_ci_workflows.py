# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""CI 工作流里 PyInstaller 调用点的守卫（只用标准库，不引入 PyYAML）。

起因是一个真实事故：给三个桌面 job 批量插入"构建安装器"步骤时，替换模板把
`run:` 行**硬编码**成了 `"${{ matrix.spec }}"`，而 Windows job 没有 matrix ——
展开成空串，CI 报 `Script file '' does not exist`，本地 pytest 完全发现不了
（YAML 依然合法）。所以这里对 workflow 文本做结构性断言。

运行：uv run python -m pytest tests/test_ci_workflows.py -v
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

BUILD = ROOT / ".github" / "workflows" / "build.yml"
TEXT = BUILD.read_text(encoding="utf-8")
LINES = TEXT.splitlines()

_JOB_RE = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")


def _job_blocks() -> dict[str, list[str]]:
    """把 build.yml 按 job 切成块（顶层两空格缩进的键）。"""
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in LINES:
        m = _JOB_RE.match(line)
        if m and not line.startswith("    "):
            current = m.group(1)
            blocks[current] = []
            continue
        if current is not None:
            blocks[current].append(line)
    # 去掉非 job 的顶层块（on / permissions / env 等）
    return {k: v for k, v in blocks.items() if any(l.strip().startswith("- ") for l in v)}


def _pyinstaller_calls() -> list[str]:
    """所有 `pyinstaller … --clean <spec>` 调用行（含多行 run 块里的那一行）。"""
    return [l.strip() for l in LINES if "pyinstaller" in l and "--clean" in l]


@pytest.mark.parametrize("call", _pyinstaller_calls())
def test_pyinstaller_call_has_a_spec(call: str):
    rest = call.split("--clean", 1)[1].strip().rstrip("\\").strip()
    assert rest not in ('""', "''", ""), f"pyinstaller 的 spec 参数为空：{call!r}"
    assert rest.endswith(".spec") or rest == '"${{ matrix.spec }}"', (
        f"pyinstaller 的 spec 参数看起来不对：{call!r}"
    )


def test_pyinstaller_calls_are_found():
    """每个桌面 job 有两次调用：先建安装器，再建主程序（共 3 + 3）。"""
    calls = _pyinstaller_calls()
    installer = [c for c in calls if "packaging/installer.spec" in c]
    main = [c for c in calls if "packaging/installer.spec" not in c]
    assert len(installer) == 3, f"安装器构建应出现在 3 个桌面 job 里：{installer}"
    assert len(main) == 3, f"主程序构建应出现在 3 个桌面 job 里：{main}"


def test_matrix_spec_jobs_define_spec():
    """用到 matrix.spec 的 job，其矩阵 include 里必须有 spec 字段。"""
    for job, lines in _job_blocks().items():
        body = "\n".join(lines)
        if "matrix.spec" not in body:
            continue
        assert re.search(r"^\s+spec:\s+\S", body, re.M), (
            f"{job} 用了 matrix.spec，但矩阵里没有 spec 字段（会展开成空串）"
        )


def test_windows_job_uses_a_fixed_spec():
    """Windows job 没有矩阵，必须用固定 spec 路径（就是这次踩的坑）。"""
    windows = _job_blocks().get("build-windows")
    assert windows, "找不到 build-windows job"
    body = "\n".join(windows)
    assert "matrix.spec" not in body, "build-windows 没有矩阵，不得引用 matrix.spec"
    assert "packaging/main_win.spec" in body


def test_referenced_specs_exist():
    for spec in set(re.findall(r"packaging/[\w./-]+\.spec", TEXT)):
        assert (ROOT / spec).is_file(), f"workflow 引用的 spec 不存在：{spec}"
    for spec in set(re.findall(r"spec:\s+(\S+\.spec)", TEXT)):
        assert (ROOT / spec).is_file(), f"矩阵里的 spec 不存在：{spec}"


def test_installer_built_before_main_app_and_tkinter_checked():
    """每个桌面 job：先查 tkinter → 再建安装器 → 最后建主程序。"""
    desktop = [j for j in _job_blocks() if j.startswith("build-")]
    assert len(desktop) == 3, desktop
    for job in desktop:
        names = [
            m.group(1)
            for m in re.finditer(r"^      - name: (.+)$", "\n".join(_job_blocks()[job]), re.M)
        ]
        for required in (
            "Ensure tkinter for the installer GUI",
            "Build update installer",
            "Build with PyInstaller",
        ):
            assert required in names, f"{job} 缺少步骤：{required}"
        assert names.index("Build update installer") < names.index("Build with PyInstaller")


def test_installer_env_var_points_at_built_binary():
    """主程序 spec 通过 MANGAPROOF_INSTALLER 拿到安装器；Windows 必须指向 .exe。"""
    for job, lines in _job_blocks().items():
        body = "\n".join(lines)
        if "MANGAPROOF_INSTALLER" not in body:
            continue
        expected = ".exe" if job == "build-windows" else "MangaProof-update-installer"
        assert expected in body, f"{job} 的 MANGAPROOF_INSTALLER 指向疑似不对"
