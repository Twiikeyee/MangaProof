# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

# MangaProof Android recipe —— socksio（SOCKS4/5 代理协议，纯 Python）
#
# 来源：pyproject 里声明的是 httpx[socks]（需求允许用户填 socks5 代理），
# uv.lock 因此把 socksio 记进了运行时依赖闭包，Android 侧必须有归宿，
# 否则 scripts/android/analyze_lock_deps.py --fail-on-unknown 会直接失败。
#
# 名字 = 目录名（p4a Recipe.name 取模块路径的末段），须与 REQUIREMENT_TOKENS 一致。

from pythonforandroid.recipe import PyProjectRecipe


class SocksioRecipe(PyProjectRecipe):
    version = "1.0.0"
    url = ("https://files.pythonhosted.org/packages/f8/5c/"
           "48a7d9495be3d1c651198fd99dbb6ce190e2274d0f28b9051307bdec6b85/"
           "socksio-{version}.tar.gz")

    depends = ["python3"]


recipe = SocksioRecipe()
