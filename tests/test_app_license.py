# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""本软件自身许可（GPL-3.0-only）的完整性守卫。

覆盖：
- 仓库根 `LICENSE` 存在，且是**本项目的**许可声明：带版权行、无 GPL 模板占位符；
- 程序内展示用的 `app_license.license_text()` 与 `LICENSE` **逐字一致**
  （常量由 scripts/build_app_license_text.py 生成，两边不许各改一份）；
- 三个 PyInstaller spec 仍把 `LICENSE` 与 `THIRD_PARTY_LICENSES.md` 收进产物
  （便于拿到安装包的人直接取用，GPLv3 §6）；
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
    license_text,
)

SPECS = sorted((ROOT / "packaging").glob("*.spec"))


def _repo_license() -> str:
    return (ROOT / LICENSE_FILE).read_text(encoding="utf-8")


def test_license_file_carries_project_notice():
    text = _repo_license()
    assert "GNU GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 29 June 2007" in text
    # 版权行必须是我们自己的，而不是 GPL 模板的占位符
    assert "Copyright (C) 2026 gunfub" in text
    for placeholder in ("<name of author>", "<year>", "<program>", "<one line to give"):
        assert placeholder not in text, f"LICENSE 里仍残留占位符：{placeholder}"


def test_embedded_text_matches_license_file():
    """程序内展示的全文必须与 LICENSE 同源（生成 + 守卫，不许各改一份）。"""
    embedded = license_text()
    assert len(embedded) > 30_000, "内置许可文本过短，疑似生成出错"
    assert embedded == _repo_license(), (
        "mangaproof/gpl_text.py 与 LICENSE 不一致，"
        "请运行：uv run python scripts/build_app_license_text.py"
    )


def test_embedded_text_renders_without_reading_files(monkeypatch, tmp_path):
    """内置文本不依赖磁盘：程序目录与冻结资源目录都不存在时也必须能取到全文。"""
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "no-such-bundle"), raising=False)
    monkeypatch.setattr(
        "mangaproof.config.paths.get_app_dir", lambda: tmp_path / "no-such-app-dir"
    )
    monkeypatch.chdir(tmp_path)
    text = license_text()
    assert "GNU GENERAL PUBLIC LICENSE" in text
    assert "Copyright (C) 2026 gunfub" in text


@pytest.mark.parametrize("spec", SPECS, ids=[p.name for p in SPECS])
def test_spec_ships_license_files(spec: Path):
    """三个平台的产物都要带上许可文本（便于再分发者取用，GPLv3 §6）。"""
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
