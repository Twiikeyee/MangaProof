# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""第三方许可文件的完整性守卫（程序内页面 ↔ 仓库根导出文件同源）。

覆盖：
- `THIRD_PARTY_LICENSES.md` 覆盖 `third_party.py` 里的每一个组件（名称 / SPDX / 主页）；
- 总览表行数与组件数一致（新增或删除组件后文档不能落后）；
- 每个组件都有非空的版本、SPDX、版权、主页与许可正文；
- 文档与当前数据**逐字节一致**（改了组件表却忘记重新生成时直接失败）。

为什么要字节级比对：文档是给人看的合规材料，版本号写错（例如依赖升级后文档仍写旧版本）
比缺一项更难发现。版本号取自本机包元数据，Android 设备上的包版本与桌面锁文件不同，
因此该用例在 Android 上跳过。

运行：uv run python -m pytest tests/test_third_party_doc.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from build_third_party_doc import OUTPUT, render  # noqa: E402
from mangaproof.third_party import build_third_party_items  # noqa: E402
from mangaproof.utils.platform import is_android_strict  # noqa: E402

_ON_ANDROID = is_android_strict()


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert OUTPUT.exists(), f"缺少 {OUTPUT.name}，请运行 scripts/build_third_party_doc.py"
    return OUTPUT.read_text(encoding="utf-8")


def test_doc_covers_every_component(doc_text):
    for item in build_third_party_items():
        assert item.name in doc_text, f"缺少组件：{item.name}"
        assert f"**许可证（SPDX）**：{item.spdx}" in doc_text, f"缺少 SPDX：{item.name}"
        assert f"<{item.homepage}>" in doc_text, f"缺少主页：{item.name}"


def test_overview_row_count_matches_items(doc_text):
    items = build_third_party_items()
    rows = [
        line
        for line in doc_text.splitlines()
        if line.startswith("| ") and line.split("|")[1].strip().isdigit()
    ]
    assert len(rows) == len(items), f"总览表 {len(rows)} 行，组件 {len(items)} 个"


def test_every_component_has_complete_metadata():
    for item in build_third_party_items():
        assert item.name.strip(), "组件名不能为空"
        assert item.version.strip(), f"{item.name} 缺版本"
        assert item.spdx.strip(), f"{item.name} 缺 SPDX"
        assert item.copyright.strip(), f"{item.name} 缺版权声明"
        assert item.homepage.startswith("http"), f"{item.name} 主页无效"
        assert len(item.license_text.strip()) > 100, f"{item.name} 许可正文过短"


@pytest.mark.skipif(_ON_ANDROID, reason="Android 上的包版本与桌面锁文件不同")
def test_doc_is_up_to_date(doc_text):
    assert doc_text == render(build_third_party_items()), (
        "THIRD_PARTY_LICENSES.md 与 third_party.py 不一致，"
        "请运行：uv run python scripts/build_third_party_doc.py"
    )
