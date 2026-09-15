# MangaProof Android 端适配设计：原生文件框架 × 强制横屏全屏 × 分层图标 × 内存策略

> **文档性质**：只读调研 + 设计方案（本轮仍只新增/修改 md 文件，未改任何代码与配置）
> **日期**：2026-09-15
> **上游文档**：`docs/Android打包研究_PySide6官方工具与GitHubActions云端构建.md`（工具链与 CI 方案）
> **本轮新增需求（来自需求方）**：
> 1. 只构建 **APK**（暂不考虑 AAB）
> 2. **文件读写适配安卓**：异步 + 调用 **安卓原生文件框架**（SAF）
> 3. 内存回收策略 **只允许"激进"** 档
> 4. **强制横屏** + **全面屏全屏**（刘海/挖孔/手势条区域全适配）
> 5. **分层图标（自适应图标）**：美术资源稍后就位，当前还没有
>
> **证据标记**：`【源码】`= Qt 6.11 / buildozer / p4a 的一手源码；`【官方文档】`= Qt 或 Android 官方文档原文；`【实测】`= 本轮实际 HTTP/文件核验；`【推断】`= 待实测的工程判断。

---

## 0. 本轮结论速览

| 需求 | 结论 | 关键依据 |
|------|------|----------|
| 只出 APK | **很容易**：buildozer 1.5.0 的 `android.release_artifact` 默认就是 `aab`（release 时），显式改成 `apk` 即可；签名走 `P4A_RELEASE_*` 环境变量或事后 `apksigner` | 【源码】buildozer `target.py:142`、`targets/android.py:925-940` |
| 文件读写走安卓原生框架 | **已定方案**：清单声明 `MANAGE_EXTERNAL_STORAGE`（"所有文件访问"，minSdk 提到 30 后为单一权限模型）→ 用**真实路径直读**（`open()`/psd-tools/Pillow 全部原样可用），不做导入兜底；**SAF 只用于选目录**（Qt 原生选择器，零 JNI），选完把 tree URI 按 §2.8 规则映射成真实路径；云盘/媒体库/受限目录直接拒绝 | 【官方文档】manage-all-files："Read and write access to all files within shared storage… **This write access includes direct file path access**"；【源码】qtbase `qandroidplatformfiledialoghelper.cpp`（选目录）、【源码】AOSP `RawDocumentsHelper`（`raw:` = 真实路径） |
| 异步 | 需要新增"存储 worker"层：目录枚举、能力探测、可选导入、导出 PDF 全部放 QThread；沿用现有 `QThread + Signal(progress)` 模式 | 【源码】本仓库 `mangaproof/ui/task_loader.py` 等四个既有 worker |
| 内存只用激进 | **已实现**：Android 上读取即强制 `aggressive`（不看文件里写什么）、非法值回落激进、启动期把值写回 `settings.json`；设置页该档**禁用但保留**（看得见当前档、改不了）。桌面端三档与默认值不变 | 【本仓库】`config/settings.py:226-282`（`ANDROID_MEMORY_POLICY` / `android_memory_policy_locked()` / `default_memory_policy()` / `effective_memory_policy()`）、`:514-544`（`reconcile_android_memory_policy()`）、`:833`（`_from_dict` 强制）、`main.py:164`、`ui/main_window.py:1331`、`ui/settings_dialog.py:397-421,585`；测试 `tests/test_android_memory_policy.py`；详见 §5 |
| 强制横屏 + 全面屏全屏 | **必须显式覆盖 buildozer 默认值**：buildozer 1.5.0 的 `default.spec` 写死 `orientation = portrait` 与 `fullscreen = 0`；刘海适配需要给 p4a 传 `--display-cutout shortEdges`（buildozer 无对应键）；运行时用 `showFullScreen()` + `QWindow.safeAreaMargins()`（Qt 6.9+，PySide6 6.11.2 已有） | 【源码】buildozer `default.spec:54,77`；p4a `bootstraps/common/build/build.py:877`、qt 模板 `strings.tmpl.xml`；Qt `qandroidplatformwindow.cpp:249-266`、`qwindow.cpp:1975-2025` |
| 分层图标 | 链路已具备：`icon.adaptive_foreground.filename` + `icon.adaptive_background.filename` → 生成 `res/mipmap-anydpi-v26/icon.xml`；美术规格为 **两层各 108×108 dp、安全区 66×66 dp**；资源未就位时先只用 `icon.filename` 兜底 | 【源码】buildozer `targets/android.py:1144-1150`、p4a `common/build/build.py:430-443`；【官方文档】Android Adaptive icons |
| 退出时闪退（真机复测新增） | **改代码即可**：Android 上退出走 `os._exit()`，跳过 CPython finalize 与 Qt/C++ 析构 —— 即 QTBUG-85449 家族的绕行做法（与 Qt 官方 `QT_ANDROID_NO_EXIT_CALL` 同向）；桌面保持 `sys.exit()` 语义。收敛在 `mangaproof/utils/shutdown.py` 一处 | 【官方文档】Qt for Android Environment Variables → `QT_ANDROID_NO_EXIT_CALL`（机制原文见 §8）；【上游 bug】QTBUG-85449「Android: crash on exit」；【本仓库】`utils/shutdown.py`、根 `main.py`（APK 入口，`input_file`）、`ui/main_window.py:2443`（关窗落盘先于退出）；详见打包文档 §5.10 ⑧ |

**总体判断**：四条需求里，**第 1、2、5 条基本是"配置 + 少量适配代码"**；**第 4 条是"配置 + 布局适配"**；**第 3 条是确定的小改动**。真正的工作量仍然集中在"SAF 读写的架构落地"（因为 Python 侧 `open()` 无法直接访问 `content://`，而本项目现有的 psd-tools/Pillow 读取链路都基于路径）与后续的触屏交互改造。

---

## 1. 打包形态：只出 APK

### 1.1 配置变更（写入 buildozer.spec，由包装脚本注入）

| 键 | 值 | 说明 |
|----|----|------|
| `android.release_artifact` | **`apk`** | release 模式默认产物是 `aab`，必须显式覆盖【源码】buildozer `target.py:142` |
| `android.debug_artifact` | `apk`（默认，无需改） | debug 模式产物【源码】同上 `:105` |
| `mode`（pysidedeploy.spec 的 `[buildozer]` 段） | `release`（推荐）或 `debug` | 见下 |

### 1.2 两条出包路径

**路径 A（推荐）：release APK + 直接签名**

```bash
# 在 CI 中、调用 pyside6-android-deploy 之前导出（buildozer 1.5.0 靠这 4 个变量判断"release 要签名"）
export P4A_RELEASE_KEYSTORE="$RUNNER_TEMP/release.jks"
export P4A_RELEASE_KEYALIAS="${{ secrets.KEY_ALIAS }}"
export P4A_RELEASE_KEYSTORE_PASSWD="${{ secrets.KEYSTORE_PASSWORD }}"
export P4A_RELEASE_KEYALIAS_PASSWD="${{ secrets.KEY_PASSWORD }}"
# 产物：<package>-<version>-arm64-v8a-release.apk，落在项目根（bin_dir = exec_directory）
```

- 依据：【源码】buildozer 1.5.0 `targets/android.py:919-940`（`P4A_RELEASE_KEYSTORE/KEYALIAS/KEYSTORE_PASSWD/KEYALIAS_PASSWD` 四个环境变量）、p4a `toolchain.py:516-535, 982-998`（把 CLI 参数映射到同名环境变量）。
- 注意：`android.release_artifact=apk` 时 p4a 走 Gradle `assembleRelease`；产物名格式见 buildozer `:1279`（`{packagename}-{version}-{arch}-{mode}.{artifact_format}`）。

**路径 B（兜底）：debug APK + 事后重签**

```bash
BT="$(ls -d "$ANDROID_HOME"/build-tools/* | sort -V | tail -1)"
"$BT/zipalign" -p -f 4 app-debug.apk aligned.apk          # 先对齐
"$BT/apksigner" sign --ks release.jks --ks-key-alias "$KEY_ALIAS" \
  --ks-pass env:KS_PASS --key-pass env:KEY_PASS --out MangaProof.apk aligned.apk
"$BT/apksigner" verify --verbose --print-certs MangaProof.apk
```

> 路径 B 在 P0 阶段更省事（不依赖 Gradle signingConfig 是否吃到了环境变量），但正式分发建议 A（release 变体 + 自定义密钥）。

### 1.3 分发方式（无 AAB）

- **侧载 / 内部渠道**：把签名 APK 挂到 GitHub Release，或直接分发文件；
- **Google Play 需要 AAB**：本轮不做，但方案里保留开关（`android.release_artifact = aab`）与 `jarsigner` 签名路径，将来一行切换；
- **16 KB page size**：与 APK/AAB 无关，Play 与 Android 15+ 设备都要求；校验与缓解见上游文档 §6-R1。

---

## 2. 文件读写适配安卓：全文件访问权限 + 真实路径直读（已定方案）

> **决策（需求方确认）**：不再走"把 `content://` 当文件用"的 SAF 读写路线。改为 **申请 MANAGE_EXTERNAL_STORAGE（"所有文件访问权限"）+ 用真实文件路径直读**（不做导入兜底）；**SAF 只用来"选目录"**（保留系统原生选择器的 UX），选完立刻把 `content://` **映射成真实路径**，之后全部 I/O 都是普通文件操作。

### 2.0 已定方案总览

| 环节 | 做法 | 依据 |
|------|------|------|
| 最低版本 / targetSdk | **minSdk 30（Android 11）** / targetSdk 35 | minSdk 30 是"单一权限模型"的前提（MANAGE_EXTERNAL_STORAGE 从 API 30 才有）；targetSdk 35 保住平板强制横屏 |
| 权限 | 清单声明 `MANAGE_EXTERNAL_STORAGE`；用户在系统设置页授权 | 【官方文档】"Read and write access to all files within shared storage… **This write access includes direct file path access**" |
| 授权 UX | Android 下**弹窗**提示需要授权（文案含设置路径）；详细引导写在 `README.md`；应用**不**做自动跳转 | 需求方决定；自动跳转需 Java 注入（PySide6 无 JNI 绑定） |
| 选目录 | `QFileDialog.getExistingDirectoryUrl()`（系统原生 Documents UI，`ACTION_OPEN_DOCUMENT_TREE`） | 【源码】qtbase `qandroidplatformfiledialoghelper.cpp:206-208` |
| 选完 | **URI → 真实路径映射**（纯 Python 字符串解析，见 §2.9），失败则提示"请从『本机存储』入口选择文件夹" | 本轮研究结论 |
| 文件 I/O | 全部用真实路径：`open()`、`psd_tools.PSDImage.open(path)`、`PIL.Image.open(path)`、`json` 读写 | MANAGE_EXTERNAL_STORAGE 允许 direct file path access |
| 设备端解释器 | **CPython 3.11.5**（由本地 `python3`/`hostpython3` recipe 钉住；Qt 官方 Android wheel 是 cp311 构建，其原生模块硬依赖 `libpython3.11.so`） | 【实测】`readelf -d` + 安装闪退复现；**应用代码必须保持 3.11 兼容**（已 grep 确认无 3.12+ 写法，但后续新增代码需注意；`pyproject` 的 `requires-python >=3.12` 只约束桌面环境） |
| 任务文件 | `.mangaproof.json` 写回打开文件/文件夹所在目录（与桌面一致） | 需求方确认；普通文件写入即可 |
| 异步 | 目录扫描 / 哈希 / 预加载 / PDF 生成仍走现有 `QThread` worker（不再需要"导入 worker"） | 复用现有架构 |
| 云端验证 | 模拟器矩阵（API 30/34 + `google_apis_ps16k` 16KB 镜像）跑安装 + 启动 + `adb shell ls /storage` 冒烟 | `ReactiveCircus/android-emulator-runner@v2` |
| 辅助功能（无障碍） | **永久不适配**（效率工具，需求方明确不做）：通过 p4a hook 注入 `A11yEnvProvider`（ContentProvider）在 Activity 之前设置 `QT_ANDROID_DISABLE_ACCESSIBILITY=1`，用 Qt 官方开关让无障碍桥不安装覆盖 View | 【源码】`QtAccessibilityDelegate.java:94`；这不是临时取舍而是**产品决策**（见 §7 决策记录第 10 条），因此不安排任何后续无障碍工作 |

**权限语义逐条（官方原文，`developer.android.com/training/data-storage/manage-all-files`）**

| 授权后可以 | 授权后仍**不可以** |
|-----------|-------------------|
| 读写**共享存储中的所有文件**（含 `/sdcard/Android/media`） | 访问**其他应用**的专属目录（`Android/data/<其它包名>/…`） |
| 访问 `MediaStore.Files` 表 | 写入 `/Android/data/`、`/sdcard/Android` 及其大部分子目录 |
| 访问 **SD 卡与 USB OTG 的根目录** | （经 SAF 打开时仍受 URI 授权范围约束——与路径直读无关） |
| **直接文件路径访问**（这正是 `open()` 能用的原因） | — |

> 合规提示：Google Play 对 `MANAGE_EXTERNAL_STORAGE` 有政策限制（仅文件管理器/备份/文档管理类可申请）。本项目只侧载 APK，不受影响；将来若上 Play 需切回 SAF 模式——因此存储层接口保持"可替换"（§2.3），旧 SAF 方案归档在 §2.2/附录 A。

### 2.1 问题的本质

| 事实 | 依据 |
|------|------|
| Android 11+（scoped storage）下，应用**不能**用普通路径访问用户共享目录（下载、Documents、SD 卡等） | 【官方文档】QStandardPaths：*"On Android 11 and above, public directories are no longer directly accessible in scoped storage mode… Instead, you can use QFileDialog which uses the Storage Access Framework (SAF) to access such directories."* |
| SAF 返回的是 `content://` URI，**不是**文件路径 | 【官方文档】Android Content URIs |
| **Python 内置 `open()` / `os.scandir()` 无法访问 `content://`** | 【推断，但属确定性结论】CPython 走 POSIX 路径系统调用，`content://` 由 ContentProvider 提供，无对应文件系统节点 |
| 本项目所有 I/O 都是路径式：`psd/loader.py`（`open(path,'rb')`、`PSDImage.open(path)`、`Image.open(path)`）、`review/persistence.py`（`Path` + `json`）、`report/generator.py`（PDF 输出路径） | 【源码】本仓库 |
| 但 **psd-tools 与 Pillow 都接受 file-like 对象** | 【源码】`psd_tools/api/psd_image.py:214-239`（`fp: IO[bytes] \| str \| bytes \| os.PathLike`）；且 psd_tools 只用到 `fp.tell()/read()/seek()`（全仓无 `mmap`/`fileno()` 依赖） |

→ 因此"适配安卓文件读写"的核心是：**把 I/O 收敛到一个存储层**，在该层里用 Qt 的 SAF 能力（Python 侧用 `QFile`/`QDir`，它们的 `content://` 支持由 Qt 平台插件提供）替代裸 `open()`。

### 2.2 Qt 的"安卓原生文件框架"通道（用于选目录；文件读写已改为路径直读）

> 本节结论仍然有效且是方案基石：**选目录**由 Qt 内建的原生 SAF 选择器完成（零 JNI）。变化只在于——选完之后不再用 `QFile/QDir` 操作 `content://`，而是**映射成真实路径**（§2.8）后用普通文件 API。

**（1）文件/目录选择 = 原生 SAF 选择器，无需 JNI、无需 pyjnius**

Qt Android 平台插件实现了原生文件对话框（qtbase 的 `6.8 / 6.9 / 6.10 / 6.11 / dev` 分支均存在该文件，实测 HTTP 200；更早版本未核证）【源码】`src/plugins/platforms/android/qandroidplatformfiledialoghelper.cpp`：

| `QFileDialog` 模式 | 使用的 Android Intent |
|--------------------|----------------------|
| `FileMode::Directory` / `DirectoryOnly`（即 `getExistingDirectoryUrl()`） | `ACTION_OPEN_DOCUMENT_TREE` |
| `FileMode::ExistingFile` / `AnyFile`（`getOpenFileUrl()`） | `ACTION_OPEN_DOCUMENT`（+ `CATEGORY_OPENABLE`、`EXTRA_MIME_TYPES`） |
| `FileMode::ExistingFiles`（`getOpenFileUrls()`） | `ACTION_OPEN_DOCUMENT` + `EXTRA_ALLOW_MULTIPLE` |
| `AcceptSave`（`getSaveFileUrl()`） | `ACTION_CREATE_DOCUMENT` |

并且**自动持久化授权**：拿到结果后调用 `ContentResolver.takePersistableUriPermission(uri, …)`【源码】同文件 `:47, 65, 76-90` → 重启应用后仍可访问该目录/文件（无需重新选择）。

**（2）`content://` 的读写与枚举 = Qt 的 content 文件引擎**

【源码】qtbase 6.11 `src/plugins/platforms/android/androidcontentfileengine.cpp`：

| 能力 | 实现情况 |
|------|----------|
| 打开读取 | ✅ `ContentResolver.openFileDescriptor(uri, "r")` → 取 `ParcelFileDescriptor.getFd()` → 交给 `QFSFileEngine`（**后续 read/seek 走裸 fd，无逐次 JNI**） |
| 打开写入/新建文件 | ✅ 模式 `"w"`；文件不存在时用 `DocumentFile.parent().createFile(mimeType, name)` 创建 |
| `size()` | ✅ |
| 删除 / 重命名 / 建目录 / 删目录 | ✅ `remove()` / `rename()` / `mkdir()` / `rmdir()`（基于 DocumentFile） |
| **目录枚举** | ✅ `beginEntryList()` → 迭代器把路径解析为 DocumentFile，`isDirectory()` 时 `listFiles()` 逐项产出 |
| 权限位 | 读权限恒置；写权限按 DocumentFile 能力返回 |

→ 也就是说：**`QFile`/`QDir` 在 Android 上可以直接吃 `content://` URI**，等价于安卓原生的 ContentResolver/DocumentFile，但不用我们写一行 Java。

**（3）PySide6 侧的可用 API（已在本机 6.11.2 核对）**

| 用途 | PySide6 API | 位置 |
|------|-------------|------|
| 选目录（SAF 树） | `QFileDialog.getExistingDirectoryUrl(parent, caption, dir, options, supportedSchemes)` → `QUrl` | `QtWidgets.pyi:1596` ✅ |
| 选文件（可多选） | `QFileDialog.getOpenFileUrl()` / `getOpenFileUrls()` | `QtWidgets.pyi:1602,1604` ✅ |
| 导出到用户选择位置 | `QFileDialog.getSaveFileUrl()` | `QtWidgets.pyi:1608` ✅ |
| URI 读写 | `QFile(QUrl/str)`、`QIODevice.open(ReadOnly/WriteOnly)`、`read/readAll/seek/pos/size/write` | `QtCore.pyi` ✅ |
| URI 枚举 | `QDir(content_uri).entryInfoList()` / `QDirListing` | `QtCore.pyi` ✅ |
| 路径判断 | `QFileInfo(url).exists()/size()/fileName()`（content:// 同样适用） | `QtCore.pyi` ✅ |
| URL 编码/解码 | `QUrl.fromLocalFile()`、`QUrl.toString()`、`QUrl.toLocalFile()`（后者对 content:// 返回空 → 用它区分"是否 SAF"） | `QtCore.pyi` ✅ |

> **注意**：`QJniObject` / `QJniEnvironment` / `QtAndroidPrivate` 在 PySide6 中**不存在**（官方 wiki 亦列为缺失绑定；本机 6.11.2 的 QtCore.pyi 与 Android wheel 的 `QtCore.abi3.so` 中均查无此符号）。所以**不要设计任何依赖 JNI 的 Python 方案**——好在 Qt 的 SAF 通道已经把需要 JNI 的部分全部封装好了。

### 2.3 目标架构：统一存储层 + 异步 worker

```
UI（Qt Widgets）
   │  只调用 storage 层的 API（耗时动作由 worker 执行，信号回调，永不阻塞）
   ▼
mangaproof/storage/service.py         ← 门面：pick_folder / scan / read_task / write_task / export_pdf
   ├── paths.py                       ← Location：桌面=PosixPath，Android=已映射的真实路径（统一成 str/Path）
   ├── android_uri.py                 ← URI→真实路径映射 + 权限检测（§2.7/§2.8，纯 Python）
   ├── picker.py                      ← 选目录：桌面 getExistingDirectory；Android getExistingDirectoryUrl + 映射
   └── workers.py                     ← ScanWorker / ExportWorker（QThread + Signal，复用现有风格）
```

> 与旧方案的差别：**没有 `QFileIO`、没有 `ImportWorker`、没有 content:// 文件引擎依赖**——Android 上拿到的就是真实路径，I/O 代码与桌面**完全同构**，唯一新增的是"选目录 + URI→路径映射 + 权限检测"这一薄层。

**关键组件说明（新方案）**

1. **`android_uri.py`**：`content_uri_to_path()`（§2.8 的规则表 + 代码骨架）+ `has_all_files_access()`（§2.7 的纯 Python 探测）+ `resolve_picked_folder(QUrl) -> Path | None`；映射失败返回 `None` 并带上"原因码"（云盘 / 媒体库 / 受限目录），由 UI 提示。
2. **`picker.py`**：桌面用 `getExistingDirectory`（返回路径）；Android 用 `getExistingDirectoryUrl`（返回 tree URI）→ 立刻映射；两者对上层都返回 `Path`。
3. **`workers.py`**：目录扫描（`os.scandir`）、SHA-256、批量预加载、PDF 生成——与桌面同一套路径 API，无需 provider 能力探测，也无需拷贝。
4. **任务文件（`.mangaproof.json`）读写**：普通文件读写，写在打开文件/文件夹所在目录（与桌面完全一致）；仅当目标位置**不可写**（如写保护 SD/OTG）时提示并可另存到应用私有目录。
5. **PDF 导出**：默认写到任务目录（普通文件）；另提供"另存为"入口（Android 可用 `getSaveFileUrl()` 走 `ACTION_CREATE_DOCUMENT`，也可继续用路径直写）。
6. **设置/最近记录/日志**：继续放应用私有目录（`get_app_dir()`；Android 上位于 `<APPROOT>/files`，可写且无需任何权限）【官方文档】QStandardPaths Android 映射表。

### 2.4 读取策略：已决策为"真实路径直读、不做导入兜底"

**决策**：Android 上拿到映射后的真实路径后**直接读**（`open()` / psd-tools / Pillow），**不实现**"拷贝到应用私有目录"的降级路径。理由（需求方已确认）：
- 与桌面端语义完全一致："读到的永远是最新的 PSD"；
- 无额外磁盘占用、无导入等待、无副本失效检测；
- 代码同构，存储层只有"选目录 + 映射"这一薄层（实现量最小）。

被否决的旧备选（**归档，仅供将来切换参考**）：

| 备选 | 为什么不用 |
|------|-----------|
| `QFileIO` 包装 `QFile(content://)` 零拷贝直读 | 在有全文件权限的前提下没有必要；且实现/调试成本高于路径直读 |
| 异步导入私有目录 | 副本会过期（作者重导 PSD 后读到旧文件），需要失效检测与增量重导入 |
| provider 能力探测 + 自动降级 | "不兜底"决策下不需要；探测本身也要在真机上反复验证 |

> 结论可靠性说明：任务文件校验只依赖 **文件大小 + 抽样 SHA-256**（`review/persistence.py` 的 `build_file_records/verify_single/verify_folder`），直读模式下校验对象就是作者的原始文件，语义最强。

### 2.5 异步实现约定（与现有代码风格一致）

- 复用现有模式：`QThread` 子类 + `Signal(int,int,str)` 进度 + `cancel()` 标志（参考 `ui/task_loader.py`、`ui/preloader.py`、`ui/report_worker.py`）；
- **禁止在 UI 线程做**：SAF 选择后的目录枚举、能力探测、导入拷贝、哈希、任务文件读写（>几 MB 时）；
- **选择器本身必须在 UI 线程**（`QFileDialog` 是模态 UI），但其结果是 `QUrl`，交给 worker 处理；
- 取消语义与现有"取消操作（Esc）"一致：worker 检查取消标志并清理半成品文件；
- 错误分层（新方案）：未授权全文件访问 / 目录无法映射（云盘、媒体库、受限目录）/ 路径不可读 / 目录内无 PSD / 任务文件位置不可写——每类给**可操作**提示（去设置授权、换"本机存储"入口、另存任务数据等，见 §2.9）。

### 2.6 具体改造点清单（实施阶段）

| 文件 | 现状 | 改造 |
|------|------|------|
| `mangaproof/config/paths.py` | `get_app_dir()` 返回程序目录 | 增加"私有数据目录"概念（Android = `<APPROOT>/files`），`settings/recent/logs` 与"用户文档"分离 |
| `mangaproof/ui/main_window.py:692,699` | `getOpenFileName` / `getExistingDirectory` | Android 分支改用 `getOpenFileUrl(s)` / `getExistingDirectoryUrl` → 立即映射为真实路径（失败则按 §2.9 提示） |
| `mangaproof/psd/loader.py` | `open(path,'rb')`、`PSDImage.open(path)`、`Image.open(path)`、`file_sha256(path)` | **基本不动**（Android 上同样是真实路径）；只把入参统一为 `pathlib.Path` |
| `mangaproof/review/persistence.py` | `Path` 拼 `.mangaproof.json`、`json.load/dump` | 基本不动；补"目标不可写 → 提示并可另存私有目录" |
| `mangaproof/report/generator.py` | PDF 直接写路径 | 基本不动；另加"另存为"入口（可选 SAF） |
| `mangaproof/config/settings.py` / `recent.py` | 写程序目录 | 只改根目录取值（私有数据目录）；`recent` 在 Android 上存**映射后的路径**（可选同时存原始 URI 便于排障） |
| `mangaproof/ui/preloader.py` / `task_loader.py` | 后台预加载/校验（已异步 ✅） | 不改（数据来源仍是本地真实路径） |
| 新增 `mangaproof/storage/**` | — | `android_uri.py`（映射+权限检测）、`picker.py`（选目录）、`service.py`（门面）、`workers.py`（扫描/导出） |

### 2.7 权限获取与检测（不写引导页，引导进 README）

- **清单**：`android.permissions` 增加 `android.permission.MANAGE_EXTERNAL_STORAGE`（APK 侧由包装脚本写入 buildozer.spec）；
- **用户授权路径（写进 README + 弹窗文案）**：`设置 → 应用 → MangaProof → 特殊应用权限/所有文件访问 → 允许管理所有文件`；Android 11+ 各家 ROM 的入口略有差异（部分机型在"特殊应用权限"下，部分在应用详情页顶部）；
- **纯 Python 检测**（无 JNI）：
  ```python
  def has_all_files_access() -> bool:
      try:
          os.listdir("/storage/emulated/0")   # 未授权时会抛 PermissionError
          return True
      except (PermissionError, OSError):
          return False
  ```
  可再加一条真实读写探针（在自己的应用专属目录之外创建/删除一个临时文件）以排除"能列目录但不能写"的个别 ROM 差异；
- **弹窗策略**：启动时或首次选目录前检测 → 未授权则弹窗（"需要『所有文件访问』权限才能直接打开漫画目录"。两个按钮：`去设置`（仅文字提示路径）/`退出`）→ 用户回到应用后**再检测一次**（在 `applicationStateChanged` 回到 `ApplicationActive` 时）；
- **不做**自动跳转设置页：需要 Java 注入或 pyjnius（PySide6 无 `QJniObject`），需求方已决定不做；
- adb 侧测试用（CI/调试）：`adb shell appops set --uid <pkg> MANAGE_EXTERNAL_STORAGE allow`（官方文档给出的测试手段）。

### 2.8 URI → 真实路径 映射规则（本轮核心研究）

**输入**：Qt 原生选择器返回的 `QUrl`（`ACTION_OPEN_DOCUMENT_TREE` 的结果）。Qt 的处理逻辑是取 `intent.getData()` 原样返回【源码】`qandroidplatformfiledialoghelper.cpp:34-52`，所以拿到的就是 Android 的 **tree URI**，形如：

```
content://com.android.externalstorage.documents/tree/primary%3AManga%2Fch01
                                     └── authority ──┘      └── docId（URL 编码）──┘
```

**映射算法（纯 Python：`QUrl` → 字符串 → `urllib.parse.unquote` → 拼路径 → `os.path.isdir` 复核）**

| authority / docId 形态 | 含义 | 映射结果 | 证据等级 |
|------------------------|------|----------|----------|
| `com.android.externalstorage.documents` + `primary:<rel>` | 主共享存储（内置存储） | `/storage/emulated/0/<rel>`（建议启动时用 `os.path.realpath("/sdcard")` 或 `StorageManager` 语义核对一次） | 高（AOSP 该 provider 的 docId 为 `<volumeId>:<path>`，主卷 volumeId 字面量 `primary`；**待真机核验**，见 §2.10） |
| 同上 + `<UUID>:<rel>`（如 `1A2B-3C4D:Manga`） | 可移除卷（SD 卡 / USB OTG） | `/storage/<UUID>/<rel>`；UUID 可用 `os.listdir("/storage")`（过滤 `emulated`/`self`）列出并校验 | 同上（官方文档明确授权范围含 SD 卡与 OTG 根目录） |
| `com.android.providers.downloads.documents` + `downloads`（根） | 系统"下载"目录根 | `/storage/emulated/0/Download`（AOSP：该 root 指向 `Environment.DIRECTORY_DOWNLOADS`） | 中高（AOSP `DownloadStorageProvider` 源码） |
| 同上 + `raw:<绝对路径>` | 下载目录下的真实文件/子目录 | 直接取 `<绝对路径>`（`raw:` 前缀即"真实路径"） | **高（AOSP 源码）**：`RawDocumentsHelper.RAW_PREFIX="raw:"`、`getDocIdForFile() = "raw:" + file.getAbsolutePath()`、`getFileForDocId()` 去掉前缀 |
| 同上 + `msf:<id>` / `com.android.providers.media.documents` + `image:|video:|audio:|document:<id>` | MediaStore 记录 ID（媒体库/部分"下载"入口） | ❌ **拒绝**：需 MediaStore 查询（要 JNI），且 PSD 不是媒体文件 | AOSP `MediaDocumentsProvider` 的 `TYPE_IMAGE/TYPE_VIDEO/TYPE_AUDIO/TYPE_DOCUMENT` 常量 |
| 云盘/网络 provider（`com.google.android.apps.docs.storage`、OneDrive、SMB…） | 无本地路径 | ❌ **拒绝**："请从『本机存储 / Internal storage』入口选择文件夹" | 设计决策 |
| `com.android.documentsui.recents`（最近）/ 各类虚拟 root | 无本地路径 | ❌ 拒绝 | 设计决策 |
| `/storage/emulated/0/Android/data/<其它包名>/…`、`/Android/obb/…` | 其他应用的专属目录 | ❌ 即使有 MANAGE_EXTERNAL_STORAGE 也**不可访问** | 【官方文档】"Apps … still can't access the app-specific directories that belong to other apps" |
| `/storage/emulated/0/Android/data/<本应用包名>/files/…` | 本应用专属外部目录 | ✅ 可用（无需任何存储权限） | Android 存储模型 |
| 内部私有目录（`get_app_dir()` 所在，`/data/user/0/<pkg>/…`） | 应用私有 | ✅ 普通路径，无需映射 | 【官方文档】QStandardPaths Android 映射 |

**映射代码骨架**

```python
from urllib.parse import unquote, urlparse
import os

EXT_AUTH   = "com.android.externalstorage.documents"
DL_AUTH    = "com.android.providers.downloads.documents"
STOP_AUTHS = ("com.android.providers.media.documents",)          # 媒体库：拒绝

def content_uri_to_path(uri: str) -> str | None:
    """把 SAF tree/document URI 映射为真实路径；无法映射返回 None。"""
    u = urlparse(uri)
    if u.scheme != "content":
        return None
    segs = [s for s in u.path.split("/") if s]
    if len(segs) < 2 or segs[0] not in ("tree", "document"):
        return None
    doc_id = unquote(segs[1])                      # "primary:Manga/ch01" / "raw:/storage/..."
    auth = u.netloc

    if auth == EXT_AUTH:
        vol, _, rel = doc_id.partition(":")
        base = "/storage/emulated/0" if vol == "primary" else f"/storage/{vol}"
        path = os.path.normpath(os.path.join(base, rel))
        return path if path.startswith(base) else None      # 防 ../ 越权

    if auth == DL_AUTH:
        if doc_id == "downloads":
            return "/storage/emulated/0/Download"
        if doc_id.startswith("raw:"):
            return doc_id[4:]
        return None                                          # msf: 等 → 拒绝

    if auth in STOP_AUTHS:
        return None
    return None                                              # 云盘/虚拟 root → 拒绝
```

**复核（映射后必做）**：`os.path.isdir(p)` → 再 `os.access(p, os.R_OK)` → 再尝试 `os.scandir(p)` 抽样一次；任何一步失败都归为"无法访问"，提示用户改从"本机存储"入口选择（并把原始 URI 记进日志，便于排障）。

### 2.9 拒绝与失败处理（"不兜底"的具体语义）

| 场景 | 行为 |
|------|------|
| 无 MANAGE_EXTERNAL_STORAGE | 弹窗提示去系统设置授权；未授权时**功能不可用**（阻断式提示，不提供降级路径） |
| 选了云盘/虚拟 root/媒体库入口 | 提示"请从『本机存储』入口选择文件夹"，不加载 |
| 选了其他应用专属目录（`Android/data/<其它包>`） | 同上（系统层面不可访问） |
| 映射出的路径不可读/不存在 | 提示 + 记录日志（含原始 URI 与映射结果） |
| 目录内无 PSD | 走现有"未找到可监制文件"提示 |
| 任务文件写入失败（只读介质，如部分 OTG/SD 写保护） | 提示"该位置不可写"，可另存到应用私有目录（唯一保留的降级，属**写入**侧，不影响"读取不兜底") |

### 2.10 验证方法（本地无 Android 环境，全部放 CI/真机）

| 验证项 | 手段 |
|--------|------|
| **URI 形态与映射正确性（最高优先）** | P0 在应用里把"原始 URI → 映射路径 → `isdir` 结果"写进日志；云端模拟器（`android-emulator-runner`）`adb logcat` 抓取；分别在：内置存储、`Download`、SD 卡模拟卷、云盘 provider 上各试一次 |
| 卷 UUID 映射 | `adb shell ls /storage`（应见 `emulated`、`self`、`XXXX-XXXX`）与应用日志对照 |
| 权限检测 | 未授权启动 → 应弹窗；`adb shell appops set --uid <pkg> MANAGE_EXTERNAL_STORAGE allow` 后重启 → 应直接可用 |
| 直读性能 | 真机上打开一话（多页大 PSD），与桌面端对照预加载耗时；观察 `adb shell dumpsys meminfo <pkg>` |
| 任务文件写回 | 标注若干问题 → 原目录 `.mangaproof.json` 更新时间/内容正确 |
| 横屏全屏 + 刘海 | 见 §3.3 |
| 图标 | 见 §4.6 |

### 2.11 ⚠️【已废弃】自建 SAF 选择器（自建 Java 选择器 + 共享文件协议）

> **已被 §2.13 取代**：该方案真机表现**不稳定**（依赖 Java 侧守护线程 + 共享文件协议 +
> Activity 结果接管，环节多、失败面大），已整体移除。以下内容仅作为决策留痕，**勿再引入**。

> **状态**：已实现并落地在仓库；本地用 android.jar + Qt 的 jar 通过 `javac` 编译验证、
> 协议两端有单元测试；真机行为待 CI 出包后复测。

#### （1）为什么不能用 `QFileDialog`（本方案的全部动因）

Qt 6.11.2 的 `QAndroidPlatformFileDialogHelper` 存在**同线程重入死锁**，触发条件是
"选择器返回"这一瞬间，且**选中、取消都会触发**（现象：选文件、选文件夹、取消，界面
全部永久卡死）：

| # | 位置（qtbase v6.11.2） | 关键内容 |
|---|------------------------|----------|
| 1 | `src/corelib/kernel/qjnihelpers.cpp:94,113-121` | `ActivityResultListeners` 用的是**非递归** `QMutex`；`handleActivityResult()` **持锁**逐个调用监听者（`listeners.at(i)->handleActivityResult(...)`）；调用者是 Android 主线程（`QtActivityBase.onActivityResult` → `QtNative.onActivityResult` → `androidjnimain.cpp:739`） |
| 2 | `src/widgets/dialogs/qdialog.cpp:93-94` | `connect(m_platformHelper, SIGNAL(accept()), dialog, SLOT(accept()))` —— 同线程**直连**，同步执行 |
| 3 | `src/plugins/platforms/android/qandroidplatformfiledialoghelper.cpp:33,229,235-239` | `handleActivityResult()` 里 `Q_EMIT accept()/reject()`（`resultCode != RESULT_OK` 走 reject）；`hide()` 里调用 `unregisterActivityResultListener(this)`，而它同样要 `QMutexLocker locker(&...->mutex)` |
| 4 | `qdialog.cpp:121-153,175-187,768-772` + `qwidget.cpp`（`QWidgetPrivate::close` → `hide_helper` → 虚函数 `QDialog::setVisible(false)`） | `accept()/reject()` → `QDialog::done()` → `QDialogPrivate::close()` → 隐藏窗口 → `setNativeDialogVisible(false)` → **`helper->hide()`** |

合起来就是：**① 持锁 → ② 同步回调 → ③ 回到 `hide()` → ④ 再取同一把非递归锁** → 主线程自锁死。
`hide()` 里那句 `unregister` 正是上游 2020 年为修 QTBUG-78912（"Android 原生文件对话框崩溃"）
加的（commit `6839d297`），此后该文件再无相关改动；6.11 全系（含我们锁定的 6.11.2）都在。

**旁证**（非本仓库独有）：[Qt 论坛「Android QFileDialog returns nothing and code in background keeps running」](https://forum.qt.io/topic/109548/android-qfiledialog-returns-nothing-and-code-in-background-keeps-running)（"对话框还开着、代码却继续跑；选完/关掉就崩"）、[Qt 论坛「QFileDialog::getOpenFileContent on Android」](https://forum.qt.io/topic/164852/qfiledialog-getopenfilecontent-on-android/7)（Qt 6.10–6.11.1 + Android 15/16，回调永不触发）、[QTBUG-83372 / 论坛「getOpenFileName 恒返回空串」](https://forum.qt.io/topic/113335/qfiledialog-getopenfilename-always-returns-empty-string-on-android/10)。

#### （2）为什么走"共享文件协议"而不是 JNI

PySide6 的 Android wheel **不向 Python 暴露任何 JNI 绑定**（实测
`pyside6-6.11.2-…-android_aarch64.whl`：`QtCore.abi3.so` / `libpyside6.abi3.so` 里
`QJniObject`、`QJniEnvironment`、`QtAndroidPrivate`、`QCoreApplication.getJniType`
命中数**全为 0**；wheel 中出现的 `QJniObject` 符号来自 Qt 自己的 C++ 库
`libQt6Core_arm64-v8a.so`），也没有 CPython 的 `java`/`_jni` 模块。
→ **Python 既不能 new Java 对象，也不能注册 Java 回调，甚至不能调用 Java 静态方法。**

因此"打开选择器"这件事只能由 Java 侧主动监听一个共享文件来触发；同理，结果也只能写回文件。

#### （3）实现（与上文 §2.3 目标架构的对应关系）

```
packaging/android/java/com/mangaproof/picker/PickerActivity.java   ← 继承 QtActivity，自建 SAF 选择器
packaging/android/java/com/mangaproof/a11y/A11yEnvProvider.java    ← 启动时拉起命令消费线程 + 发布目录
packaging/android/p4a_hook.py                                      ← 装 Java 源 + 清单入口改为 PickerActivity
mangaproof/storage/picker.py                                       ← 门面：桌面 QFileDialog（零改动）/ Android 分流
mangaproof/storage/android_picker.py                               ← 协议实现（原子写 + QTimer 轮询 + 超时兜底）
mangaproof/ui/main_window.py                                       ← 两处调用点改走门面
```

**协议**（目录 `files/picker/`，两边都"先写 `.tmp` 再 `rename`"保证原子）：

| 方向 | 文件 | 内容 |
|------|------|------|
| Python → Java | `cmd.txt` | `FOLDER <token>` / `FILE <token>` |
| Java → Python | `result.txt` | `OK\t<真实路径>\t<uri>` / `CANCEL` / `ERROR\t<原因>` |

**关键设计点**

1. **入口 Activity 换成 `PickerActivity`**：p4a 的 Qt 模板把入口写成
   `org.qtproject.qt.android.bindings.QtActivity`，hook 改为我们的子类。只有启动
   选择器的 Activity 才收得到结果，而父类 `onActivityResult` 会把**所有** request code
   转给 Qt（未知 code 被丢弃），所以子类先截获自己的 code（`0x4D50`），其余一律 `super`
   交回 Qt —— Qt 自身的权限/对话框流程行为不变。
2. **主线程绝不阻塞**：Python 侧只挂 `QTimer` 轮询结果文件，**没有嵌套事件循环**；
   超时（默认 5 分钟）即返回并清掉命令，所以即使 Java 侧完全没响应、Activity 被系统
   重建、选择器被强杀，界面也只会恢复原状 + 提示，**不可能卡死**。Java 侧在
   `onDestroy()` 里还会兜底写一条 `ERROR`，避免"进程还活着但结果永远不来"的干等。
3. **SAF URI → 真实路径**：`ExternalStorageProvider` 的 documentId（`primary:Download/x`、
   `XXXX-XXXX:dir`）解码即可得真实路径；`DownloadStorageProvider` 的 `raw:/…` 直接可用。
   云盘/媒体库**没有**真实路径 → 明确报错让用户改选"本机存储"（延续 §2.4"不做导入兜底"）。
4. **取当前 Activity 用反射**：本版本 Qt 的 `QtNative` **所有方法都是包级可见**
   （`javap` 实测 `activity()`/`getContext()`/`runAction()` 均无 public），跨包直调会被
   javac 拒绝（本地编译实测），因此读其私有静态字段 `m_activity`（`WeakReference<Activity>`），
   取不到就回一条可读错误而不是崩溃。
5. **消费线程用轮询而非 `FileObserver`**：只读应用私有目录里的一个小文件、250 ms 一次，
   空闲开销可忽略；换来"行为可预测、不引入额外的系统回调语义"。

#### （4）与 §2.2 旧设计的差异（重要）

| 项 | §2.2 原计划 | 实际落地 |
|----|-------------|----------|
| 选目录 | `QFileDialog.getExistingDirectoryUrl()`（Qt 原生 SAF 通道） | ❌ 不可用（会死锁）；改为**自建 Java 选择器** + 文件协议 |
| Python↔Java | "Qt 已把需要 JNI 的部分封装好" | ❌ 实测 PySide6 无任何 JNI 绑定；改为**共享文件协议** |
| URI→路径 | Python 侧实现（`android_uri.py`） | Java 侧 `resolveRealPath()` 完成（能用 `DocumentsContract` 拿 documentId） |
| 任务文件/PDF 读写 | 真实路径直读 | 不变（本轮未改动） |
| 权限引导（§2.7） | 弹窗引导进系统设置 | **仍未实现**（本轮聚焦"卡死"；未授权时读共享目录会失败，需后续补） |

#### （5）本地可验证 / 需真机验证

| 项 | 手段 | 状态 |
|----|------|------|
| Java 编译 | `javac -cp android.jar:Qt6Android.jar:Qt6AndroidBindings.jar`（本地 JDK21 + SDK34 + wheel 里的 Qt jar） | ✅ 通过（并借此发现 `QtNative.activity()` 不可跨包调用） |
| 协议两端契约 | `tests/test_android_picker.py`（模拟 Java 侧写结果：成功/取消/失败/脏数据/超时/清理/并发拒绝） | ✅ 24 项 |
| hook 注入 | `tests/test_android_packaging_hook.py`（Java 源镜像、provider 注入、入口替换、幂等、缺清单/模板改名时硬失败） | ✅ 10 项 |
| 桌面零改动 | 门面分流单测 + 全量回归（`QFileDialog` 分支逐项对照） | ✅ 201 项全绿 |
| 真机：选择器可用、路径正确、不卡死 | `adb logcat -s MangaProofPicker`（含 `SAF authority=… documentId=…` 与最终 `realPath`） | ⏳ 待 CI 出包后复测 |

### 2.12 ⚠️【已废弃】清单注入 `extractNativeLibs="true"`

> **已撤掉**：注入后经 aapt2 读最终 APK 确认属性生效（=true），但真机 so **依旧不解压** ——
> 该属性压不过 AGP 的 `packagingOptions { jniLibs { useLegacyPackaging } }`。
> Android 构建改为固定 **debug** 模式（真机验证 so 会解压），见 §2.13。以下为决策留痕。

> **现象（真机实测）**：APK 内 `lib/<abi>/` 的 so **是全的**，但**安装后没有解压出来**；
> 同一份代码的 **debug 构建正常**。失败表现是启动时 `dlopen failed` / `import PySide6.*` 失败。

#### （1）机制

`android:extractNativeLibs` 决定**安装时是否把 APK 内 `lib/<abi>/*.so` 解压到**
`/data/app/<pkg>/lib/<abi>/`：

| 取值 | 安装后形态 | 对加载方式的影响 |
|------|-----------|------------------|
| `true` | 解压落盘，`ApplicationInfo.nativeLibraryDir` 真实存在 | `System.load("绝对路径")` 与 `System.loadLibrary("名字")` 都可用 |
| `false`（AGP 现代默认） | **不解压**，so 以「未压缩 + 页对齐」留在 APK 内 | 只能靠 `System.loadLibrary`/链接器命名空间从 APK 内映射 |

而 **p4a 的 Qt 清单模板把这一行注释掉了**（`AndroidManifest.tmpl.xml`）：

```xml
<!--
 android:extractNativeLibs="true" = needed for smaller apk size
 android:requestLegacyExternalStorage="true"
 android:allowNativeHeapPointerTagging="false"
-->
```

于是取值落到 AGP 默认，**debug 与 release 的实际行为不一致** —— 这就是"debug 包没问题、
release 包装完 so 没解压"的来源。

#### （2）为什么"不解压"对我们必然致命

p4a 渲染的 `libs.tmpl.xml` 里，`load_local_libs` 同时列出了两种命名：

```xml
<item>{{arch}};libshiboken6.abi3.so</item>
<item>{{arch}};libpyside6.abi3.so</item>
<item>{{arch}};Qt{{qt_lib}}.abi3.so</item>   <!-- 没有 lib 前缀 -->
```

`bundle_local_qt_libs=1` 时 QtLoader 倾向用 `m_extractedNativeLibsDir`（= `nativeLibraryDir`）
拼**绝对路径** `System.load(...)`；一旦 so 没解压，该目录为空/不存在。而
`QtCore.abi3.so` / `QtGui.abi3.so` / `QtWidgets.abi3.so` 这类**不带 `lib` 前缀**的名字，
又天然不满足 `System.loadLibrary` 在 APK 内查找 `lib<name>.so` 的约定 → 两头都够不着。

#### （3）修复与验证

| 项 | 内容 |
|----|------|
| 修复 | `packaging/android/p4a_hook.py` 新增 `_patch_extract_native_libs()`：在 `<application>` 开始标签内注入 `android:extractNativeLibs="true"`；`before_apk_assemble` 阶段断言成功，否则**拒绝组装**（避免打出"装上也起不来"的包） |
| 一个已踩的坑 | 判定"是否已注入"若用朴素的 `in text`，会被**模板注释里那句** `android:extractNativeLibs="true"` 骗过去 → 根本没注入（本地实测）。因此判定前先剥掉 `<!-- … -->`，只认真正的属性 |
| 冲突值处理 | 若清单里已有该属性但不是 `true`（例如 `false`）→ **硬失败**而不是叠加第二个同名属性（aapt2 会因重复属性报错） |
| Gradle 侧 | **无需改**：p4a 的 `build.tmpl.gradle` 已对 debug/release 统一设置 `packagingOptions { jniLibs { useLegacyPackaging = true } }`（所以差异不在 Gradle，而在清单属性）；自 AGP 7 起显式清单属性是最终裁决者 |
| 代价（已接受） | APK 体积略增 + 安装后多占一份磁盘；换来"确定能加载"。与 Android 15+/Play 的「未压缩 + 16 KB 对齐」现代形态方向相反，但本项目只侧载、不上架，且 CI 里的 16 KB 校验仍为告警级 |

**CI 新增产物校验**（`.github/workflows/android.yml` → `Verify APK native libs and manifest flag`）：
APK 产出后按 ABI ①校验必需 so 清单（`QtCore/QtGui/QtWidgets.abi3.so`、`libpyside6/libshiboken6.abi3.so`、
`libpython3.11.so`、`libc++_shared.so`、每个模块的 `libQt6Xxx_<abi>.so`、Qt 平台插件），
②用 `aapt2 dump xmltree` 读**最终**清单确认 `extractNativeLibs=true`（`(type 0x12)0xffffffff`）。
此前流水线只断言"APK 文件存在"，这两类问题（so 缺失 / 不解压）本可一路全绿到真机才暴露。

> 说明：`libpybundle.so` 与 openssl 系列库名随 p4a 版本变化且本应用不直接依赖，校验里只做**提示**、不阻断，避免校验本身误报。

---

### 2.13 【现行方案】Android 端用 Qt 控件版文件对话框（`DontUseNativeDialog`）

> 取代 §2.11（自建 Java 选择器，真机不稳定）与 §2.12（`extractNativeLibs` 注入，无效）。

#### （1）做法

`mangaproof/storage/picker.py` 是唯一入口，两端**同一个 API**：

| 平台 | 调用 | 差异 |
|------|------|------|
| 桌面 | `QFileDialog.getOpenFileName(parent, "打开单个 PSD", "", "PSD/PSB 文件 (*.psd *.psb)")` / `getExistingDirectory(parent, "打开漫画文件夹", "")` | **零改动**：不传任何 option，参数与改造前逐字一致 |
| Android | 同上，但 `options=DontUseNativeDialog`（目录选择再加 `ShowDirsOnly`），起始目录取 `/storage/emulated/0/Download` → `Documents` → `emulated/0` 中第一个存在的 | 只多这一个选项 |

#### （2）为什么 Android 必须显式关掉原生对话框

- Qt 在 Android 上默认使用**平台原生**文件对话框
  （`qandroidplatformtheme.cpp`：`usePlatformNativeDialog(FileDialog)` 返回 `true`），
  而 6.11.2 的 `QAndroidPlatformFileDialogHelper` 有**同线程重入死锁**（§2.11 的四段源码证据）；
- 传 `DontUseNativeDialog` 后，`QFileDialogPrivate::canBeNativeDialog()` 直接返回 `false`
  → `nativeDialogInUse = false` → **不创建平台 helper、不注册 ActivityResultListener、
  不碰那把非递归 `QMutex`**，就是一个纯 Qt 控件版对话框。

因此这条路径上：**没有 JNI、没有 Java 侧线程、没有共享文件协议、没有 Activity 结果接管**，
只有 Python + Qt Widgets —— 这正是"不稳定"的根因被整体移除的地方。

#### （3）代价（已知并接受）

| 项 | 说明 |
|----|------|
| 需要全文件访问权限 | 控件版对话框浏览**真实路径**，访问 `/storage/emulated/0/...` 需 `MANAGE_EXTERNAL_STORAGE` 已授予（清单已声明，见 `scripts/android/build_android.py`）；未授予时用户只能看到应用私有目录 |
| 系统不可读区域 | Android 11+ 的 `Android/data`、`Android/obb` 与 `/data` 其余部分不可读，属系统限制 |
| 观感 | 不是系统原生 SAF 界面，而是 Qt 控件版（与桌面同款），触屏下按钮偏小（界面缩放已按机型分档，见 §3 相关文档） |
| 云盘 | 只能选真实路径，云盘位置天然不可用（与 §2.4"不做导入兜底"一致） |

#### （4）同时移除的东西

| 已删除 | 原因 |
|--------|------|
| `packaging/android/java/.../picker/PickerActivity.java` | 自建选择器本体 |
| `mangaproof/storage/android_picker.py` | 共享文件协议实现 |
| `tests/test_android_picker.py` | 上述协议的测试 |
| `A11yEnvProvider` 里的选择器命令消费线程与 `MANGAPROOF_PICKER_DIR` | 协议已不存在 |
| `p4a_hook.py` 的入口 Activity 替换与 `extractNativeLibs` 注入 | 前者为接管 onActivityResult、后者无效（§2.12） |

**hook 现在只做一件事**：注入 `A11yEnvProvider`（无障碍开关 + 机型 dp）。
入口 Activity 保持 p4a 渲染结果不动（原样 `org.qtproject.qt.android.bindings.QtApplication`
+ p4a 的 entrypoint）。

#### （5）验证

| 项 | 手段 | 状态 |
|----|------|------|
| 桌面零改动 | `tests/test_picker.py`：断言桌面调用**不传任何 option**、标题/初始目录/过滤器逐参数一致 | ✅ |
| Android 分支 | 同一测试断言 Android 传 `DontUseNativeDialog`（目录另加 `ShowDirsOnly`）、起始目录可用性回退 | ✅ |
| hook 只做 provider 注入 | `tests/test_android_packaging_hook.py`（含幂等、缺清单/多 `</application>` 硬失败） | ✅ |
| 真机：能选到文件夹且不卡死 | 打开「文件 → 打开漫画文件夹」；`adb logcat` 不应再出现 `dlopen failed`；选择器为 Qt 控件版 | ⏳ 待真机 |

---

## 3. 强制横屏 + 全面屏全屏

### 3.1 清单/构建级配置（buildozer.spec 必须显式覆盖）

| 键 | buildozer 1.5.0 默认 | 我们要设 | 作用与依据 |
|----|---------------------|----------|------------|
| `orientation` | **`portrait`**（default.spec 第 54 行，未注释！） | `landscape`（锁定横屏）或 `sensorLandscape`（允许左右横屏，见下） | buildozer → p4a `--orientation`；p4a 在单值时映射为 manifest `android:screenOrientation="landscape"`【源码】p4a `common/build/build.py:803-822` |
| `fullscreen` | **`0`**（default.spec 第 77 行，未注释！） | `1` | 为 `1` 时 buildozer 不传 `--window` → 应用主题拼接 `.Fullscreen`：`@android:style/Theme.NoTitleBar.Fullscreen`【源码】buildozer `targets/android.py:1170-1177`、p4a qt 模板 `AndroidManifest.tmpl.xml` |
| `android.manifest.orientation` | 未设置 | 需要"传感器横屏"时设 `sensorLandscape` | 直接写 manifest 值，绕过 `--orientation` 的单值映射【源码】buildozer `:1204-1207` |
| `android.apptheme` | p4a 默认 `@android:style/Theme.NoTitleBar` | 保持默认（推荐）或自定义主题 | ⚠️ **坑**：fullscreen 时 p4a 会把主题名拼成 `<apptheme>.Fullscreen`，自定义主题必须额外定义 `X.Fullscreen` 变体 |
| `p4a.extra_args` | 由 pyside6-android-deploy 写成 `--qt-libs=… --load-local-libs=… --init-classes=…` | 在其后**追加** `--display-cutout shortEdges` | buildozer 1.5.0 **没有** display-cutout 键（实测其键列表里不存在）→ 只能走 p4a 参数 |

**`--display-cutout shortEdges` 的效果**（【源码】p4a qt 模板 `build/templates/strings.tmpl.xml`）：

```xml
<style name="KivySupportCutout">          <!-- activity 主题（模板固定引用） -->
    <item name="android:windowNoTitle">true</item>
    <item name="android:windowLayoutInDisplayCutoutMode">shortEdges</item>  <!-- ← 刘海/挖孔区可绘制 -->
    <item name="android:windowTranslucentStatus">true</item>
    <item name="android:windowTranslucentNavigation">true</item>
    <item name="android:windowFullscreen">true</item>
</style>
```

> 默认值 `never` 时，这些 item **全部不写入**（即：不进入刘海区、系统栏不透明）。这就是"全面屏全屏"的清单级开关。

**"锁死 landscape" 还是 "sensorLandscape"**（两者都只允许横屏，区别在"要不要跟着重力翻转 180°"）：

| manifest 值 | 行为 | 适用 |
|-------------|------|------|
| `landscape` | 只允许**一个**横屏方向（系统固定那一侧），设备转 180° 也不会翻过来 | 想要"永远同一方向"（截图/支持/工位固定） |
| `sensorLandscape` | 只允许横屏，但**两侧都可以**，由传感器决定朝哪边 | 手持/平板监制，用户可能为了插线或左利手把设备转 180° |
| `userLandscape` | 尊重系统"旋转锁定"偏好（用户锁竖屏时会变成竖屏） | ❌ 不适合"强制横屏" |
| `locked` | 锁定在当前物理方向 | ❌ 同上 |
| `reverseLandscape` | 与 `landscape` 相反的那一侧（配合 `landscape` 用可拼出"两侧"） | 备用 |

怎么设（两条通道，语义不同）：
- buildozer 的 `orientation` 键 → p4a `--orientation`：取值仅 `portrait/landscape/landscape-reverse/portrait-reverse`，**多值会被映射成 `unspecified`**（不是 sensorLandscape！）【源码】p4a `common/build/build.py:803-822`；
- 因此 **`sensorLandscape` 必须走 buildozer 的 `android.manifest.orientation` 键**（→ p4a `--manifest-orientation`，值原样写进 manifest）【源码】buildozer `targets/android.py:1204-1207`、p4a `get_manifest_orientation(..., manifest_orientation)` 原样返回。

> ⚠️ **重要前置警告（Android 16 / API 36）**：官方文档明确说明——**对 targetSdk ≥ 36 的应用，在 smallest width ≥ 600dp 的屏幕（平板、展开态折叠屏、桌面窗口）上，系统会忽略 `screenOrientation`**；opt-out 属性 `android.window.PROPERTY_COMPAT_ALLOW_RESTRICTED_RESIZABILITY` **不能**恢复方向锁定，而且该 opt-out 将在 API 37 被彻底移除。也就是说：**"强制横屏"在平板上只在 targetSdk ≤ 35 时有效**；如果将来把 targetSdk 提到 36，应用必须自己做到"两种方向都能用"。由于本项目**只侧载 APK、不上 Play**，targetSdk 取值不受 Play 政策约束 → 想保住强制横屏，就把 targetSdk 钉在 35，同时布局仍应容忍旋转（防 OEM/系统覆盖）。
> 【官方文档】[`<activity>` 元素 · android:screenOrientation](https://developer.android.com/guide/topics/manifest/activity-element)、[Device compatibility mode](https://developer.android.com/guide/practices/device-compatibility-mode)。

### 3.2 Qt 运行时行为（Qt 6.11 已内建，无需 JNI）

| 需求 | Qt 6.11 机制 | 依据 |
|------|--------------|------|
| 进入沉浸式全屏（隐藏状态栏/导航栏） | `window.showFullScreen()` → 平台层判定 `Qt::WindowFullScreen` → JNI 调 `QtWindowInsetsController.showFullScreen(activity)` | 【源码】`platforms/android/qandroidplatformwindow.cpp:249-266` |
| edge-to-edge（保留系统栏但内容铺满） | `Qt::ExpandedClientAreaHint` → `QtWindowInsetsController.showExpanded(activity)` | 【源码】同上 |
| 刘海/手势条安全区 | `QWindow::safeAreaMargins()`（**Qt 6.9 新增**）+ 变更信号 `safeAreaMarginsChanged(QMargins)`；Android 侧由 `QtWindowInsetsController` 回传 insets（left/top/right/bottom） | 【源码】`qwindow.cpp:1975-2025`、`qandroidplatformwindow.cpp:127-134, 391-425` |
| PySide6 是否可用 | ✅ `QWindow.safeAreaMargins()` 与 `safeAreaMarginsChanged` 在 6.11.2 绑定中存在 | 【实测】`PySide6/QtGui.pyi:10930, 11033` |
| QWidget 便捷获取 | `widget.window().windowHandle().safeAreaMargins()`（QWidget 本身没有该方法） | 【源码】Qt 只把该 API 放在 QWindow |

**实现要点（Qt Widgets 应用）**

1. 启动后：`main_window.showFullScreen()`（而非 `showMaximized()`）；
2. 布局安全区：在窗口 `resizeEvent` / `safeAreaMarginsChanged` 中把 `safeAreaMargins()` 应用到根布局 `setContentsMargins(left, top, right, bottom)`，避免工具栏/状态栏被刘海或手势条遮挡；
3. 横屏双栏：横向可用宽度充足，现有 `QDockWidget` 左右停靠可保留（但触屏拖动体验仍需 P3 评估），或改为左右固定面板 + 中间画布；
4. 状态提示：`QStatusBar` 在沉浸式下依然可用（自有 widget），但要把它的内容放进安全区；
5. 不要用 `QMenuBar`（Android 无原生菜单栏，Qt 会退化成窗口内自绘菜单栏，横屏下很占高度）→ 用 `QToolBar` / 顶部按钮；
6. **屏幕常亮（需注意：Qt bootstrap 下 p4a 的 `wakelock` 开关无效）**：`wakelock` meta-data 只有 **sdl2(kivy) bootstrap 的 `PythonActivity`** 会读（`pythonforandroid/bootstraps/sdl2/.../PythonActivity.java:179`），而 Qt bootstrap 用的是 Qt 自己的 activity → 设了也不会生效；同理 `--presplash` 也只由 kivy 的 activity 显示（`:496`）。若确实需要这两项，只能：(a) 通过 `android.add_resources` + `android.apptheme` 自定义主题，用 `android:windowBackground` 做"启动底色/Logo"（这条是 Android 框架行为，Qt bootstrap 下有效）；(b) 常亮则需注入自己的 Java（`android.add_src` + 覆盖 `<application android:name>` 为 QtApplication 的子类，在其 activity 生命周期回调里设置 `FLAG_KEEP_SCREEN_ON`）——**属未验证的额外工作项**，建议先不做。

### 3.3 验收标准

| 项 | 验收 |
|----|------|
| 强制横屏 | 设备竖持时界面仍为横屏；`adb shell dumpsys window \| grep mCurrentRotation` 或截图确认 |
| 不重建 | 旋转/分屏不触发重建（manifest `configChanges` 已含 `orientation\|screenSize\|smallestScreenSize\|density`【源码】p4a qt 模板） |
| 全屏 | 状态栏/导航栏隐藏；从边缘下滑可临时唤出（系统行为） |
| 刘海适配 | 带 cutout 的机型上，顶部工具栏与左侧列表不被切角遮挡（`safeAreaMargins()` 生效） |
| 手势条 | 底部内容内缩，标注拖拽不误触手势区 |
| 最低版本 | **Android 11（API 30）** —— minSdk 30（单一权限模型；Qt 6.11 支持下限本为 API 28，本项目主动上抬） |

---

### 3.4 首屏（启动底色 + Logo）—— 已实现

**要解决的问题**：首次启动要解包 Python 发行包，这段时间 Android 已经显示窗口、而 Qt 还没
画出第一帧 → 用户看到**白屏/黑屏**几百毫秒到数秒。p4a 的 presplash 机制在 **Qt bootstrap
下不生效**，所以改用 Android 框架自己的 `android:windowBackground`（窗口创建时由
system_server 绘制，与 Qt 无关，因此 bootstrap 无关）。

**资源**（`packaging/android/res/**`，经 `android.add_resources` 投放）

| 文件 | 作用 |
|------|------|
| `values/colors.xml` | `mangaproof_splash_bg = #202227`（需求方指定，与应用图标底板同色系） |
| `drawable/mangaproof_splash.xml` | `layer-list`：底色 + 居中 Logo |
| `values/themes.xml` | `MangaProofSplash` 与 `MangaProofSplash.Fullscreen` 两个变体，均含 `android:windowBackground` |
| Logo | 复用 `ico/Android-foreground.png` → 投放到 `drawable-nodpi/mangaproof_logo.png`（**不做密度缩放**，按原像素居中；432×432、可见图形约 190px 宽 ≈ 1080p 屏宽的 17.6%） |

**⚠️ 关键坑：只设 `android.apptheme` 不生效**

p4a 的 Qt 清单模板里两处主题是分开的：

```xml
<application android:theme="{{args.android_apptheme}}{% if not args.window %}.Fullscreen{% endif %}">
    <activity android:theme="@style/KivySupportCutout">      ← 硬编码
```

**Activity 主题覆盖 Application 主题**，而首屏观感由 Activity 窗口决定。所以真正有效的是
给 `@style/KivySupportCutout` 补一条 `android:windowBackground`：

- `android.apptheme` 仍然指向 `@style/MangaProofSplash`（覆盖 apptheme 路径，两个变体都已定义——
  `fullscreen=1` 时 p4a 会拼成 `MangaProofSplash.Fullscreen`）；
- 由 `packaging/android/p4a_hook.py` 的 `_patch_splash_background()` 在
  `after_apk_build`/`before_apk_assemble` 阶段**就地改写 dist 的 `res/values/styles.xml`**，
  往 `KivySupportCutout` 里插入 `android:windowBackground`；
- 找不到可改写的 styles.xml 时**只警告不阻断**（首屏是观感问题，不该让整条流水线失败），
  由 CI 的 aapt2 校验兜底发现。

**为什么不会在运行期残留**：`windowBackground` 只由窗口装饰层绘制；Qt 画出第一帧（不透明、
铺满整窗）后即被覆盖。

**验证**

| 项 | 手段 | 状态 |
|----|------|------|
| 注入位置正确 | `tests/test_android_packaging_hook.py`：断言 `windowBackground` 落在 `KivySupportCutout` 内、其它主题不动、原有挖孔配置保留 | ✅ |
| 幂等 / 缺文件不阻断 | 同上（二次执行清单不变；无 styles.xml 时返回 False 且不抛错） | ✅ |
| 资源与构建参数齐全 | 同上：`#202227`、两个主题变体、三条 `add_resources`、`android.apptheme` | ✅ |
| 资源真的进包且被引用 | CI：`aapt2 dump resources` 断言 `mangaproof_splash` / `mangaproof_splash_bg` 存在（缺了硬失败），并检查引用 | ✅（待下一轮构建实测） |
| 真机观感（冷暖色是否与 Qt 首帧一致、Logo 大小是否合适） | 装机看冷启动；不满意只需替换 `drawable-nodpi` 的 PNG 或改 `#202227` | ⏳ 待真机 |

> Logo 大小的取舍：当前用**原始像素**（不随密度缩放），因此在 1080p 手机上约占屏宽 17.6%
> （190/1080）、1440p 上约 13.2%、720p 上约 26.4%，平板上更小。要更大/更小只需换
> `ico/Android-foreground.png` 的尺寸（或改用别的图），XML 无需改动。
>
> ⚠️ 注意「首屏 Logo」与「图标前景」共用同一个文件：2026-09-15 美术把前景内容从 245×254
> 缩到 190×197 后，首屏 Logo 也**跟着变小了 22.4%**。若后续只调其中一个，需改为各自独立的文件。

---

## 4. 分层图标（自适应图标）

### 4.1 现有资源与目标（美术资源已就位 ✅）

| 资源 | 用途 | 结论 |
|------|------|------|
| `ico/Android-foreground.png`（432×432 RGBA） | 自适应图标**前景层** | ✅ 合规（实测见 §4.6） |
| `ico/Android-background.png`（432×432 RGBA） | 自适应图标**背景层** | ✅ 可用（有 0.92% 像素 alpha=230/242，非纯不透明；见 §4.6） |
| `ico/Android-fallback.png`（432×432 RGBA） | 旧系统/极端场景的**合成兜底图**（= 前景 over 背景） | ✅ 实测确为合成图（与"前景 over 背景"平均差 2.18） |
| `ico/ico.png`（1024×1024 RGBA） | 桌面运行时图标；Android 上作 `icon.filename` legacy 兜底 + **启动屏居中 logo** | ✅ |
| `ico/ico.ico` / `ico.icns` | Windows / macOS | Android 不用 |
| monochrome 层 | Android 13+ 主题图标 | ❌ 需求方明确**不做**

### 4.2 buildozer / p4a 的链路（已核实）

| 步骤 | 行为 |
|------|------|
| `icon.filename` | p4a 复制为 `res/mipmap/icon.png`（**无密度限定**；缺省时用 p4a 自带 kivy 图标）【源码】p4a `common/build/build.py:425-429` |
| `icon.adaptive_foreground.filename` + `icon.adaptive_background.filename` | **两者必须同时给**；否则 p4a 打印 warning 并忽略【源码】同上 `:430-443` |
| 生成物 | `res/mipmap/icon_foreground.png`、`res/mipmap/icon_background.png`、`res/mipmap-anydpi-v26/icon.xml`：`<adaptive-icon><background/><foreground/></adaptive-icon>`（**不含 monochrome 层**） |
| manifest | `android:icon="@mipmap/icon"` → API 26+ 自动使用 `mipmap-anydpi-v26/icon.xml`【源码】p4a qt 模板 `AndroidManifest.tmpl.xml` |
| 附加资源通道 | `android.add_resources = <src>:<dest>` → 复制到 `src/main/res/<dest>`（可用来补密度专用图或自定义 XML）【源码】p4a `common/build/build.py:414-421` |
| ⚠️ **顺序坑** | p4a 先复制用户资源、**后**生成图标资源 → 若同时设置 `icon.adaptive_*` 与自定义 `mipmap-anydpi-v26/icon.xml`，**自定义文件会被覆盖**。所以只有两条自洽路线：(a) 用 `icon.adaptive_*` 键（不支持 monochrome）；(b) 完全不设 `icon.adaptive_*`，全部用 `android.add_resources` 提供 XML + 各密度层 |
| ❌ **p4a bug ⇒ 路线 (a) 在 Qt 路线下不可用** | p4a `bootstraps/common/build/build.py:433` 生成自适应图标时直接 `open(join(res_dir, 'mipmap-anydpi-v26/icon.xml'), "w")`，**却从不创建该目录**；该目录在 SDL bootstrap 模板里有、**Qt bootstrap 模板里没有**，git 又不跟踪空目录 → 必然 `FileNotFoundError`（CI run 34914938082 实测）。**因此本项目改用路线 (b)**：不设 `icon.adaptive_*`，改由 `android.add_resources` 自带 `mipmap-anydpi-v26/icon.xml` + 两层 PNG —— p4a 的文件模式会先 `ensure_dir(dirname(dest))` 再复制，目录自然被创建【源码】`common/build/build.py:414-421` |

### 4.3 美术资源规格契约（与安卓官方一致，可直接发给美术）

【官方文档】Android "Adaptive icons"：

| 项 | 规格 |
|----|------|
| 画布 | **两层均为 108 × 108 dp** |
| 安全区 | **中央 66 × 66 dp**（永不被 OEM 遮罩裁切，图形必须落在此区内） |
| logo 尺寸 | 至少 48 × 48 dp，且不超过 66 × 66 dp |
| 前景层 | 透明 PNG；图形居中；**不要**自带遮罩/圆角/投影 |
| 背景层 | 不透明 PNG（纯色或纹理均可） |
| 密度像素（建议交付 432×432 单图或全套） | mdpi 108 / hdpi 162 / xhdpi 216 / xxhdpi 324 / **xxxhdpi 432** |
| 单色层（可选，Android 13+ 主题图标） | p4a 生成的 XML **不含** `<monochrome>`，且会覆盖同名自定义 XML → 要支持主题图标只能走路线 (b)：不设 `icon.adaptive_*`，用 `android.add_resources` 自带 `mipmap-anydpi-v26/icon.xml`（含 monochrome）与各密度层 |

**建议交付路径（约定，待美术确认）**

```
ico/android/ic_launcher_foreground.png     # 432×432，透明，图形在中央 264×264 px（=66dp@xxxhdpi）内
ico/android/ic_launcher_background.png     # 432×432，不透明
ico/android/ic_launcher_monochrome.png     # 可选，432×432，单色（Alpha 蒙版）
```

> 换算给美术的口径：`432 px = 108 dp`（xxxhdpi，4×）；安全区 `66 dp = 264 px`；logo 建议 `48–66 dp = 192–264 px`。

### 4.4 接入步骤 —— ✅ 已采用路线 (b)（路线 (a) 被 p4a 的 bug 堵死）

**路线 (b)：自带图标资源（本项目现行实现，见 `scripts/android/build_android.py` 的 `RESOURCE_ENTRIES`）**

```
android.add_resources =
  ico/Android-foreground.png:mipmap/icon_foreground.png              # 无密度兜底，保证 @mipmap 引用在任何密度可解析
  ico/Android-background.png:mipmap/icon_background.png
  ico/Android-foreground.png:mipmap-xxxhdpi/icon_foreground.png      # 432×432 主图，高密度设备直接用原图
  ico/Android-background.png:mipmap-xxxhdpi/icon_background.png
  ico/android/res/mipmap-anydpi-v26/icon.xml:mipmap-anydpi-v26/icon.xml   # 自适应图标入口（同时把该目录创建出来）
```

配套：`icon.filename = ico/Android-fallback.png`（legacy `mipmap/icon.png`）；**不设** `icon.adaptive_foreground/background.filename`。
文件模式投放时 p4a 会先 `ensure_dir(dirname(dest))` 再复制（`common/build/build.py:414-421`），因此 `mipmap-anydpi-v26/` 会被创建——这既满足资源引用，也正好绕开 p4a 那个"写 XML 不建目录"的 bug。
将来要做 Android 13 主题图标：只需在 `ico/android/res/mipmap-anydpi-v26/icon.xml` 里加一行 `<monochrome android:drawable="@mipmap/icon_monochrome"/>` 并投放对应 PNG，**不需要**动构建脚本。

**（已废弃）路线 (a)：用 buildozer 的 `icon.adaptive_*` 键**

p4a 生成 XML 时直接 `open('res/mipmap-anydpi-v26/icon.xml', "w")` 却从不建目录，Qt bootstrap 模板里也没有该目录 → 构建在打包阶段必然 `FileNotFoundError`（run 34914938082）。除非上游修掉，否则不要走这条。

**CI 校验**（Pillow，桌面 Python 即可跑）：尺寸 = 432×432、前景层含透明通道、背景层无透明、前景图形像素落在中央 264×264 px 安全区内（包围盒断言）。

### 4.5 接入时的注意事项（资源已就位）

- 三层一起给：`icon.filename`（legacy）+ 自适应 XML + 两层 PNG（**走路线 b**，不要设 `icon.adaptive_*` 键）；
- minSdk 30 下自适应图标（API 26+）**始终生效**，`Android-fallback.png` 实际只在极端/老启动器场景被用到——保留即可，不必为它优化；
- **可选增强**：把 432×432 主图用 Pillow 缩成 mdpi/hdpi/xhdpi/xxhdpi 各套并逐条投放（现只投了无密度 + xxxhdpi 两档，中低密度由系统下采样，观感已可接受）。

### 4.6 图标资源实测核验（2026-09-15 美术更新后复验）

用 Pillow 对三个文件做的量化核验（安全区 = 中央 264×264 px，即 66 dp @xxxhdpi；本文件 432 px = 108 dp，故 1 dp = 4 px）：

| 文件 | 内容 bbox | 内容尺寸 | 内容中心 vs 画布中心(215.5) | 安全区 | 透明度 |
|------|-----------|----------|------------------------------|--------|--------|
| `Android-foreground.png` | (122, 118, 312, 315) | 190×197 | (216.5, 216.0) → **居中 ✅**（偏移 +1.0/+0.5） | 197 ≤ 264 → **未越界 ✅** | 85.9% 完全透明（正常，前景层就该大面积透明） |
| `Android-background.png` | 满幅 | 432×432 | — | — | 无全透明像素；**0.92% 像素 alpha=230/242**（角落/边缘轻微半透明）⚠️ 建议改为纯 255 |
| `Android-fallback.png` | 满幅 | 432×432 | — | — | 同上；且实测 ≈ `前景 over 背景` 的合成结果（平均差 2.18/255）✅ 符合"已合成的兜底图"定位 |

**前景占比（本次美术优化的重点）**

| 指标 | 旧版（245×254） | 现版（190×197） |
|------|------------------|------------------|
| 内容尺寸 | 245×254 px = 61.3×63.5 dp | **190×197 px = 47.5×49.2 dp** |
| 占 72 dp 可见区宽 | 85.1% | **66.0%** |
| 占 66 dp 安全区宽 | 92.8% | **72.0%** |
| 内容面积 / 画布 | 33.4% | **20.1%**（面积 -39.9%） |
| 内容落入 288 px 可见圆的比例 | — | 40.4% |

缩到 66% 是自适应图标的常见做法：mask（圆形 / 圆角方形 / squircle）会把 108 dp 画布裁到
72 dp 可见区，留白不足时边缘元素会被裁掉；现在内容宽 190 px（47.5 dp）只占 66 dp 安全区的
72%，四周还有约 28% 余量，任何 mask 形状都不会切到图形。

**兜底图与前景层的一致性**（本次同步重绘，避免"新系统小图标、老系统大图标"割裂）：
`Android-fallback.png` 内"非背景"区域的 bbox = `(121, 117, 311, 315)`，与新前景
`(122, 118, 312, 315)` 对齐（±1 px，差异来自合成时的抗锯齿）。

结论：**前景/背景/兜底三层都可直接入库使用**；唯一可选的打磨是背景层与兜底图那 0.92% 的半透明像素（不影响自适应图标显示，但严格说背景层最好全不透明）。

---

## 5. 内存回收策略：只允许"激进"

### 5.1 实现（代码事实，2026-09-15 落地）

平台判定的唯一入口是 `android_memory_policy_locked()`（内部走 `utils/platform.is_android_strict`，
编译期判据、不含环境变量、异常一律按非 Android 处理）——与 `android_ui_scaling()` 同构，
避免多处各自判平台。

| 位置 | 内容 |
|------|------|
| `config/settings.py:226-228` | `MEMORY_POLICIES`（三档）；`DEFAULT_MEMORY_POLICY = "balanced"`（桌面默认，**未改**）；`ANDROID_MEMORY_POLICY = "aggressive"` |
| `config/settings.py:242-250` | `android_memory_policy_locked()`：是否 Android（锁定判定的唯一入口） |
| `config/settings.py:252-261` | `default_memory_policy()`：平台默认档——Android 激进 / 桌面平衡 |
| `config/settings.py:263-276` | `effective_memory_policy()`：把设置里的档位规整成本平台该用的档——Android 一律激进，桌面非法值回落平衡 |
| `config/settings.py:833-836` | `_from_dict()` 读取时即规整：非法的旧值回落**平台默认**（不是硬编码 balanced） |
| `config/settings.py:514-544` | `reconcile_android_memory_policy()`：启动期把锁定的值写回 `settings.json` |
| `main.py:162-164` | 启动序列里调用上面的 reconcile（桌面端直接返回，不落盘） |
| `config/settings.py:676-683` | `SettingsManager.path`：reconcile 读**本实例**路径，不用 `paths.settings_path()`（否则测试注入的 `tmp_path` 会被绕过、写到真实程序目录） |
| `ui/main_window.py:1324-1340` | `_apply_memory_policy()` 先过 `effective_memory_policy()` 再查档位表，未知档位记 warning 后回落 balanced |
| `ui/settings_dialog.py:397-421` | 三档下拉；Android 上 `setEnabled(False)` **禁用但保留**（与同文件 `console_check` 的非 Windows 处理同做法），tooltip 换成平台说明 |
| `ui/settings_dialog.py:551-553` | 「恢复默认」复位到 `default_memory_policy()`——Android 上复位到激进（否则灰控件会显示成"平衡"，与事实不符） |
| `ui/settings_dialog.py:585-588` | `apply_to()` 写入前再过一次 `effective_memory_policy()`：程序化改下拉也绕不过锁定 |
| `ui/main_window.py:373` / `config/settings.py:278-282` | 首次运行提醒条文案按平台给：Android 版不提"内存策略"（它已不可调，提了会指向一个改不了的设置项） |
| 实际数值（仍是 `ui/main_window.py:131-135` 的 `_MEMORY_POLICIES`） | `aggressive`：bg 池 **68 MB** + LRU **256 MB**；`balanced`：512 MB + 512 MB；`relaxed`：768 MB + 768 MB |

**三个容易踩的坑（都有测试守着）**

1. **写回的判据必须是文件里的原始值**，不能看 `manager.settings.memory_policy`——后者在读取阶段
   就被强制成 aggressive 了，拿它判断会永远"已一致"，写回形同虚设（`config/settings.py:529-532`）。
2. **首次运行（文件不存在）不写回**：此时内存值就是默认激进，磁盘上没有旧值要纠正；凭空建出
   `settings.json` 会让「首次使用」提醒条立刻收起（`_refresh_settings_banner` 判 `has_settings_file`）。
3. **非法值在 Android 上回落激进而不是 balanced**：`_from_dict` 原来硬编码回落
   `DEFAULT_MEMORY_POLICY`，照搬会让一份损坏的配置静默拿到 512 MB 档。

**不做的事**：不改三档预算数值（需求方决策 #7：就用激进档，不再额外收紧）；桌面端行为零改动
（默认值、三档可选、复位逻辑全部照旧）。

### 5.2 测试（`tests/test_android_memory_policy.py`，10 项）

| 覆盖 | 断言 |
|------|------|
| 平台判定与规整 | Android 锁定/桌面不锁；`effective_memory_policy` 对 `relaxed/balanced/aggressive/turbo/None/42` 在两端各自的输出 |
| 强制 + 写回 | 旧配置写 `balanced` → 读到 `aggressive`，reconcile 后文件里也是 `aggressive`；再调一次返回 `None`（幂等） |
| 非法值/缺键 | Android 回落 aggressive；桌面回落 balanced |
| 桌面不受影响 | 读到 `relaxed` 就是 `relaxed`；reconcile 不落盘；文件里不出现 `aggressive` |
| 路径正确性 | 写回落在 `manager.path`；首次运行不凭空创建文件 |
| 设置页 | Android 控件 `isEnabled() is False`、显示"激进"、tooltip 含"固定"；「恢复默认」复位到激进；绕过控件改下拉后 `apply_to()` 仍写回激进。桌面 `count() == 3` 且可编辑 |
| 横幅文案 | Android 文案不含"内存策略"、含"界面缩放"；桌面含"内存策略" |

> 测试用 `monkeypatch.setattr(settings_mod, "is_android_strict", lambda: True)` 模拟平台
> （与 `tests/test_android_ui_scale.py` 同手法）；在真实 Android 上跑时桌面侧用例自动 skip。

### 5.3 风险与验收

| 风险 | 说明 | 验收/缓解 |
|------|------|-----------|
| 大 PSD 频繁重解码 | 激进档意味着窗口外图层更早被逐出，切页/切层可能重解码 | 真机用 15 页·大 PSD 压测：连续翻页 30 次，记录卡顿与重解码次数 |
| 进程被杀后状态 | Android 可能直接杀进程 | 依赖现有"任务文件自动保存 + 启动恢复"链路；确认自动保存频率在 Android 上够用 |
| 双档位语义混淆 | 桌面仍是三档 | 设置页用 tooltip 说明"Android 固定为激进"；桌面文案不变 |
| 峰值内存仍偏高 | Qt + numpy + Pillow 自身占用；256 MB LRU 是桌面视角的"激进" | 用 `adb shell dumpsys meminfo <pkg>` 观察 PSS 峰值；数据说话后再决定是否调数值 |

---

## 6. 实施计划（在本轮需求下更新）

| 阶段 | 内容 | 验收 |
|------|------|------|
| **P0 打包链路（APK only）** | 上游文档 §5 的 CI + 包装脚本；`android.release_artifact=apk`；`android.api=35`（targetSdk）、`minapi=30`；`android.manifest.orientation=sensorLandscape`、`fullscreen=1`、`p4a.extra_args` 追加 `--display-cutout shortEdges`；`MANAGE_EXTERNAL_STORAGE` 权限；启动底色主题；**内存强制激进 ✅ 已实现（§5）** | CI 出签名 APK；模拟器/真机安装后**横屏全屏**启动；设置页内存策略显示"激进"且灰显不可改；未授权时弹窗（§2.7） |
| **P1 选目录 + 路径直读** | `storage/**`（`android_uri.py` 映射与权限检测、`picker.py` 选目录、`service.py`）；`main_window` 改用 Url 版选择器 + 映射；任务文件写回原目录 | 真机从"本机存储"选目录 → 直读标注 → `.mangaproof.json` 落在原目录；云盘/受限目录给出明确拒绝提示；全程 UI 不卡 |
| **P2 真机核验 + 收尾** | §2.10 的核验清单（URI 形态、卷 UUID、权限检测、性能、内存）；图标接入 + CI 图标校验；PDF 导出路径；启动底色资源 | 内置存储/SD 卡/Download 三类目录都能直读；图标自适应生效；大 PSD 不 OOM |
| **P3 触屏交互改造** | 菜单/工具栏化、Dock 改面板、手势缩放/长按菜单、快捷键辅助（外接键盘） | 全程无键鼠完成"打开目录→标注→导出 PDF" |

> 说明：本轮需求（1/2/4/5）主要落在 P0–P2；P3 是上一份文档就列出的独立主线，未被本轮需求取消。

---

## 7. 决策记录（需求方已确认，2026-09-15）

| # | 问题 | **已确认的决定** |
|---|------|------------------|
| 1 | 目录访问模型 | **真实路径直读、不兜底**；为此申请 **MANAGE_EXTERNAL_STORAGE（所有文件访问权限）**；SAF 仅用于选目录 + URI→路径映射（§2.0 / §2.8） |
| 2 | `.mangaproof.json` 位置 | **与桌面一致**：写在打开的文件/文件夹所在目录 |
| 3 | 横屏方向 | **`sensorLandscape`**（允许左右横屏自动翻转） |
| 3b | targetSdk / minSdk | **targetSdk 35**（保住平板强制横屏）/ **minSdk 30**（Android 11，单一权限模型） |
| 4 | 图标 | 美术三层资源**已就位**：`ico/Android-foreground.png`、`ico/Android-background.png`、`ico/Android-fallback.png`（已合成的旧系统兜底图）；**不使用 monochrome 层**；实测结论见 §4.6 |
| 5 | 屏幕常亮 | **不需要**（连带省掉 Java 注入工作项） |
| 6 | 最低 Android 版本 | **API 30**（随 ① 的权限模型上抬；Qt 官方下限为 28） |
| 7 | Android 内存预算 | **就用激进档**（LRU 256 MB + bg 池 68 MB），不再额外收紧。已实现：读取强制 + 写回配置 + 设置页禁用该档（§5.1） |
| 8 | 首屏启动底色/Logo | **做**：纯色底 `#2b2d30` + 用 `ico/ico.png` 作居中 logo；通过自定义主题 `android:windowBackground` 实现（注意 `.Fullscreen` 后缀） |
| 9（新增·待确认） | 无法映射的目录（云盘/网络位置、媒体库入口、其他应用专属目录） | 方案按**直接拒绝 + 提示"请从『本机存储』入口选择文件夹"**处理（§2.9）——如无异议即按此实现 |
| 10 | 辅助功能（无障碍 / 读屏） | **永久不适配**（需求方明确：本项目是效率工具，未来也不准备适配无障碍）。Android 端的做法是用 Qt 官方开关 `QT_ANDROID_DISABLE_ACCESSIBILITY=1` **主动声明不参与**，以规避部分系统（HyperOS）读屏查询与 Qt 主线程建窗并发导致的死锁——实现见打包文档 §5.10 ⑦ |
| 11 | 退出阶段的闪退 | **Android 上直接 `os._exit()`**（跳过 CPython finalize 与 Qt/C++ 析构），桌面仍是 `sys.exit()`。理由：退出阶段崩溃来自 QTBUG-85449 家族，且数据/日志早已落盘（`closeEvent` 先于 `exec()` 返回，日志逐条 flush）→ 跳过收尾无副作用。**不设** `QT_ANDROID_NO_EXIT_CALL=1`：对本路径无效，且其"让进程别退出"的语义在异常路径下会从"崩溃"变成"卡死"。见打包文档 §5.10 ⑧ |

---

## 8. 本轮证据索引（新增）

| 论断 | 位置 |
|------|------|
| Qt Android 原生文件对话框（SAF）实现 | qtbase 6.11 `src/plugins/platforms/android/qandroidplatformfiledialoghelper.cpp`（目录模式 → `ACTION_OPEN_DOCUMENT_TREE`；保存 → `ACTION_CREATE_DOCUMENT`；`:47,65,76-90` 持久化授权） |
| `content://` 文件引擎（读写/枚举/删除/重命名） | qtbase 6.11 `src/plugins/platforms/android/androidcontentfileengine.cpp`（`open` :53-113、`size` :129、`remove/rename/mkdir/rmdir` :134-193、`fileFlags` :214-231、目录迭代 :257-300 与 `listFiles`） |
| Android 11+ 公共目录必须走 SAF | qtbase 6.11 `src/corelib/io/qstandardpaths.cpp`（Android 路径表与注意项，`:353-356`） |
| content URI 的官方限制说明 | qtbase 6.11 `src/corelib/doc/src/includes/android-content-uri-limitations.qdocinc` |
| psd-tools 支持 file-like 且只需 read/seek/tell | 本机 `.venv/.../psd_tools/api/psd_image.py:214-239`；全仓无 `mmap`/`fileno()` 依赖 |
| PySide6 无 QJniObject/QJniEnvironment | 本机 `PySide6/QtCore.pyi` 无该符号；Android wheel 的 `QtCore.abi3.so` 亦查无（本轮实测） |
| PySide6 有 Url 版文件对话框与 safeAreaMargins | `PySide6/QtWidgets.pyi:1596,1602,1604,1608`；`PySide6/QtGui.pyi:10930,11033` |
| buildozer 1.5.0 默认 `orientation=portrait`、`fullscreen=0` | buildozer 1.5.0 `buildozer/default.spec:54,77`（未注释） |
| buildozer → p4a 的方向/全屏/主题映射 | buildozer 1.5.0 `buildozer/targets/android.py:1170-1177`、`:1204-1207`；p4a `bootstraps/common/build/build.py:803-822, 877, 924-946` |
| 刘海适配的样式注入 | p4a `bootstraps/qt/build/templates/strings.tmpl.xml`（`windowLayoutInDisplayCutoutMode` / `windowTranslucent*` / `windowFullscreen`，仅当 `--display-cutout != never`） |
| 应用主题 `.Fullscreen` 拼接 | p4a `bootstraps/qt/build/templates/AndroidManifest.tmpl.xml`（`android:theme="{{args.android_apptheme}}{% if not args.window %}.Fullscreen{% endif %}"`） |
| Qt 全屏 → Android 沉浸式 | qtbase 6.11 `src/plugins/platforms/android/qandroidplatformwindow.cpp:249-266`（`showFullScreen`/`showExpanded`/`showNormal`）、`:127-134, 391-425`（safe area insets） |
| `safeAreaMargins()` 语义与版本 | qtbase 6.11 `src/gui/kernel/qwindow.cpp:1975-2034`（\since 6.9） |
| 自适应图标链路与"必须同时给两层" | buildozer 1.5.0 `buildozer/targets/android.py:1144-1150`；p4a `bootstraps/common/build/build.py:425-443` |
| 自适应图标规格（108/66/48 dp） | Android 官方 "Adaptive icons"（developer.android.com） |
| 云端模拟器冒烟 | `ReactiveCircus/android-emulator-runner`（tag `v2` / `v2.38.0`；README：ubuntu runner + 开启 KVM；`target` 支持 `google_apis_ps16k` 等 16 KB 镜像） |
| 内存策略代码位置 | 本仓库 `config/settings.py:217-220,252-253,483-484,511`、`ui/main_window.py:123-131`（三档预算真值：aggressive = bg 68 MB + LRU 256 MB）、`ui/main_window.py:1313-1320`、`ui/settings_dialog.py:368-378,510-511,536`、`psd/image_cache.py:65` |
| `wakelock` meta-data 只有 kivy bootstrap 消费 | p4a `develop` `pythonforandroid/bootstraps/sdl2/build/src/main/java/org/kivy/android/PythonActivity.java:179`（`mMetaData.getInt("wakelock")`）；`pythonforandroid/bootstraps/qt/**` 中无该逻辑 |
| `--presplash` 同样只由 kivy activity 显示 | 同上 Java `:496`（`getIdentifier("presplash","drawable")`）；p4a `bootstraps/common/build/build.py:462`（复制到 `res/drawable/presplash.jpg`） |
| 任务身份校验只看大小 + 抽样 SHA-256 | 本仓库 `review/persistence.py`（`build_file_records` / `verify_single` / `verify_folder`） |

---

## 9. 与上游文档的差异（上游需要同步修订的点）

| 上游文档位置 | 原内容 | 本轮修订 |
|--------------|--------|----------|
| §2.2 硬约束 4 | 把"SAF 改造"列为纯产品级大改造 | 修订：文件访问改为 **MANAGE_EXTERNAL_STORAGE + 真实路径直读**，SAF 只用于选目录 → 打包侧零成本，改造集中在 `storage/**` 薄层 |
| §2.2 / §5.3 / §5.4 | `minapi=28`、`ANDROID_MIN_API=28` | **修订为 30**（Android 11）：全文件访问权限的下限，同时让权限模型单一 |
| §5.5 / §5.6 | 并列 APK 与 AAB 两条签名路径 | 修订：本轮只出 **APK**；AAB 降级为"将来上 Play 时的可选开关" |
| §7 实施计划 | P1"依赖完整+签名"、P3"交互改造" | 修订：P0 打包链路（含横屏/全屏/权限/启动底色/内存强制激进）→ P1 选目录+路径直读 → P2 真机核验+图标+导出 → P3 触屏交互 |
| §5.7 workflow 骨架 | 未含横屏/刘海/图标/内存/权限相关注入 | 修订：包装脚本需注入 §3.1、§4.5、§2.0 的键值（含 `MANAGE_EXTERNAL_STORAGE`） |
| §4.2 G8 / §6 R8 | "无 SAF 就打不开用户目录" | 修订：改为"未授权全文件访问就打不开用户目录"，并给出弹窗+拒绝策略 |

### 9.1 第三轮新增证据

| 论断 | 位置 |
|------|------|
| MANAGE_EXTERNAL_STORAGE 授予"**直接文件路径访问**"、含 SD/OTG 根目录，且**不能**访问其他应用专属目录 | Android 官方 [manage-all-files](https://developer.android.com/training/data-storage/manage-all-files)（原文已逐条摘录于 §2.0） |
| 授权入口是系统设置页（`ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION`），测试可用 `adb shell appops set --uid <pkg> MANAGE_EXTERNAL_STORAGE allow` | 同上 |
| Qt 把 `ACTION_OPEN_DOCUMENT_TREE` 的结果 URI **原样**返回给应用（`intent.getData()` → `QUrl(uri.toString())`） | qtbase 6.11 `src/plugins/platforms/android/qandroidplatformfiledialoghelper.cpp:34-52` |
| `raw:` 前缀就是"真实路径" | AOSP `DownloadProvider/src/com/android/providers/downloads/RawDocumentsHelper.java:35-46`（`RAW_PREFIX="raw:"`、`getDocIdForFile()="raw:"+file.getAbsolutePath()`） |
| Downloads provider 的根指向系统下载目录 | AOSP `DownloadStorageProvider.java`（`DOC_ID_ROOT = Constants.STORAGE_ROOT_ID`；`getExternalStoragePublicDirectory(DIRECTORY_DOWNLOADS)`） |
| 媒体库 provider 的 docId 前缀（`image:`/`video:`/`audio:`/`document:`）→ 需 MediaStore 查询 → 本方案拒绝 | AOSP `MediaProvider/src/com/android/providers/media/MediaDocumentsProvider.java:118-133` |
| 平台把 `com.android.externalstorage.documents` 作为已知常量 | AOSP `frameworks/base/core/java/android/provider/DocumentsContract.java:245` |
| 三个图标资源的量化核验结果 | 本机 Pillow 实测（§4.6） |
| 退出阶段崩溃的官方机制描述（"C++ threads… destroying these without joining them terminates an application"）与官方绕行方式（不调用 `exit()`、交给 Android 系统处理） | Qt 官方 [Qt for Android Environment Variables](https://doc.qt.io/qt-6/android-environment-variables.html) → `QT_ANDROID_NO_EXIT_CALL`（Qt 6.11 页，已复核） |
| `QT_ANDROID_NO_EXIT_CALL` 只管 Qt 自己那条收尾 | qtbase `androidjnimain.cpp`（main 返回后 `if (!qEnvironmentVariableIsSet("QT_ANDROID_NO_EXIT_CALL")) exit(ret);`）；bug tracker **QTBUG-85449**「Android: crash on exit」 |
| 关窗时数据先落盘、退出不依赖 atexit/析构 | 本仓库 `ui/main_window.py:2443-2462`（`closeEvent` → `save_task()` + `_save_settings()`）；`utils/logging_setup.py:100`（`RotatingFileHandler`，`StreamHandler.emit` 每条记录后 flush） |
| APK 的真正入口就是仓库根 `main.py` | PySide6 `scripts/deploy_lib/config.py:106,268`（`input_file` → buildozer.spec `[app] input_file`）；本仓库 `main.py` |

> 未能直接抓到的证据：AOSP **ExternalStorageProvider** 的源码（该 git 仓库路径已迁移/不可直接访问）→ `primary:<rel>` / `<UUID>:<rel>` 规则标为"高置信度、待真机核验"（§2.10 第一项）。
