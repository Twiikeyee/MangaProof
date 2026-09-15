"""p4a hook 的本地验证：Java 源安装 + 清单注入 + 入口 Activity 替换。

真机构建跑在 CI（NDK 交叉编译），但 hook 本身是纯 Python，可以、也应该在本地
把"幂等 / 硬失败 / 注入结果"全部测掉——这几处一旦出错就是 60 分钟构建后才暴露。

对应实现：`packaging/android/p4a_hook.py`。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGING_DIR = REPO_ROOT / "packaging" / "android"
HOOK_PATH = PACKAGING_DIR / "p4a_hook.py"


@pytest.fixture()
def hook(monkeypatch):
    """按文件路径加载 hook（`packaging/` 不是 Python 包，p4a 也是按路径加载它）。"""
    spec = importlib.util.spec_from_file_location("mangaproof_p4a_hook", HOOK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_state",
                        {"java_copied": False, "manifest_patched": False,
                         "activity_swapped": False})
    return module


@pytest.fixture()
def fake_dist(tmp_path):
    """构造一个只含 AndroidManifest.xml 的假 dist 目录。"""
    dist = tmp_path / "dist"
    (dist / "src" / "main").mkdir(parents=True)
    manifest = dist / "src" / "main" / "AndroidManifest.xml"
    manifest.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <application android:name="org.qtproject.qt.android.bindings.QtApplication"\n'
        '                 android:label="@string/app_name">\n'
        '        <activity android:name="org.qtproject.qt.android.bindings.QtActivity"\n'
        '                  android:exported="true">\n'
        '            <intent-filter>\n'
        '                <action android:name="android.intent.action.MAIN" />\n'
        '            </intent-filter>\n'
        '        </activity>\n'
        '    </application>\n'
        '</manifest>\n',
        encoding="utf-8",
    )
    return dist


def _run(hook, dist, monkeypatch, *, require_manifest=True):
    monkeypatch.chdir(dist)
    hook._apply(require_manifest=require_manifest)
    return dist / "src" / "main" / "AndroidManifest.xml"


# --------------------------------------------------------------------- Java 源

def test_installs_all_java_sources(hook, fake_dist, monkeypatch):
    _run(hook, fake_dist, monkeypatch)
    java_root = PACKAGING_DIR / "java"
    expected = sorted(p.relative_to(java_root).as_posix() for p in java_root.rglob("*.java"))
    assert expected, "仓库里应有 Java 源"
    installed = sorted(p.relative_to(fake_dist / "src" / "main" / "java").as_posix()
                       for p in (fake_dist / "src" / "main" / "java").rglob("*.java"))
    assert installed == expected
    # 关键类必须在（清单指向的就是它）
    assert "com/mangaproof/picker/PickerActivity.java" in installed
    assert "com/mangaproof/a11y/A11yEnvProvider.java" in installed


def test_installed_java_matches_source_bytewise(hook, fake_dist, monkeypatch):
    _run(hook, fake_dist, monkeypatch)
    src = PACKAGING_DIR / "java" / "com" / "mangaproof" / "picker" / "PickerActivity.java"
    dest = fake_dist / "src" / "main" / "java" / "com" / "mangaproof" / "picker" / "PickerActivity.java"
    assert dest.read_bytes() == src.read_bytes()


# --------------------------------------------------------------------- 清单

def test_injects_provider_and_swaps_entry_activity(hook, fake_dist, monkeypatch):
    manifest = _run(hook, fake_dist, monkeypatch)
    text = manifest.read_text(encoding="utf-8")
    # 1) provider 注入
    assert "com.mangaproof.a11y.A11yEnvProvider" in text
    assert 'android:authorities="com.priloba.mangaproof.a11y.env"' in text
    assert 'android:exported="false"' in text
    # 2) 入口 Activity 换成 PickerActivity，且**不再**直接指向 Qt 模板类
    assert 'android:name="com.mangaproof.picker.PickerActivity"' in text
    assert 'android:name="org.qtproject.qt.android.bindings.QtActivity"' not in text
    # 3) 其它属性没被动坏
    assert 'android:name="org.qtproject.qt.android.bindings.QtApplication"' in text
    assert 'android:exported="true"' in text
    assert text.count("<activity") == 1 and text.count("</activity>") == 1


def test_hook_is_idempotent(hook, fake_dist, monkeypatch):
    first = _run(hook, fake_dist, monkeypatch).read_text(encoding="utf-8")
    second = _run(hook, fake_dist, monkeypatch).read_text(encoding="utf-8")
    assert first == second, "重复执行不应改变清单（否则每次 before_apk_* 都会重复注入）"
    assert second.count("A11yEnvProvider") == first.count("A11yEnvProvider")
    assert second.count("com.mangaproof.picker.PickerActivity") == 1


def test_missing_manifest_is_hard_failure_when_required(hook, tmp_path, monkeypatch):
    (tmp_path / "dist" / "src" / "main").mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "dist")
    with pytest.raises(RuntimeError, match="清单不存在"):
        hook._apply(require_manifest=True)


def test_missing_manifest_is_tolerated_early(hook, tmp_path, monkeypatch):
    """before_apk_build 阶段清单还没生成：只拷 Java，不报错。"""
    dist = tmp_path / "dist"
    (dist / "src" / "main").mkdir(parents=True)
    monkeypatch.chdir(dist)
    hook._apply(require_manifest=False)
    assert (dist / "src" / "main" / "java" / "com" / "mangaproof" / "picker"
            / "PickerActivity.java").is_file()


def test_unexpected_template_activity_is_hard_failure(hook, fake_dist, monkeypatch):
    """p4a 模板换名时必须停下来，而不是"改了但没生效"。"""
    manifest = fake_dist / "src" / "main" / "AndroidManifest.xml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace("org.qtproject.qt.android.bindings.QtActivity",
                                     "org.qtproject.qt.android.SomeOtherActivity"),
                        encoding="utf-8")
    monkeypatch.chdir(fake_dist)
    with pytest.raises(RuntimeError, match="无法定位入口 Activity"):
        hook._apply(require_manifest=True)


# ---------------------------------------- 入口 Activity 的 Jinja 变量写法（CI run 34947644803）

def _p4a_style_manifest(entry: str) -> str:
    """p4a Qt 模板的入口写法：`android:name="{{args.android_entrypoint}}"`。

    结构按真实模板还原（含 `LAUNCHER` category 与 Qt 的 meta-data）——夹具与线上
    不一致曾让我的"按 launcher 定位"兜底在测试里假失败，故此处严格对齐。
    """
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
        '    <application android:name="org.qtproject.qt.android.bindings.QtApplication"\n'
        '                 android:label="@string/app_name"\n'
        '                 >\n'
        '                <!--\n'
        '                 android:extractNativeLibs="true" = needed for smaller apk size\n'
        '                -->\n'
        f'        <activity android:name="{entry}"\n'
        '                  android:label="@string/app_name"\n'
        '                  android:exported="true"\n'
        '                  >\n'
        '            <intent-filter>\n'
        '                <action android:name="android.intent.action.MAIN" />\n'
        '                <category android:name="android.intent.category.LAUNCHER" />\n'
        '            </intent-filter>\n'
        '            <meta-data\n'
        '                    android:name="android.app.lib_name"\n'
        '                    android:value="main"/>\n'
        '        </activity>\n'
        '    </application>\n'
        '</manifest>\n'
    )


@pytest.mark.parametrize("entry", [
    "{{args.android_entrypoint}}",
    "{{ args.android_entrypoint }}",
    "org.qtproject.qt.android.bindings.QtActivity",
    # CI run 34950341979 实测 p4a 渲染出的就是这个名字
    "org.kivy.android.PythonActivity",
])
def test_swap_handles_known_entrypoint_shapes(hook, entry):
    """入口的四种已知形态都要能替换。

    两轮 CI 失败换来的：先是只认 Qt 模板类名（命中 0 次），再是只加 Jinja 变量
    写法（渲染出来其实是 org.kivy.android.PythonActivity，仍命中 0 次）。
    """
    out, ok = hook._swap_entry_activity(_p4a_style_manifest(entry))
    assert ok is True
    assert 'android:name="com.mangaproof.picker.PickerActivity"' in out
    assert entry not in out


def test_swap_falls_back_to_launcher_intent_filter(hook):
    """未知类名（p4a/部署工具换名）也要能定位：按 MAIN/LAUNCHER 找唯一 launcher。"""
    out, ok = hook._swap_entry_activity(_p4a_style_manifest("com.example.BrandNewEntry"))
    assert ok is True
    assert 'android:name="com.mangaproof.picker.PickerActivity"' in out
    assert "com.example.BrandNewEntry" not in out


def test_swap_launcher_fallback_needs_exactly_one(hook):
    """有多个 launcher 时不能猜，必须报错（避免改错入口）。"""
    two_launchers = _p4a_style_manifest("com.example.A").replace(
        "    </application>",
        '        <activity android:name="com.example.B">\n'
        '            <intent-filter>\n'
        '                <category android:name="android.intent.category.LAUNCHER" />\n'
        '            </intent-filter>\n'
        '        </activity>\n'
        '    </application>',
    )
    with pytest.raises(RuntimeError, match="无法定位入口 Activity"):
        hook._swap_entry_activity(two_launchers)


def test_swap_is_idempotent_for_all_shapes(hook):
    for entry in ("{{args.android_entrypoint}}",
                  "org.qtproject.qt.android.bindings.QtActivity",
                  "org.kivy.android.PythonActivity",
                  "com.example.BrandNewEntry"):
        once, _ = hook._swap_entry_activity(_p4a_style_manifest(entry))
        twice, _ = hook._swap_entry_activity(once)
        assert twice == once, entry


def test_swap_failure_message_lists_actual_activity_names(hook):
    """定位失败时要能自诊断：错误信息里给出清单中真实的 activity 名。

    构造一个"既非已知入口、又没有 LAUNCHER 可兜底"的清单（去掉 category）——
    这正是前两轮 CI 里我最需要、却拿不到的那种信息。
    """
    broken = _p4a_style_manifest("com.example.Whatever").replace(
        '                <category android:name="android.intent.category.LAUNCHER" />\n', "")
    with pytest.raises(RuntimeError) as exc:
        hook._swap_entry_activity(broken)
    msg = str(exc.value)
    assert "com.example.Whatever" in msg
    assert "无法定位入口 Activity" in msg


def test_duplicate_application_tag_is_hard_failure(hook, fake_dist, monkeypatch):
    manifest = fake_dist / "src" / "main" / "AndroidManifest.xml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace("</application>", "</application>\n</application>"),
                        encoding="utf-8")
    monkeypatch.chdir(fake_dist)
    with pytest.raises(RuntimeError, match="</application>"):
        hook._apply(require_manifest=True)


# --------------------------------------------------------------------- 阶段断言

def test_before_apk_assemble_asserts_patch_done(hook, tmp_path, monkeypatch):
    """组装前必须确认注入完成（否则会打出"点了没反应"的包）。"""
    dist = tmp_path / "dist"
    (dist / "src" / "main").mkdir(parents=True)
    monkeypatch.chdir(dist)
    # 清单缺失 → _apply 直接抛错，不会静默通过
    with pytest.raises(RuntimeError):
        hook.before_apk_assemble()


def test_before_apk_assemble_passes_after_patch(hook, fake_dist, monkeypatch):
    manifest = _run(hook, fake_dist, monkeypatch)
    monkeypatch.chdir(fake_dist)
    hook.before_apk_assemble()          # 不应抛错
    assert "com.mangaproof.picker.PickerActivity" in manifest.read_text(encoding="utf-8")
