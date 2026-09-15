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
                        {"java_copied": False, "manifest_patched": False})
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
    # provider 必须在（清单注入指向的就是它）
    assert "com/mangaproof/a11y/A11yEnvProvider.java" in installed


def test_installed_java_matches_source_bytewise(hook, fake_dist, monkeypatch):
    _run(hook, fake_dist, monkeypatch)
    src = PACKAGING_DIR / "java" / "com" / "mangaproof" / "a11y" / "A11yEnvProvider.java"
    dest = fake_dist / "src" / "main" / "java" / "com" / "mangaproof" / "a11y" / "A11yEnvProvider.java"
    assert dest.read_bytes() == src.read_bytes()


# --------------------------------------------------------------------- 清单

def test_injects_provider_only(hook, fake_dist, monkeypatch):
    """hook 现在只注入 provider；入口 Activity 保持 p4a 渲染的原样（不再改写）。"""
    manifest = _run(hook, fake_dist, monkeypatch)
    text = manifest.read_text(encoding="utf-8")
    assert "com.mangaproof.a11y.A11yEnvProvider" in text
    assert 'android:authorities="com.priloba.mangaproof.a11y.env"' in text
    assert 'android:exported="false"' in text
    # 入口 Activity 不动（原样保留 p4a 渲染结果）
    assert 'android:name="org.qtproject.qt.android.bindings.QtActivity"' in text
    # 其它属性没被动坏
    assert 'android:name="org.qtproject.qt.android.bindings.QtApplication"' in text
    assert 'android:exported="true"' in text
    assert text.count("<activity") == 1 and text.count("</activity>") == 1


def test_hook_is_idempotent(hook, fake_dist, monkeypatch):
    first = _run(hook, fake_dist, monkeypatch).read_text(encoding="utf-8")
    second = _run(hook, fake_dist, monkeypatch).read_text(encoding="utf-8")
    assert first == second, "重复执行不应改变清单（否则每次 before_apk_* 都会重复注入）"
    assert second.count("A11yEnvProvider") == first.count("A11yEnvProvider")


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
    assert (dist / "src" / "main" / "java" / "com" / "mangaproof" / "a11y"
            / "A11yEnvProvider.java").is_file()


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
    assert "com.mangaproof.a11y.A11yEnvProvider" in manifest.read_text(encoding="utf-8")
