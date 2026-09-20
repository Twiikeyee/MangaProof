# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

# MangaProof Android recipe —— attrs（psd-tools 的运行时依赖，纯 Python）
#
# 背景：p4a（python-for-android）develop 分支没有 attrs 的官方 recipe
# （实测 recipe 目录 170 项内不存在），因此随项目自带一个本地 recipe。
#
# 基类 PyProjectRecipe 的行为（p4a recipe.py:1234+）：
#   1. 先用 pip + Android 平台标签去 PyPI 找**预编译 wheel**（纯 Python 包通常
#      有 py3-none-any wheel，因此这里大概率直接装 wheel，秒过）；
#   2. 找不到才用本 recipe 的 url 拉 sdist，跑 python -m build --wheel 现场构建。
# 所以 url 必须填对（sdist），作为兜底路径。

from pythonforandroid.recipe import PyProjectRecipe


class AttrsRecipe(PyProjectRecipe):
    version = "26.1.0"
    # PyPI 的 canonical sdist 路径（已实测 HTTP 200）
    url = "https://files.pythonhosted.org/packages/source/a/attrs/attrs-{version}.tar.gz"

    depends = ["python3"]
    site_packages_name = "attrs"


recipe = AttrsRecipe()
