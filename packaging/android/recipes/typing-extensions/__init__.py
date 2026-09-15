# MangaProof Android recipe —— typing-extensions（psd-tools 的运行时依赖，纯 Python）
#
# 注意：typing-extensions 同时出现在 pyproject 的 dev 依赖组里，但它**确实是
# psd-tools 的运行时依赖**（见 scripts/android/analyze_lock_deps.py 的
# 「组/运行时重叠」判定），必须打包进 APK，不能因为"出现在依赖组里"而剔除。
#
# sdist 文件名用下划线（typing_extensions-*.tar.gz），已实测 HTTP 200。

from pythonforandroid.recipe import PyProjectRecipe


class TypingExtensionsRecipe(PyProjectRecipe):
    version = "4.16.0"
    url = ("https://files.pythonhosted.org/packages/source/t/typing-extensions/"
           "typing_extensions-{version}.tar.gz")

    depends = ["python3"]
    site_packages_name = "typing_extensions"


recipe = TypingExtensionsRecipe()
