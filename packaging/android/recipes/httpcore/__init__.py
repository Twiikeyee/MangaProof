# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

# MangaProof Android recipe —— httpcore（httpx 的底层传输层，纯 Python）
#
# p4a develop 没有 httpcore 的 recipe（实测 404），随项目自带。
# 名字 = 目录名（p4a Recipe.name 取模块路径的末段），须与 REQUIREMENT_TOKENS 一致。

from pythonforandroid.recipe import PyProjectRecipe


class HttpcoreRecipe(PyProjectRecipe):
    version = "1.0.9"
    url = ("https://files.pythonhosted.org/packages/06/94/"
           "82699a10bca87a5556c9c59b5963f2d039dbd239f25bc2a63907a05a14cb/"
           "httpcore-{version}.tar.gz")

    # httpcore 的同步/异步连接池依赖 certifi（TLS 信任根）与 h11（HTTP/1.1 解析）
    depends = ["python3", "certifi", "h11"]


recipe = HttpcoreRecipe()
