"""python-for-android hook：为 MangaProof 注入「进程环境预置」用的 ContentProvider。

这个 provider（`A11yEnvProvider`）在任何 Activity 之前、同一进程内写入两个环境
变量；两件事都必须在 Qt 启动前完成，理由分别如下。

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

**`MANGAPROOF_SW_DP=<最小宽度 dp>`——界面缩放默认值按机型分档**

界面缩放要在 QApplication 之前写进 `QT_SCALE_FACTOR`（Qt 只在启动时读一次），
那一刻 PySide6 拿不到屏幕信息（没有 QJniObject，QScreen 也还不存在），所以由
Java 侧用 `DisplayMetrics` 算好 `min(宽,高) / density` 写进环境变量；Python 侧
据此把默认缩放分成"手机 0.55 / 折叠屏内屏与平板 0.75"（见
docs/Android端界面适配_缩放与菜单栏.md §2.1）。

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
after_apk_build / before_apk_assemble 注入 provider + 改入口 Activity 并**断言**成功。

第二件事：把入口 Activity 换成 `PickerActivity`
--------------------------------------------
p4a 的 Qt 模板把入口写成 `org.qtproject.qt.android.bindings.QtActivity`。我们的
`com.mangaproof.picker.PickerActivity` 继承它，只多一件事：**截获自己的
`onActivityResult`**（系统 SAF 选择器的结果只有启动它的 Activity 收得到），
其余 request code 原样 `super` 交回 Qt。

为什么必须换掉 Qt 自带的原生文件对话框：Qt 6.11.2 的
`QAndroidPlatformFileDialogHelper` 存在同线程重入死锁（`handleActivityResult`
持非递归 `QMutex` 期间同步回调 → `accept()/reject()` → `QDialog::hide()` →
`helper->hide()` → `unregisterActivityResultListener()` 再取同一把锁），
表现为**选中/取消后界面永久卡死**。详见
`docs/Android端适配设计_原生文件读写_横屏全屏_分层图标_内存策略.md`。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

# packaging/android/p4a_hook.py → parents[2] = 仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]
#: Java 源码树根（`com/…` 结构，整体镜像进 Gradle 源码集）
JAVA_SRC_ROOT = REPO_ROOT / "packaging" / "android" / "java"
JAVA_REL = Path("com/mangaproof/a11y/A11yEnvProvider.java")
JAVA_SRC = JAVA_SRC_ROOT / JAVA_REL

PROVIDER_CLASS = "com.mangaproof.a11y.A11yEnvProvider"
# 授权名必须**全局唯一**（同一设备同时装着旧包 org.MangaProof.mangaproof 与本包
# com.priloba.mangaproof 时，若授权名相同会 INSTALL_FAILED_CONFLICTING_PROVIDER），
# 所以跟随应用 ID 一起改。
PROVIDER_AUTHORITY = "com.priloba.mangaproof.a11y.env"

#: p4a Qt 模板写下的入口 Activity
QT_TEMPLATE_ACTIVITY = "org.qtproject.qt.android.bindings.QtActivity"
#: CI 实测 p4a 实际渲染出的入口类名（buildozer.spec 的 android.entrypoint 取值）
P4A_ENTRYPOINT_FALLBACK = "org.kivy.android.PythonActivity"
#: 我们的入口 Activity（继承 Qt 的 QtActivity，只为截获自己的 onActivityResult）
PICKER_ACTIVITY = "com.mangaproof.picker.PickerActivity"

_PROVIDER_XML = (
    "\n        <!-- MangaProof: 在 Activity 之前把 QT_ANDROID_DISABLE_ACCESSIBILITY=1"
    " 与 MANGAPROOF_SW_DP（最小宽度 dp，供界面缩放按机型分档）写入进程环境"
    "（见 packaging/android/p4a_hook.py） -->\n"
    f'        <provider android:name="{PROVIDER_CLASS}"\n'
    f'                  android:authorities="{PROVIDER_AUTHORITY}"\n'
    '                  android:exported="false" />\n    '
)

#: before_apk_assemble 时必须已经注入成功
_state = {
    "java_copied": False,
    "manifest_patched": False,
    "activity_swapped": False,
}


def _log(message: str) -> None:
    print(f"[mangaproof-hook] {message}", flush=True)


def _install_java(dist_dir: Path) -> None:
    """把 `packaging/android/java/**` 整棵树镜像进 Gradle 源码集。

    逐个文件拷贝（而不是整个目录 tree）便于校验"一个都不能少"：漏掉
    `PickerActivity.java` 会表现为"清单指向的类不存在"——构建期才炸，代价高。
    """
    if not JAVA_SRC_ROOT.is_dir():
        raise RuntimeError(f"[mangaproof-hook] 找不到 Java 源目录：{JAVA_SRC_ROOT}")
    sources = sorted(JAVA_SRC_ROOT.rglob("*.java"))
    if not sources:
        raise RuntimeError(f"[mangaproof-hook] {JAVA_SRC_ROOT} 下没有任何 .java")
    if not JAVA_SRC.is_file():
        raise RuntimeError(f"[mangaproof-hook] 缺少必需文件：{JAVA_SRC}")

    dest_root = dist_dir / "src" / "main" / "java"
    copied: list[str] = []
    for src in sources:
        rel = src.relative_to(JAVA_SRC_ROOT)
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        if not dest.is_file():
            raise RuntimeError(f"[mangaproof-hook] Java 源拷贝失败：{dest}")
        copied.append(rel.as_posix())
    if not _state["java_copied"]:
        _log(f"已放入 {len(copied)} 个 Java 源：{copied}")
        _state["java_copied"] = True


# 【已移除】曾在此注入 `android:extractNativeLibs="true"`（run 34950341979 后撤掉）
# ---------------------------------------------------------------------------
# 动机是对的：release 包安装后 `lib/<abi>/*.so` **不被解压**，而 p4a 的
# `libs.tmpl.xml` 要求按**绝对路径**加载 `QtCore/QtGui/QtWidgets.abi3.so`
# （它们不带 lib 前缀，走不通 `System.loadLibrary` 在 APK 内查 `lib<name>.so`
# 的约定）→ 启动即 dlopen failed；debug 包则正常。
#
# 但实测**清单属性压不过 AGP 的 `packagingOptions { jniLibs { useLegacyPackaging } }`**：
# 注入后经 aapt2 读最终 APK，属性确实是 `extractNativeLibs=true`，真机上 so 依旧
# 不解压。既然该注入不解决问题，就**不再保留**（少一处"看着有用其实没用"的改动，
# 也不再多一条会误伤流水线的校验）。
#
# 现方案：Android 构建固定用 **debug 模式**（已被真机验证可用），由 CI 用仓库
# keystore 重新签名后再分发 —— 见 `.github/workflows/android.yml` 的 mode 设置与
# `Sign APK` 步骤。若将来要回到 release，必须先解决"so 解压"（例如让 p4a/Gradle
# 的 useLegacyPackaging 真正生效），否则不要切。

def _swap_entry_activity(text: str) -> tuple[str, bool]:
    """把清单里的入口 Activity 换成 `PickerActivity`（幂等）。

    **为什么需要多级匹配**（两轮 CI 失败换来的教训）：
    p4a 的 Qt 模板里入口**不是固定类名**，渲染结果取决于构建参数：

        <activity android:name="{{args.android_entrypoint}}" …>

    - 第一次（run 34947644803）我以为模板里是字面量
      `org.qtproject.qt.android.bindings.QtActivity` → 命中 0 次 → 我那道
      "找不到就硬失败"的保护把**正确的构建**挡死了；
    - 第二次（run 34950341979）加上模板变量写法后，实测渲染出的是
      **`org.kivy.android.PythonActivity`**（p4a/buildozer 的默认 entrypoint，
      与 Qt bootstrap 无关）→ 仍然命中 0 次。

    所以现在的策略是三级：① 已知字面类名（Qt 模板名 / p4a 默认名）→
    ② 未渲染的模板变量 → ③ **按 `MAIN`/`LAUNCHER` intent-filter 定位唯一 launcher**
    （不依赖任何具体类名，p4a 或部署工具换名也不会再挡构建）。三级都失败才报错，
    并列出清单里真实的 activity 名，便于一眼定位。

    注意：这个替换是**必须生效**的——只有启动选择器的 Activity 才收得到
    `onActivityResult`，而 p4a 渲染出的入口类（如 PythonActivity）并不存在于我们的
    DEX 里（我们并未打包 p4a 的 Java 源），不换掉它连启动都起不来。
    """
    if PICKER_ACTIVITY in text:
        _log("清单入口 Activity 已是 PickerActivity，跳过")
        return text, True

    candidates = (
        # ① 渲染后的字面类名：CI 实测 p4a 用的是 buildozer.spec 里的 android.entrypoint，
        #    即 org.kivy.android.PythonActivity（Qt bootstrap 也沿用这个名字）
        f'android:name="{QT_TEMPLATE_ACTIVITY}"',
        f'android:name="{P4A_ENTRYPOINT_FALLBACK}"',
        # ② 模板变量原文（含/不含花括号内空格两种写法）
        'android:name="{{args.android_entrypoint}}"',
        'android:name="{{ args.android_entrypoint }}"',
        'android:name="{{args.android_entrypoint }}"',
        'android:name="{{ args.android_entrypoint}}"',
    )
    for marker in candidates:
        if text.count(marker) == 1:
            text = text.replace(marker, f'android:name="{PICKER_ACTIVITY}"', 1)
            if PICKER_ACTIVITY not in text:
                raise RuntimeError("[mangaproof-hook] 入口 Activity 替换后校验失败")
            _log(f"入口 Activity 已替换（匹配写法：{marker}）")
            return text, True

    # ③ 兜底：入口 Activity 是**主 launcher**（带 MAIN/LAUNCHER intent-filter 的那个），
    #    只要它唯一存在就替换——不依赖任何具体类名，p4a/部署工具换名也不会再挡构建。
    activity_tags = list(re.finditer(r"<activity\b.*?</activity>", text, flags=re.S))
    launchers = [m for m in activity_tags if "android.intent.category.LAUNCHER" in m.group(0)]
    if len(launchers) == 1:
        block = launchers[0].group(0)
        replaced = re.sub(r'android:name="[^"]*"',
                          f'android:name="{PICKER_ACTIVITY}"', block, count=1)
        text = text[:launchers[0].start()] + replaced + text[launchers[0].end():]
        if PICKER_ACTIVITY not in text:
            raise RuntimeError("[mangaproof-hook] 入口 Activity 替换后校验失败（launcher 兜底路径）")
        _log("入口 Activity 已替换（按 MAIN/LAUNCHER intent-filter 定位）")
        return text, True

    found = re.findall(r'<activity[^>]*android:name="([^"]*)"', text, flags=re.S)
    raise RuntimeError(
        "[mangaproof-hook] 无法定位入口 Activity：清单里既没有已知的入口写法，"
        f"也没有唯一的 MAIN/LAUNCHER activity（找到 {len(launchers)} 个 launcher、"
        f"{len(activity_tags)} 个 activity）。\n"
        f"        清单里现有的 activity 名：{found or '（一个都没有）'}\n"
        "        请核对 p4a 的 Qt bootstrap 模板是否改了入口写法，以及 "
        "build_android.py 里是否设置了 android.entrypoint"
    )


def _patch_manifest(dist_dir: Path, *, required: bool) -> None:
    """注入 provider 声明，并把入口 Activity 换成 PickerActivity。"""
    manifest = dist_dir / "src" / "main" / "AndroidManifest.xml"
    if not manifest.is_file():
        if required:
            raise RuntimeError(f"[mangaproof-hook] 清单不存在，无法注入 provider：{manifest}")
        _log("清单尚未生成，跳过注入（before_apk_build 阶段属正常）")
        return

    text = manifest.read_text(encoding="utf-8")
    original = text

    # 1) provider（无障碍开关 + 机型 dp）
    if PROVIDER_CLASS in text:
        _log("清单中已包含 A11yEnvProvider，跳过注入")
    else:
        if text.count("</application>") != 1:
            raise RuntimeError(
                f"[mangaproof-hook] 清单里 </application> 出现 {text.count('</application>')} 次，"
                "无法安全注入 provider"
            )
        text = text.replace("</application>", _PROVIDER_XML + "</application>", 1)
        if PROVIDER_CLASS not in text:
            raise RuntimeError("[mangaproof-hook] provider 注入后校验失败")

    # 2) 入口 Activity → PickerActivity（只为截获自己的 onActivityResult）
    text, activity_ok = _swap_entry_activity(text)

    if text != original:
        manifest.write_text(text, encoding="utf-8")
    _state["manifest_patched"] = True
    _state["activity_swapped"] = activity_ok
    _log(f"清单已就绪：provider + 入口 Activity = {PICKER_ACTIVITY}")


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
    if not _state["activity_swapped"]:
        raise RuntimeError("[mangaproof-hook] 入口 Activity 未替换成 PickerActivity，拒绝继续组装 APK")
    if not _state["java_copied"]:
        raise RuntimeError("[mangaproof-hook] Java 源未安装，拒绝继续组装 APK")
