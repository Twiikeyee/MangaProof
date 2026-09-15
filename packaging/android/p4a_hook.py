"""python-for-android hook：为 MangaProof 注入「不参与辅助功能」所需的 ContentProvider。

为什么需要这个 hook
------------------
某些系统（实测 HyperOS）的读屏/辅助功能会在应用启动、Qt 主线程正在创建窗口时
并发查询界面；Qt 的 Android 无障碍桥每次查询都要 `BlockingQueuedConnection`
回到主线程（`androidjniaccessibility.cpp` 的 `runInObjectContext()`），于是撞上
Qt 的 `AndroidDeadlockProtector` → 死锁/崩溃。

解法用 Qt 的**官方开关**（不触碰死锁保护器）：在进程环境里设置
`QT_ANDROID_DISABLE_ACCESSIBILITY=1`，Qt 的
`QtAccessibilityDelegate.onAccessibilityStateChanged()` 会直接 return，
不再创建覆盖在 Qt 布局上的无障碍 View，系统根本不来查（源码见
qtbase:src/android/jar/src/org/qtproject/qt/android/QtAccessibilityDelegate.java:94）。

而这个环境变量必须**在任何 Activity 之前**写入进程（该监听的注册与首次触发都在
QtLayout/Activity 初始化时）。Android 的生命周期保证 ContentProvider 早于所有
Activity（`ActivityThread.handleBindApplication()` 里 `installContentProviders()`
先于 Activity 创建），因此本 hook 做两件事：

1. 把 `packaging/android/java/.../A11yEnvProvider.java` 放进 dist 的 Gradle 源码集
   （`src/main/java/...`）——不用 `android.add_src`，避免与 p4a 的复制逻辑重复；
2. 在生成的 `AndroidManifest.xml` 的 `<application>` 内注入该 provider 声明。

hook 被调用的时机：p4a `toolchain.py` 在 `with current_directory(dist.dist_dir)` 里
依次调用 `before_apk_build` → 渲染清单/打包 → `after_apk_build` → `before_apk_assemble`
→ Gradle 组装。因此本模块：before_apk_build 只拷 Java（清单还没生成），
after_apk_build / before_apk_assemble 注入 provider 并**断言**成功。
"""

from __future__ import annotations

import shutil
from pathlib import Path

# packaging/android/p4a_hook.py → parents[2] = 仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]
JAVA_REL = Path("com/mangaproof/a11y/A11yEnvProvider.java")
JAVA_SRC = REPO_ROOT / "packaging" / "android" / "java" / JAVA_REL

PROVIDER_CLASS = "com.mangaproof.a11y.A11yEnvProvider"
PROVIDER_AUTHORITY = "com.mangaproof.a11y.env"

_PROVIDER_XML = (
    "\n        <!-- MangaProof: 在 Activity 之前把 QT_ANDROID_DISABLE_ACCESSIBILITY=1"
    " 写入进程环境，避免系统读屏查询触发 Qt 主线程死锁（见 packaging/android/p4a_hook.py） -->\n"
    f'        <provider android:name="{PROVIDER_CLASS}"\n'
    f'                  android:authorities="{PROVIDER_AUTHORITY}"\n'
    '                  android:exported="false" />\n    '
)

#: before_apk_assemble 时必须已经注入成功
_state = {"java_copied": False, "manifest_patched": False}


def _log(message: str) -> None:
    print(f"[mangaproof-hook] {message}", flush=True)


def _install_java(dist_dir: Path) -> None:
    """把 provider 的 Java 源放进 Gradle 源码集。"""
    if not JAVA_SRC.is_file():
        raise RuntimeError(f"[mangaproof-hook] 找不到 Java 源文件：{JAVA_SRC}")
    dest = dist_dir / "src" / "main" / "java" / JAVA_REL
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(JAVA_SRC, dest)
    if not dest.is_file():
        raise RuntimeError(f"[mangaproof-hook] Java 源拷贝失败：{dest}")
    if not _state["java_copied"]:
        _log(f"已放入 Java 源：{dest}")
        _state["java_copied"] = True


def _patch_manifest(dist_dir: Path, *, required: bool) -> None:
    """在 <application> 内注入 provider 声明。"""
    manifest = dist_dir / "src" / "main" / "AndroidManifest.xml"
    if not manifest.is_file():
        if required:
            raise RuntimeError(f"[mangaproof-hook] 清单不存在，无法注入 provider：{manifest}")
        _log("清单尚未生成，跳过注入（before_apk_build 阶段属正常）")
        return

    text = manifest.read_text(encoding="utf-8")
    if PROVIDER_CLASS in text:
        _log("清单中已包含 A11yEnvProvider，跳过")
        _state["manifest_patched"] = True
        return

    if text.count("</application>") != 1:
        raise RuntimeError(
            f"[mangaproof-hook] 清单里 </application> 出现 {text.count('</application>')} 次，"
            "无法安全注入 provider"
        )
    text = text.replace("</application>", _PROVIDER_XML + "</application>", 1)
    if PROVIDER_CLASS not in text:
        raise RuntimeError("[mangaproof-hook] provider 注入后校验失败")
    manifest.write_text(text, encoding="utf-8")
    _state["manifest_patched"] = True
    _log("已在 AndroidManifest.xml 注入 A11yEnvProvider（exported=false）")


def _apply(*, require_manifest: bool) -> None:
    dist_dir = Path.cwd()          # p4a 在 dist 目录内调用 hook
    _install_java(dist_dir)
    _patch_manifest(dist_dir, required=require_manifest)


# --- p4a hook 入口（函数名即阶段名，p4a 会 getattr 后调用） ------------------

def before_apk_build(toolchain=None) -> None:      # noqa: ARG001
    _apply(require_manifest=False)


def after_apk_build(toolchain=None) -> None:       # noqa: ARG001
    _apply(require_manifest=True)


def before_apk_assemble(toolchain=None) -> None:   # noqa: ARG001
    _apply(require_manifest=True)
    if not _state["manifest_patched"]:
        raise RuntimeError("[mangaproof-hook] 无障碍开关注入未完成，拒绝继续组装 APK")
