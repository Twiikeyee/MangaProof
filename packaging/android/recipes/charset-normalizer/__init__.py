# MangaProof Android recipe —— charset-normalizer（reportlab 的运行时依赖，纯 Python）
#
# charset-normalizer 在 PyPI 上同时发布「mypyc 加速的平台 wheel」与
# 「py3-none-any 纯 Python wheel」；PyProjectRecipe 会优先尝试预编译 wheel
# （纯 Python 版即可满足功能），失败则用下面的 sdist 现场构建。
#
# sdist 文件名用下划线（charset_normalizer-*.tar.gz），已实测 HTTP 200。

from pythonforandroid.recipe import PyProjectRecipe


class CharsetNormalizerRecipe(PyProjectRecipe):
    version = "3.5.1"
    url = ("https://files.pythonhosted.org/packages/source/c/charset-normalizer/"
           "charset_normalizer-{version}.tar.gz")

    depends = ["python3"]
    site_packages_name = "charset_normalizer"


recipe = CharsetNormalizerRecipe()
