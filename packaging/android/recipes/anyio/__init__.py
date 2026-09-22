# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

# MangaProof Android recipe —— anyio（httpx 的异步/同步兼容层，纯 Python）
#
# p4a develop 没有 anyio 的 recipe（实测 404），随项目自带。
#
# 注意 depends 里的 typing-extensions：anyio 的 marker 是
# `typing_extensions>=4.16.0; python_full_version < "3.15"`，而 p4a 编的是 3.11，
# 因此运行期确实需要它（本项目已有该 recipe，见隔壁目录）。
# 另外 anyio 4.x 已不再依赖 sniffio（旧版才有），别照抄过时资料。
#
# 名字 = 目录名（p4a Recipe.name 取模块路径的末段），须与 REQUIREMENT_TOKENS 一致。

from pythonforandroid.recipe import PyProjectRecipe


class AnyioRecipe(PyProjectRecipe):
    version = "4.15.1"
    url = ("https://files.pythonhosted.org/packages/a9/d2/"
           "f4d173e22df740bc37b1db102b386ba719b66e95b0f0d751f556b387e6d2/"
           "anyio-{version}.tar.gz")

    depends = ["python3", "idna", "typing-extensions"]


recipe = AnyioRecipe()
