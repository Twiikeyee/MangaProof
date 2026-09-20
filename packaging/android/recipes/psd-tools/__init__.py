# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

# MangaProof Android recipe —— psd-tools（核心依赖，纯 Python + 可选 Cython 加速）
#
# 与其它三个纯 Python recipe 的三点差异：
#
# 1. **PyPI 上没有 sdist**（只发布平台 wheel，实测 1.18.0 / 1.19.0 均无 sdist），
#    因此只能用 GitHub tag 归档作为源码；p4a 的 Recipe.unpack() 会自动把解压出的
#    顶层目录重命名为 recipe 名（psd-tools），与 GitHub 归档的 psd-tools-1.18.0
#    目录名不一致也没问题（源码 recipe.py:498-505）。
#
# 2. 版本号是动态的（pyproject: version = { attr = "psd_tools.version.__version__" }），
#    **不依赖 git**，所以从 tag 归档构建可以正常取到 1.18.0。
#
# 3. `setup.py` 里有 `cythonize([... "psd_tools.compression._rle" ...])` —— 会尝试
#    交叉编译 Cython 扩展。p4a 的 PyProjectRecipe 用
#    `python -m build --wheel`（默认开启 build 隔离，工具会自己装 setuptools/wheel/cython），
#    编译时的 CC/CFLAGS 由 p4a 的 recipe env 提供（NDK clang），因此正常情况可编过。
#    **兜底**：若该扩展交叉编译失败，可改成
#        extra_build_args = ["--no-isolation"]
#        hostpython_prerequisites = ["cython", "setuptools", "wheel"]
#    由 hostpython 直接提供构建依赖；再不行就回退「不要该扩展」——psd-tools 的
#    compression/__init__.py 对 `_rle` 缺失有 `except ImportError: from . import rle`
#    的纯 Python 回退（本仓库 README 的性能说明也确认了这一点），功能不受影响、
#    只是 RLE 解码变慢。

from pythonforandroid.recipe import PyProjectRecipe


class PsdToolsRecipe(PyProjectRecipe):
    version = "1.18.0"
    url = "https://github.com/psd-tools/psd-tools/archive/refs/tags/v{version}.tar.gz"

    # 运行时依赖（同时保证它们在 dist 里被构建）
    depends = ["python3", "numpy", "pillow", "attrs", "typing-extensions"]
    site_packages_name = "psd_tools"


recipe = PsdToolsRecipe()
