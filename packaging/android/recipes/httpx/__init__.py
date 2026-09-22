# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

# MangaProof Android recipe —— httpx（自更新系统的 HTTP 客户端，纯 Python）
#
# 为什么自带本地 recipe（p4a develop 其实有一个 httpx recipe）：
#   1. 官方那份的 depends = ["httpcore", "h11", "certifi", "idna", "sniffio"]
#      **漏了 anyio**，而 httpx 在运行期确实 import anyio（`httpx._client`）；
#   2. 它 depends 里的包在 p4a 里同样没有 recipe（实测全 404），
#      本来也得由本项目补齐，索性连 httpx 一起接管，避免"官方 recipe 改动"
#      在 CI 上突然换掉我们的构建图。
#   p4a 的 Recipe.get_recipe 会**优先搜索 --local-recipes 目录**（recipe.py），
#   因此本地同名目录即覆盖官方实现。
#
# 名字来源：p4a 的 Recipe.name 是**目录名**（recipe.py 的 name property：
# `modname.split(".", 2)[-1]`），所以目录名必须与 build_android.py 里
# REQUIREMENT_TOKENS 的 token 完全一致（大小写不敏感匹配）。
#
# 基类 PyProjectRecipe 的行为：先按 Android 平台标签去 PyPI 找**预编译 wheel**
# （纯 Python 包有 py3-none-any wheel，通常直接命中），找不到才用下面的 sdist
# 兜底现场构建 —— 所以 url 必须填对（取自 uv.lock 的实际 sdist 地址）。

from pythonforandroid.recipe import PyProjectRecipe


class HttpxRecipe(PyProjectRecipe):
    version = "0.28.1"
    url = ("https://files.pythonhosted.org/packages/b1/df/"
           "48c586a5fe32a0f01324ee087459e112ebb7224f646c0b5023f5e79e9956/"
           "httpx-{version}.tar.gz")

    # 依赖闭包与 uv.lock 完全一致：httpx → anyio / certifi / httpcore / idna；
    # httpcore → certifi / h11；anyio → idna / typing-extensions。
    # socksio 对应 pyproject 里的 httpx[socks]（用户可用 socks5 代理）。
    # 注意 httpx 0.28 起**不再依赖 sniffio**（旧版本才有），不要照抄老资料。
    depends = ["python3", "anyio", "certifi", "httpcore", "h11", "idna", "socksio"]


recipe = HttpxRecipe()
