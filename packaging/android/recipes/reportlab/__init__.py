# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

# MangaProof Android recipe —— reportlab（覆盖 p4a 内置 recipe，因为它已经不可用）
#
# 为什么必须覆盖：
#   p4a develop 内置的 reportlab recipe 指向 `hg.reportlab.com` 上 2017 年的一个
#   hg 修订（fe660f227cac）。实测该地址现在返回 **HTTP 403 Forbidden**，p4a 会
#   重试 5 次后整条构建失败（CI run 34914649124 就是这样挂的）。
#
# 本 recipe 改用 PyPI 的 sdist，并把版本对齐 `uv.lock`（5.0.1）：
#   · 5.0.1 的 sdist 是标准 setuptools 构建（build-backend = setuptools.build_meta，
#     requires = setuptools, wheel），**不含需要编译的 C 扩展**——它的加速件是另一个
#     独立包 rl_accel（可选 extra），本项目（含桌面端 lock）并未使用；
#   · `Recipe.get_recipe` 会优先搜索 --local-recipes 目录（p4a recipe.py:701-708），
#     因此同名的本地 recipe 会覆盖内置实现。
#
# 已实测：https://files.pythonhosted.org/packages/source/r/reportlab/reportlab-5.0.1.tar.gz → HTTP 200
# 依赖说明：reportlab 运行期需要 pillow（图像）与 charset-normalizer，因此写进 depends，
# 保证它们也在同一个 dist 里被构建（内置 recipe 原本还带 freetype，那是给 C 加速用的，不需要）。

from pythonforandroid.recipe import PyProjectRecipe


class ReportlabRecipe(PyProjectRecipe):
    version = "5.0.1"
    url = "https://files.pythonhosted.org/packages/source/r/reportlab/reportlab-{version}.tar.gz"

    depends = ["python3", "pillow", "charset-normalizer"]
    site_packages_name = "reportlab"


recipe = ReportlabRecipe()
