# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

# MangaProof Android recipe —— certifi（CA 证书包，纯 Python + 数据文件）
#
# p4a develop 没有 certifi 的 recipe（实测 404），随项目自带。
# httpx/httpcore 默认用 certifi 的 CA bundle 建立 TLS 信任链，
# 自更新要访问 MirrorChyan / R2 / GitHub，必须带上它。
#
# 名字 = 目录名（p4a Recipe.name 取模块路径的末段），须与 REQUIREMENT_TOKENS 一致。

from pythonforandroid.recipe import PyProjectRecipe


class CertifiRecipe(PyProjectRecipe):
    version = "2026.7.22"
    url = ("https://files.pythonhosted.org/packages/a3/c2/"
           "24167ea9858356b47a87a50d39908bfdb72ceeefe0041586e704e5376b3a/"
           "certifi-{version}.tar.gz")

    depends = ["python3"]


recipe = CertifiRecipe()
