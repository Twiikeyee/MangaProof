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
| 内存只用激进 | **必须改代码**：当前默认是 `balanced`，且设置页三档可选；Android 需强制 `aggressive` 并隐藏其它档 + 加后台释放钩子 | 【源码】`config/settings.py:217-253`、`ui/main_window.py:1313`、`ui/settings_dialog.py:368-378` |
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

## 4. 分层图标（自适应图标）

### 4.1 现有资源与目标（美术资源已就位 ✅）

| 资源 | 用途 | 结论 |
|------|------|------|
| `ico/Android-foreground.png`（432×432 RGBA） | 自适应图标**前景层** | ✅ 合规（实测见 §4.6） |
| `ico/Android-background.png`（432×432 RGBA） | 自适应图标**背景层** | ✅ 可用（有 0.92% 像素 alpha=230/242，非纯不透明；见 §4.6） |
| `ico/Android-fallback.png`（432×432 RGBA） | 旧系统/极端场景的**合成兜底图**（= 前景 over 背景） | ✅ 实测确为合成图（与"前景 over 背景"平均差 2.17） |
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

### 4.6 图标资源实测核验（2026-09-15 复验）

用 Pillow 对三个文件做的量化核验（安全区 = 中央 264×264 px，即 66 dp @xxxhdpi）：

| 文件 | 内容 bbox | 内容尺寸 | 内容中心 vs 画布中心(215.5) | 安全区 | 透明度 |
|------|-----------|----------|------------------------------|--------|--------|
| `Android-foreground.png` | (94, 90, 339, 344) | 245×254 | (216.0, 216.5) → **居中 ✅** | 254 ≤ 264 → **未越界 ✅**（上一版的"上边溢出 2px"已修好） | 76.8% 完全透明（正常，前景层就该大面积透明） |
| `Android-background.png` | 满幅 | 432×432 | — | — | 无全透明像素；**0.92% 像素 alpha=230/242**（角落/边缘轻微半透明）⚠️ 建议改为纯 255 |
| `Android-fallback.png` | 满幅 | 432×432 | — | — | 同上；且实测 ≈ `前景 over 背景` 的合成结果（平均差 2.17/255）✅ 符合"已合成的兜底图"定位 |

结论：**前景/背景/兜底三层都可直接入库使用**；唯一可选的打磨是背景层与兜底图那 0.92% 的半透明像素（不影响自适应图标显示，但严格说背景层最好全不透明）。

---

## 5. 内存回收策略：只允许"激进"

### 5.1 现状（代码事实）

| 位置 | 内容 |
|------|------|
| `config/settings.py:217-220` | `MEMORY_POLICIES = ("relaxed","balanced","aggressive")`；`DEFAULT_MEMORY_POLICY = "balanced"` |
| `config/settings.py:252-253` | `memory_policy` 字段；读取时校验非法值回落默认（`:483-484`），保存时写回（`:511`） |
| `ui/main_window.py:1313-1320` | `_apply_memory_policy()`：从 `_MEMORY_POLICIES` 取 `lru_bytes` 与 bg 池配额，调用 `self._layer_cache.set_max_bytes(...)` |
| `ui/settings_dialog.py:368-378` | 三档下拉（宽松/平衡/激进），`:510-511` 恢复默认，`:536` 读回 |
| `psd/image_cache.py:65` | `set_max_bytes()` 运行时调整 LRU 预算（线程安全） |
| 实际数值（`ui/main_window.py:123-131` 的 `_MEMORY_POLICIES`） | `aggressive`：bg 池 **68 MB** + LRU **256 MB**；`balanced`：512 MB + 512 MB；`relaxed`：768 MB + 768 MB |

### 5.2 Android 端"只允许激进"的实现方案

1. **平台判定**：`QOperatingSystemVersion.currentType() == QOperatingSystemVersion.OSType.Android`（PySide6 已绑定：`current()`/`currentType()`/`type()` 与 `OSType.Android`，见 `QtCore.pyi:6218-6240` ✅）或 `sys.platform == "android"`（p4a 下通常成立）；
2. **加载即强制**：`Settings.load()` 后若为 Android → `memory_policy = "aggressive"`，并把强制后的值写回 `settings.json`（保证外部工具/后续版本读到一致值）；
3. **UI 收敛**：设置页在 Android 上把三档下拉替换为只读展示（例如一行"内存策略：激进（Android 固定）"），或保留控件但禁用非激进项；
4. **兜底断言**：`_apply_memory_policy()` 内若发现 Android 且策略 ≠ aggressive，强制按 aggressive 应用并记一条 warning 日志（防止旧配置文件绕过）；
5. **后台释放钩子**：`QGuiApplication.applicationStateChanged` → 进入 `Qt.ApplicationInactive`/`ApplicationSuspended` 时主动逐出非当前文档缓存与 bg 池（Android 在后台更容易被杀；回到前台时已有预加载机制补齐）；
6. **可调参数集中**：把"Android 上更激进的预算"（例如 LRU 256MB → 192MB、bg 池 2 张 → 1 张）定义在同一处常量表里，便于真机调优（这属于**数值调整**，需在 P2 依据实测决定，不建议现在就拍）。

### 5.3 风险与验收

| 风险 | 说明 | 验收/缓解 |
|------|------|-----------|
| 大 PSD 频繁重解码 | 激进档意味着窗口外图层更早被逐出，切页/切层可能重解码 | 真机用 15 页·大 PSD 压测：连续翻页 30 次，记录卡顿与重解码次数 |
| 后台被杀后状态 | Android 可能直接杀进程 | 依赖现有"任务文件自动保存 + 启动恢复"链路；确认自动保存频率在 Android 上够用 |
| 双档位语义混淆 | 桌面仍是三档 | 文档/设置页明确"Android 固定激进" |
| 峰值内存仍偏高 | Qt + numpy + Pillow 自身占用 | 用 `adb shell dumpsys meminfo <pkg>` 观察 PSS 峰值；必要时进一步下调 |

---

## 6. 实施计划（在本轮需求下更新）

| 阶段 | 内容 | 验收 |
|------|------|------|
| **P0 打包链路（APK only）** | 上游文档 §5 的 CI + 包装脚本；`android.release_artifact=apk`；`android.api=35`（targetSdk）、`minapi=30`；`android.manifest.orientation=sensorLandscape`、`fullscreen=1`、`p4a.extra_args` 追加 `--display-cutout shortEdges`；`MANAGE_EXTERNAL_STORAGE` 权限；启动底色主题；内存强制激进 | CI 出签名 APK；模拟器/真机安装后**横屏全屏**启动；设置页显示"激进（固定）"；未授权时弹窗（§2.7） |
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
| 7 | Android 内存预算 | **就用激进档**（LRU 256 MB + bg 池 68 MB），不再额外收紧 |
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
