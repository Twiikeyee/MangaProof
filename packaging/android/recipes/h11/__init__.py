# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

# MangaProof Android recipe —— h11（HTTP/1.1 协议状态机，纯 Python）
#
# p4a develop 没有 h11 的 recipe（实测 404），随项目自带。
# 名字 = 目录名（p4a Recipe.name 取模块路径的末段），须与 REQUIREMENT_TOKENS 一致。

from pythonforandroid.recipe import PyProjectRecipe


class H11Recipe(PyProjectRecipe):
    version = "0.16.0"
    url = ("https://files.pythonhosted.org/packages/01/ee/"
           "02a2c011bdab74c6fb3c75474d40b3052059d95df7e73351460c8588d963/"
           "h11-{version}.tar.gz")

    depends = ["python3"]


recipe = H11Recipe()
