# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

# MangaProof Android recipe —— idna（国际化域名编解码，纯 Python）
#
# p4a develop 没有 idna 的 recipe（实测 404），随项目自带。
# 名字 = 目录名（p4a Recipe.name 取模块路径的末段），须与 REQUIREMENT_TOKENS 一致。

from pythonforandroid.recipe import PyProjectRecipe


class IdnaRecipe(PyProjectRecipe):
    version = "3.20"
    url = ("https://files.pythonhosted.org/packages/f5/08/"
           "8eea9d4b8302028f3abb2c0813953f7aec26d33b7a8960ed760e65ff29fa/"
           "idna-{version}.tar.gz")

    depends = ["python3"]


recipe = IdnaRecipe()
