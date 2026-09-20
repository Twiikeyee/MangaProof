# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""本软件自身许可（GPL-3.0-only）的完整性守卫。

覆盖：
- 仓库根 `LICENSE` 存在，且是**本项目的**许可声明：带版权行、无 GPL 模板占位符；
- `app_license.load_license_text()` 在源码布局下能读到许可全文；
- 三个 PyInstaller spec 都把 `LICENSE` 与 `THIRD_PARTY_LICENSES.md` 收进产物
  （GPLv3 §6：分发目标码时须随附一份本许可副本与第三方许可清单）；
- 版本 / 许可 / 版权常量在 `mangaproof/__init__.py` 与 `pyproject.toml` 之间一致。

运行：QT_QPA_PLATFORM=offscreen uv run python -m pytest tests/test_app_license.py -v
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from mangaproof import __copyright__, __license__, __version__  # noqa: E402
from mangaproof.app_license import (  # noqa: E402
    BUNDLE_SUBDIR,
    LICENSE_FILE,
    THIRD_PARTY_FILE,
    find_license_file,
    load_license_text,
)

SPECS = sorted((ROOT / "packaging").glob("*.spec"))


def test_license_file_carries_project_notice():
    text = (ROOT / LICENSE_FILE).read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 29 June 2007" in text
    # 版权行必须是我们自己的，而不是 GPL 模板的占位符
    assert "Copyright (C) 2026 gunfub" in text
    for placeholder in ("<name of author>", "<year>", "<program>", "<one line to give"):
        assert placeholder not in text, f"LICENSE 里仍残留占位符：{placeholder}"


def test_load_license_text_reads_repo_license():
    path = find_license_file(LICENSE_FILE)
    assert path is not None and path.is_file()
    text = load_license_text(LICENSE_FILE)
    assert len(text) > 30_000, "许可全文过短，疑似读到了错误的文件"
    assert "GNU GENERAL PUBLIC LICENSE" in text
    assert "Version 3" in text


@pytest.mark.parametrize("spec", SPECS, ids=[p.name for p in SPECS])
def test_spec_ships_license_files(spec: Path):
    """三个平台的产物都要带上许可文本（GPLv3 §6）。"""
    text = spec.read_text(encoding="utf-8")
    assert f'ROOT / "{LICENSE_FILE}"' in text, f"{spec.name} 未把 {LICENSE_FILE} 收进产物"
    assert f'ROOT / "{THIRD_PARTY_FILE}"' in text, f"{spec.name} 未把 {THIRD_PARTY_FILE} 收进产物"
    assert f'"{BUNDLE_SUBDIR}"' in text, f"{spec.name} 的目标目录与 app_license 不一致"


def test_metadata_constants_are_consistent():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert __license__ == "GPL-3.0-only"
    assert project["license"] == __license__, "pyproject 与本包内的许可标识不一致"
    assert "gunfub" in __copyright__
    assert project["authors"][0]["name"] in __copyright__
    assert project["license-files"] == [LICENSE_FILE]
    assert __version__ == project["version"]


def test_bundle_lookup_order_prefers_meipass(monkeypatch, tmp_path):
    """打包产物布局：`<sys._MEIPASS>/licenses/LICENSE` 必须优先被找到。"""
    bundled = tmp_path / BUNDLE_SUBDIR
    bundled.mkdir()
    (bundled / LICENSE_FILE).write_text("bundled-license", encoding="utf-8")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert find_license_file(LICENSE_FILE) == bundled / LICENSE_FILE
    assert load_license_text(LICENSE_FILE) == "bundled-license"


def test_missing_license_degrades_gracefully(monkeypatch, tmp_path):
    """许可文件缺失时返回空串 / 位置提示，不抛异常。"""
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(
        "mangaproof.app_license._candidate_paths", lambda name: [tmp_path / "nope" / name]
    )
    assert find_license_file(LICENSE_FILE) is None
    assert load_license_text(LICENSE_FILE) == ""
