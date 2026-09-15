# Android 端界面适配：界面缩放 + 找回「文件 / 设置 / 帮助」

> 本文记录两项**Android 专有**适配的实现依据与取舍：
> 1. 顶部菜单栏在 Android 上消失的根因与修复（`AA_DontUseNativeMenuBar`）；
> 2. Android 专有界面缩放（`QT_SCALE_FACTOR`，默认 75%，设置页 50%–150%）。
>
> 硬约束：**桌面端（Windows / Linux / macOS）的缩放与显示逻辑不受任何影响**。

---

## 1. 为什么「文件 / 设置 / 帮助」在 Android 上看不到

Qt 并没有"降级成窗口内菜单栏"，而是**把 QMenuBar 隐藏并交给系统的 options menu**。完整链路（Qt 6.11 源码）：

| # | 事实 | 出处 |
|---|------|------|
| 1 | 平台主题提供"原生菜单栏"时，`QMenuBar` 构造阶段就被 `q->hide()`；且此时 `sizeHint()` 恒为 `(0,0)`，窗口里连空一行都不会留 | `qtbase/src/widgets/widgets/qmenubar.cpp`：`QMenuBarPrivate::init()`、`QMenuBar::minimumSizeHint()` |
| 2 | Android 平台主题**实现了** `createPlatformMenuBar()`（返回 `QAndroidPlatformMenuBar`）→ Qt 认为 Android 有原生菜单栏 | `qtbase/src/plugins/platforms/android/qandroidplatformtheme.cpp` |
| 3 | "原生菜单栏"在 Android 上 = 系统 options menu / ActionBar 溢出菜单；Qt 侧由 `QtAndroidMenu::openOptionsMenu()` → Java `activity.openOptionsMenu()` 打开 | `androidjnimenu.cpp`、`QtMenuInterface.java` |
| 4 | ActionBar 只有在 `onPrepareOptionsMenu()` 回调里才会被显示（`setActionBarVisibility(res && menu.size() > 0)`），而该回调的前提是"菜单已经被要求打开" —— 鸡生蛋问题 | `QtActivityBase.java` |
| 5 | 启动时 Qt 主动隐藏 ActionBar；若 `getActionBar() == null` 则直接放弃 | `QtActivityDelegate.java`：`initMembers()` / `setActionBarVisibility()` |
| 6 | p4a 的 Qt 模板主题让第 5 条必然成立：application 主题 `Theme.NoTitleBar(.Fullscreen)`，activity 主题 `@style/KivySupportCutout`（`windowNoTitle=true`、`windowFullscreen=true`，本项目还传了 `--display-cutout shortEdges`）→ **根本没有 ActionBar** | `bootstraps/qt/build/templates/AndroidManifest.tmpl.xml`、`strings.tmpl.xml` |

结论：窗口内没有菜单栏，系统侧也没有入口 → 用户完全看不到 文件 / 设置 / 帮助（工具栏不受影响，因为它是普通控件）。

### 修复方式与实现要点

`mangaproof/main.py::configure_android_menu_bar()`：在**任何 QMenuBar 创建之前**设置

```python
QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar)
```

- 必须在创建前：该属性在 `QMenuBarPrivate::init()` 里判定（本项目的主窗口在 `MainWindow._build_menus()` 里创建菜单栏）；
- **不要**改用"事后 `menuBar().setNativeMenuBar(False)`"：源码里 `QMenuBarPrivate::getPlatformMenu()` 会把每个顶层 `QMenu` 绑定到 `QAndroidPlatformMenu`，而 `setNativeMenuBar(false)` 只删除菜单栏、不清理子菜单的绑定，存在悬空引用隐患；
- 桌面端**不设置**该属性 → macOS 的系统菜单栏行为保持不变。

---

## 2. Android 上的 DPI 事实（缩放的依据）

| 事实 | 出处 / 实测 |
|------|-------------|
| Qt 在 Android 上：`logicalDpi = pixelDensity × 72`，`logicalBaseDpi = 72` → **设备像素比 = 屏幕密度**（densityDpi/160） | `qandroidplatformscreen.cpp`、`qhighdpiscaling.cpp` |
| 因此 **1 个 Qt 逻辑像素 == 1 个 Android dp**；QSS 里的 `px` 就是 dp，高分屏自动按物理像素渲染（不糊） | 同上 |
| `QScreen.logicalDotsPerInch()` 在 Android 上返回 **72**（不是 96）→ 用 `logicalDotsPerInch()/96` 当缩放系数会把界面缩小 25%，方向相反 | `qhighdpiscaling.cpp::effectiveLogicalDpi` + 实测 |
| 加载 MiSans 后实测（逻辑 px = dp）：完整工具栏 **1396**、窗口最小 **418×559**、菜单栏 26 / 工具栏 39 / 状态栏 23 | 本仓库探针 |

由此得到"整行工具栏不折叠成 »"的条件：`设备dp宽 ÷ 缩放s ≥ 1396 + 余量`

| 设备（横屏） | 逻辑宽度 | 整行不折叠所需缩放 |
|---|---|---|
| 折叠屏内屏（1812×2176@420 → 829×690 dp） | 829 | ≤ 0.55（不可行 → 会走 » 折叠） |
| 模拟器平板（1920×1080@280 → 1097×617 dp） | 1097 | **≤ 0.78** |
| 真 10 寸平板（2560×1600@320 → 1280×800 dp） | 1280 | ≤ 0.91 |

> 默认值取 **0.75**：它是 1097 dp 宽设备上"整行工具栏不折叠"的最大 5% 档（0.80 时 1097/0.8 ≈ 1371 < 1396，会折叠）。
>
> 顺带解释"模拟器上按钮比电脑上还大"的观感：平板上 1 dp = density 个物理像素（1.75），模拟器按 1:1 显示在显示器上时，同样的 33 dp 按钮就是 58 像素 vs 桌面 100% 缩放下的 33 像素 —— 这是**观看倍率**，不是应用被放大。真机拿在手里时元素其实比桌面更小。

---

## 3. 缩放方案：单一机制 `QT_SCALE_FACTOR`

| | 采用：`QT_SCALE_FACTOR`（启动前写入） | 未采用：应用侧改主题 QSS |
|---|---|---|
| 覆盖范围 | **全部**：QSS、代码里的固定尺寸、Qt 自身样式度量（对话框间距、消息框图标、进度条、dock 标题栏） | 只有 QSS 覆盖的部分，对话框细节会"只缩一半" |
| 代码量 | 启动时 3 行（读一次 settings.json + `setdefault`） | 主题重构 + 多处常量 + 运行时重刷 |
| 生效时机 | 启动时（改设置需重启应用） | 可即时预览 |
| 与桌面隔离 | 只在 Android 写环境变量；桌面连键都不写 | 需额外保证"系数 1.0 时逐字节还原" |

两者是同一个缩放的两种表达，**同时使用会双重缩放**（0.85² = 0.72），因此只保留一个。

**语义与副作用**（Qt 高 DPI 模型，全屏窗口）：

```
DPR = 屏幕密度 × 缩放s
逻辑可用空间 = 物理像素 ÷ DPR = dp ÷ s      ← 缩放后"桌面级"空间变大
渲染分辨率   = 逻辑尺寸 × DPR = 物理像素    ← 与 s 无关，不会发虚
```

因放大后的 backing store 始终等于物理像素，界面缩放**不影响画布（PSD）的渲染清晰度**，质检用途不受损。

**实现**（`config/settings.py` + `main.py`）：

- `resolve_ui_scale(app_dir)`：桌面直接返回 1.0（连文件都不读）；Android 读 `settings.json` 的 `ui_scale`，缺键 → 0.75，非法/越界 → 0.75；
- `apply_startup_ui_scale(app_dir)`：仅在 s ≠ 1.0 时 `os.environ.setdefault("QT_SCALE_FACTOR", ...)`（**绝不覆盖**用户/系统已有的值），并记录"实际生效值"供日志与提示使用；
- `android_ui_scaling()`：界面缩放的**唯一平台判定入口**（设置页是否显示该项、默认值、是否提示重启都走它）。

### 各档位的实际后果（便于设置页说明与验收）

| 缩放 | 字号（13 dp × s） | 按钮高（33 dp × s） | 1097×617 dp 设备 | 1280×800 dp 平板 | 829×690 dp 折叠内屏 |
|---|---|---|---|---|---|
| 50% | 6.5 dp ≈ 1.03 mm | 16.5 dp | 工具栏 ✓ | ✓ | ✓ |
| 70% | 9.1 dp ≈ 1.44 mm | 23.1 dp | ✓ | ✓ | ✗ 折叠 |
| **75%（默认）** | 9.8 dp ≈ 1.55 mm | 24.8 dp | ✓（余量 67 dp） | ✓ | ✗ 折叠 |
| 80% | 10.4 dp ≈ 1.65 mm | 26.4 dp | ✗ 折叠 | ✓ | ✗ |
| 100% | 13 dp ≈ 2.06 mm | 33 dp | ✗ | ✓ | ✗ |
| ≥110% | — | — | 垂直裁剪（617/1.1 < 最小高 559） | 1.5 时裁剪 | 1.1 时裁剪 |

---

## 4. 桌面端零影响的三重保证

1. **严格平台判定**（本轮新增 `mangaproof/utils/platform.py::is_android_strict()`）：只认编译期平台标识——

   ```cpp
   // qtbase/src/corelib/global/qoperatingsystemversion.h
   static constexpr OSType currentType() {
   #if defined(Q_OS_WIN)      return Windows;
   #elif defined(Q_OS_MACOS)  return MacOS;
   #elif defined(Q_OS_ANDROID) return Android;
   #else                      return Unknown;   // 桌面 Linux
   #endif
   }
   ```
   外加 `sys.platform == "android"`（官方 Android CPython）。**不看任何环境变量**，异常一律按非 Android 处理。

   > 为什么不能复用 `utils/shutdown.py::is_android()`：它把 `ANDROID_ROOT` 环境变量也算作证据（实测：Linux 桌面 `ANDROID_ROOT=/system` → 返回 True）。用于退出方式无妨，用于界面缩放则可能在桌面机上把界面缩到 75%。`tests/test_android_ui_scale.py` 把这一反例钉成了测试。

2. **不写、不改环境变量**：桌面分支 `resolve_ui_scale()` 恒为 1.0 → 不写 `QT_SCALE_FACTOR`；用 `setdefault`，用户自己设的值原样保留。
3. **设置层也不暴露**：「界面缩放」仅在 Android 出现在设置页；桌面端即使手工把 `ui_scale` 写进 `settings.json`，也只被 `Settings` 解析为内存值，不参与任何缩放。

---

## 5. 改动清单

| 文件 | 内容 |
|---|---|
| `mangaproof/utils/platform.py`（新增） | `is_android_strict()`（编译期平台判定）、`qt_os_type_name()`（日志用） |
| `mangaproof/config/settings.py` | 缩放常量（`DEFAULT_UI_SCALE` / `ANDROID_DEFAULT_UI_SCALE` / `UI_SCALE_MIN/MAX/STEP`）、`Settings.ui_scale`、`_from_dict` 解析、`save()` 落盘、`android_ui_scaling()` / `default_ui_scale()` / `ui_scale_choices()` / `clamp_ui_scale()` / `resolve_ui_scale()` / `apply_startup_ui_scale()` / `effective_ui_scale()` |
| `mangaproof/main.py` | QApplication **之前**应用缩放；QApplication **之后**、任何菜单栏之前调用 `configure_android_menu_bar()`；启动日志打印缩放值与平台类型 |
| `mangaproof/ui/settings_dialog.py` | 仅 Android 显示「界面缩放（重启后生效）」下拉（50%–150%，步进 5%，共 21 档）+ 说明 tooltip；`apply_to` / `_reset_defaults` 同步 |
| `mangaproof/ui/main_window.py` | 设置保存后：Android 且缩放值变化 → 弹窗提示"重启应用后完全生效" |
| `tests/test_android_ui_scale.py`（新增） | 桌面守门（含 `ANDROID_ROOT` 注入回归）、Android 路径、档位表、持久化、设置页可见性、菜单栏属性、三个菜单存在性 |

---

## 6. 验收方法

```bash
# 一台设备/模拟器即可覆盖三档几何
adb shell wm size 2560x1600 && adb shell wm density 320   # 真 10 寸平板 ≈1280x800 dp
adb shell wm size 1812x2176 && adb shell wm density 420   # 折叠内屏 ≈829x690 dp
adb shell wm size 1920x1080 && adb shell wm density 280   # 1097x617 dp（默认 75% 的基准设备）
adb shell wm size reset && adb shell wm density reset
adb exec-out screencap -p > shot.png
```

每档核对 4 件事：

1. 顶部出现 **文件 / 设置 / 帮助**，且点开有下拉菜单；
2. 顶部工具栏**整行完整**（无 "»" 折叠；折叠内屏除外，见限制）；
3. 与把设置改成 100% 重启后相比，界面明显更紧凑（对话框一起变小 → 说明覆盖到了 Qt 自身度量）；
4. 启动日志（`adb logcat` 或程序目录 `logs/mangaproof.log`）里出现
   `界面缩放 = 0.75（Qt 平台类型：Android，Android 专有判定：是）`。

桌面回归：`uv run pytest` 全绿；日志应为 `界面缩放 = 1.00（Qt 平台类型：Unknown/Windows/MacOS，Android 专有判定：否）`，界面与改动前逐像素一致。

---

## 7. 已知限制（有意为之）

- **改缩放需重启应用**：Qt 只在启动时读一次 `QT_SCALE_FACTOR`；
- **折叠内屏整行工具栏会折叠**：需要 ≤55% 才放得下（不可读），故接受 Qt 的 "»" 扩展按钮；
- **≥110% 在平板/折叠内屏/该模拟器上会垂直裁剪**：窗口最小高 559 dp 是硬下限（逻辑尺寸不随缩放变化）；
- 未做：手机档布局（抽屉/单栏）、竖屏与分屏布局改造、dock 误关闭防护与布局持久化、安全区（`safeAreaMargins`）——均另议。

---

## 8. 字体缺字：Android 上的空白方块（已修）

### 现象与根因

平板上 **✗ ▣ ✎ 🗑 ⚠** 这几个"字符图标"显示为空白，桌面端正常。根因不是字体没加载（`font/MiSans-Medium.ttf` 与 `main.pyc` 同目录、日志有"已加载应用字体：MiSans"），而是**缺字形 + Android 没有逐字回退**：

| 平台 | 缺字形时怎么办 |
|---|---|
| Windows / macOS / Linux 桌面 | 由系统字体回退补齐（DirectWrite / CoreText / fontconfig），所以看不到问题 |
| **Android** | `QAndroidPlatformFontDatabase::fallbacksForFamily()` 只追加 emoji 字体、按系统语言追加一个 CJK 字体、以及 `QT_ANDROID_FONTS` 里列出的家族名；**不会**把 `/system/fonts` 下的字体当作逐字回退【源码】`qtbase/src/plugins/platforms/android/qandroidplatformfontdatabase.cpp` → 缺字形就是空白 |

### 实测：MiSans-Medium.ttf 的真实覆盖（直接解析 cmap）

解析 `font/MiSans-Medium.ttf` 的 cmap（format 4 + 12，共 29571 个码位）后确认缺失：

| 原字符 | 码位 | 用途 | 处置 |
|---|---|---|---|
| `✗` | U+2717 | 未通过（状态图标/按钮/芯片/提示文案） | → **`✕` U+2715**（与 `✓` 同族、笔画粗细一致） |
| `▣` | U+25A3 | `▣ 自动框选` | → **`□` U+25A1** |
| `✎` | U+270E | `✎ 自定义批注` | → 去掉图标（MiSans 无任何铅笔类字形：✏✐✑✒✍ 全缺） |
| `🗑` | U+1F5D1 | `🗑 删除选中问题` | → 去掉图标（emoji 不依赖系统字体不可靠） |
| `⚠` | U+26A0 | 重要提醒 / 警告文案 | → **`▲` U+25B2** |
| `⑳` | U+2473 | 一条日志文案 | → 改写文案（`①～⑩` 才是实际覆盖上限，返修单 PDF 早已按此回退 `(11)` 写法） |

> 顺带确认：**所有中文汉字**以及 `✓ ○ ● ＋ ✕ □ · × § © → ≈ ≥ ①②…⑩ ※ ▲` 等符号 MiSans 都有字形，
> 全项目非 docstring 文案里再无其它缺字（自动化检查见下）。

### 守门测试

`tests/test_font_glyph_coverage.py`：用 reportlab 的 `TTFontFile`（与返修单 PDF 同一套字体解析）读出 MiSans 覆盖的码位，
再 AST 扫描 `mangaproof/` 下**所有非 docstring 字符串字面量**（界面文案 + 日志），断言零缺字，
并把 `✗ ▣ ✎ 🗑 ⚠` 这五个历史字符列为显式回归项。以后谁再往界面文案里塞 emoji/生僻符号，测试会直接失败并给出替代建议。

### 给后续加图标的原则

1. 优先用 MiSans 覆盖的符号（上面那组），或纯文字；**不要用 emoji**；
2. 需要真正的图标时，用 `QIcon`/`QPainter` 自己画（与字体无关，任何平台都稳），而不是找"看起来像图标"的字符；
3. 若确实要引入新符号，先跑一次 `tests/test_font_glyph_coverage.py`（或查 cmap）确认字形存在。
