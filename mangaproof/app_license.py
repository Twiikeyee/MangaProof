# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""本项目自身许可（GPL-3.0-only）的文本来源。

第三方许可页把各组件的许可全文作为常量放在 `mangaproof/third_party.py` 里；
本软件自身的许可沿用**同一套做法**——`gpl_text.GPL3_LICENSE_TEXT` 是从仓库根
`LICENSE` 生成的常量（`scripts/build_app_license_text.py`）。好处是：

- 任何产物形态（onedir / `.app` / APK）都能离线查看，运行时不依赖
  "LICENSE 有没有被打进包、打在哪个目录"；
- `LICENSE` 仍是唯一数据源，`tests/test_app_license.py` 守卫两者逐字一致。

`BUNDLE_SUBDIR` / 两个文件名只描述三个 PyInstaller spec 把许可文本放在产物的
哪个目录（便于拿到压缩包的人直接取用），运行时不读它们。
"""

from __future__ import annotations

from mangaproof.gpl_text import GPL3_LICENSE_TEXT

#: 三个 spec 的 `_datas` 目标目录（产物内 `<bundle>/licenses/…`）
BUNDLE_SUBDIR = "licenses"
LICENSE_FILE = "LICENSE"
THIRD_PARTY_FILE = "THIRD_PARTY_LICENSES.md"

#: GPLv3 全文（与仓库根 LICENSE 逐字一致，由测试守卫）
LICENSE_TEXT = GPL3_LICENSE_TEXT


def license_text() -> str:
    """程序内展示用的许可全文。"""
    return LICENSE_TEXT
