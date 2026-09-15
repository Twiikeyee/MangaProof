package com.mangaproof.picker;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.provider.DocumentsContract;

import org.qtproject.qt.android.bindings.QtActivity;

import java.io.File;
import java.io.FileOutputStream;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;

/**
 * MangaProof：Android 原生 SAF 选择器（文件 / 文件夹）+ 与 Python 的**文件协议**通道。
 *
 * <h3>为什么要自己拉选择器，而不用 Qt 自带的原生文件对话框</h3>
 * Qt 6.11.2 的 {@code QAndroidPlatformFileDialogHelper} 存在**同线程重入死锁**：
 * 系统选择器返回后，Java 在主线程同步回调
 * {@code QtAndroidPrivate::handleActivityResult()}，该函数**持有非递归 QMutex**
 * 逐个调用监听者；监听者发出的 {@code accept()} / {@code reject()} 经 Qt 直连信号
 * 走到 {@code QDialog::done()} → {@code hide()} →
 * {@code QAndroidPlatformFileDialogHelper::hide()} →
 * {@code unregisterActivityResultListener()} → **再次获取同一把 QMutex** → 主线程
 * 自锁死。表现就是**选文件、选文件夹、取消，三种操作都会让界面永久卡死**。
 * 因此本应用在 Android 上完全不走 {@code QFileDialog}。
 *
 * <h3>为什么用文件协议而不是 JNI</h3>
 * PySide6 的 Android wheel **不向 Python 暴露任何 JNI 绑定**：实测
 * {@code PySide6/QtCore.abi3.so} 与 {@code libpyside6.abi3.so} 里
 * {@code QJniObject}/{@code QJniEnvironment}/{@code QtAndroidPrivate}/
 * {@code QApplication.getJniType} 的命中数全部为 **0**（wheel 里出现的
 * {@code QJniObject} 符号来自 Qt 自己的 C++ 库 {@code libQt6Core_arm64-v8a.so}），
 * 也没有 CPython 的 {@code java}/{@code _jni} 模块。
 * 结论：**Python 无法调用任何 Java 方法**，只能通过进程内的"共享文件"通信。
 *
 * <h3>协议（目录：{@code <app>/files/picker/}）</h3>
 * <pre>
 * Python → Java ：写 cmd.tmp 后 rename 成 cmd.txt（原子），内容一行：
 *                   FOLDER
 *                   FILE
 * Java  → Python：写 result.tmp 后 rename 成 result.txt（原子），内容一行：
 *                   OK\t&lt;真实路径&gt;\t&lt;uri&gt;
 *                   CANCEL
 *                   ERROR\t&lt;原因&gt;
 * Python 读到结果后删除 result.txt；Java 处理完命令后删除 cmd.txt。
 * </pre>
 * 原子 rename 保证双方**永远不会读到半截文件**。Python 侧用 QTimer 轮询结果并带
 * 超时（见 {@code mangaproof/storage/android_picker.py}），因此**即使 Java 侧完全
 * 没有响应，界面也只是恢复原状，不会卡死**。
 *
 * <h3>线程</h3>
 * · 命令消费跑在 {@link #ensureConsumerStarted} 启动的**守护线程**上（不碰 UI）；
 * · 拉选择器与处理结果都在 **Android 主线程**（Qt GUI 线程 = 主线程）；
 * · 该守护线程只读一个应用私有文件并 sleep，开销可忽略。
 *
 * <h3>入口 Activity</h3>
 * p4a 的 Qt 模板把入口写成 {@code org.qtproject.qt.android.bindings.QtActivity}；
 * 本类继承它并由 p4a hook 替换清单里的入口名 —— **只有启动选择器的 Activity 才能
 * 收到它的结果**，而父类的 {@code onActivityResult} 会把所有 request code 转给 Qt
 * （未知 code 被丢弃），所以必须在这里截获自己的 code。其它 code 一律 {@code super}
 * 交回 Qt，行为完全不变。
 */
public class PickerActivity extends QtActivity {

    public static final String TAG = "MangaProofPicker";

    /** 本类专用 request code（与 Qt 内部的 1305 等互不冲突）。 */
    private static final int REQUEST_PICK = 0x4D50;   // 'MP'

    private static final String ACTION_OPEN_DOCUMENT = "android.intent.action.OPEN_DOCUMENT";
    private static final String ACTION_OPEN_DOCUMENT_TREE = "android.intent.action.OPEN_DOCUMENT_TREE";
    private static final String EXTRA_INITIAL_URI = "android.provider.extra.INITIAL_URI";
    private static final String EXTRA_MIME_TYPES = "android.intent.extra.MIME_TYPES";
    private static final String EXTRA_ALLOW_MULTIPLE = "android.intent.extra.ALLOW_MULTIPLE";

    // ---- 协议常量（必须与 mangaproof/storage/android_picker.py 完全一致） ----
    private static final String DIR_NAME = "picker";
    private static final String CMD_FILE = "cmd.txt";
    private static final String CMD_TMP = "cmd.tmp";
    private static final String RESULT_FILE = "result.txt";
    private static final String RESULT_TMP = "result.tmp";
    private static final String CMD_FOLDER = "FOLDER";
    private static final String CMD_FILE_PICK = "FILE";

    /** 轮询间隔（毫秒）：只读应用私有目录里的一个小文件。 */
    private static final long POLL_INTERVAL_MS = 250L;

    /** 命令是否已经处理过（防止同一个 cmd.txt 被重复消费）。 */
    private static String sHandledCommand = null;
    /** 是否有一次选择正在进行（用户还在系统选择器里）。 */
    private static volatile boolean sPickInFlight = false;

    // ------------------------------------------------------------------ Python 侧入口

    /** 结果目录：{@code /data/data/<pkg>/files/picker}（应用私有，无需任何权限）。 */
    public static String resultDirPath(Context context) {
        return new File(context.getFilesDir(), DIR_NAME).getAbsolutePath();
    }

    // ------------------------------------------------------------------ 启动守护线程

    /**
     * 启动命令消费守护线程（幂等）。
     *
     * 由 {@code A11yEnvProvider.onCreate()} 调用 —— 那是应用进程里**最早**能跑我们
     * Java 代码的地方（早于任何 Activity），且能拿到 Context。
     */
    public static synchronized void ensureConsumerStarted(final Context context) {
        final File dir = new File(context.getFilesDir(), DIR_NAME);
        if (!dir.isDirectory() && !dir.mkdirs()) {
            android.util.Log.w(TAG, "无法创建选择器目录：" + dir);
            return;
        }
        // 上一次进程留下的残留命令/结果一律清掉，避免启动即触发一次"幽灵选择"
        deleteQuietly(new File(dir, CMD_FILE));
        deleteQuietly(new File(dir, RESULT_FILE));

        Thread worker = new Thread(new Runnable() {
            @Override
            public void run() {
                android.util.Log.i(TAG, "命令消费线程已启动，目录=" + dir);
                while (true) {
                    try {
                        String cmd = readCommand(dir);
                        if (cmd != null) {
                            handleCommand(context, dir, cmd);
                        }
                        Thread.sleep(POLL_INTERVAL_MS);
                    } catch (InterruptedException ie) {
                        return;
                    } catch (Throwable t) {
                        android.util.Log.w(TAG, "命令消费循环异常（继续运行）：" + t);
                        try {
                            Thread.sleep(POLL_INTERVAL_MS);
                        } catch (InterruptedException ie) {
                            return;
                        }
                    }
                }
            }
        }, "MangaProofPicker");
        worker.setDaemon(true);
        worker.start();
    }

    /** 读取并**消费**命令（读到即删除，同一条命令只处理一次）。 */
    private static String readCommand(File dir) {
        File cmd = new File(dir, CMD_FILE);
        if (!cmd.isFile()) {
            return null;
        }
        String text;
        try {
            text = readAll(cmd).trim();
        } catch (Throwable t) {
            android.util.Log.w(TAG, "读取命令失败：" + t);
            deleteQuietly(cmd);
            return null;
        }
        deleteQuietly(cmd);
        if (text.isEmpty() || text.equals(sHandledCommand)) {
            return null;
        }
        sHandledCommand = text;
        return text;
    }

    private static void handleCommand(final Context context, final File dir, final String cmd) {
        if (sPickInFlight) {
            android.util.Log.i(TAG, "已有一次选择正在进行，忽略命令：" + cmd);
            return;
        }
        sPickInFlight = true;
        android.util.Log.i(TAG, "收到命令：" + cmd);
        // cmd 形如 "FOLDER <token>" / "FILE <token>"，取第一段做类型判断
        final String kind = cmd.split("\\s+", 2)[0];
        // 必须在主线程 startActivityForResult
        new android.os.Handler(android.os.Looper.getMainLooper()).post(new Runnable() {
            @Override
            public void run() {
                try {
                    Activity activity = currentActivity();
                    if (activity == null) {
                        finishPick(dir, "ERROR\t应用尚未就绪（没有 Activity）");
                        return;
                    }
                    Intent intent;
                    if (CMD_FOLDER.equalsIgnoreCase(kind)) {
                        intent = new Intent(ACTION_OPEN_DOCUMENT_TREE);
                        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                                | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                                | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION
                                | Intent.FLAG_GRANT_PREFIX_URI_PERMISSION);
                    } else {
                        intent = new Intent(ACTION_OPEN_DOCUMENT);
                        intent.addCategory(Intent.CATEGORY_OPENABLE);
                        intent.setType("*/*");
                        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                                | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
                        intent.putExtra(EXTRA_ALLOW_MULTIPLE, false);
                        // PSD/PSB：这些 MIME 在部分设备上不是每种都被系统认，因此同时
                        // 放进 EXTRA_MIME_TYPES（系统会取并集，认不出就退回 */*）
                        intent.putExtra(EXTRA_MIME_TYPES, new String[]{
                                "image/vnd.adobe.photoshop", "image/x-photoshop",
                                "application/x-photoshop", "application/photoshop",
                                "image/psd", "image/vnd.adobe.photoshop+psb",
                        });
                    }
                    activity.startActivityForResult(intent, REQUEST_PICK);
                } catch (Throwable t) {
                    android.util.Log.w(TAG, "打开系统选择器失败：" + t);
                    finishPick(dir, "ERROR\t无法打开系统选择器：" + t.getMessage());
                }
            }
        });
    }

    // ------------------------------------------------------------------ 结果

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode != REQUEST_PICK) {
            super.onActivityResult(requestCode, resultCode, data);   // Qt 自己的码，原样交回
            return;
        }
        // 结果目录：Activity 的 files 目录与 provider 看到的是同一个
        final File dir = new File(getFilesDir(), DIR_NAME);
        try {
            if (resultCode != Activity.RESULT_OK || data == null || data.getData() == null) {
                finishPick(dir, "CANCEL");
                return;
            }
            Uri uri = data.getData();
            takePersistablePermission(uri);
            String realPath = resolveRealPath(uri);
            android.util.Log.i(TAG, "选择结果 uri=" + uri + " realPath=" + realPath);
            if (realPath == null || realPath.isEmpty()) {
                finishPick(dir, "ERROR\t该位置没有对应的文件路径（云盘 / 媒体库等），"
                        + "请改选「本机存储」里的文件夹");
            } else {
                finishPick(dir, "OK\t" + realPath + "\t" + uri);
            }
        } catch (Throwable t) {
            android.util.Log.w(TAG, "处理选择结果异常：" + t);
            finishPick(dir, "ERROR\t处理选择结果失败：" + t.getMessage());
        }
    }

    /** 原子写出结果并解除"进行中"标记。 */
    private static synchronized void finishPick(File dir, String line) {
        try {
            writeAtomic(dir, RESULT_TMP, RESULT_FILE, line);
        } catch (Throwable t) {
            android.util.Log.w(TAG, "写出选择结果失败：" + t);
        } finally {
            sPickInFlight = false;
        }
    }

    /**
     * Activity 销毁时兜底。
     *
     * 若销毁发生在选择器打开期间（进程没死、Activity 被重建），结果将永远不会回到
     * 这个实例，Python 侧会一直等到超时。这里主动写一条 ERROR，让 Python 立刻恢复
     * 并给出可读提示，而不是让用户干等 10 分钟。
     *
     * 注意：`onDestroy()` 在正常退出流程里也会走到，但那时 Python 侧并没有等待中的
     * 请求，写进结果文件的内容会在下次启动时被 {@link #ensureConsumerStarted} 清掉。
     */
    @Override
    protected void onDestroy() {
        try {
            if (sPickInFlight) {
                finishPick(new File(getFilesDir(), DIR_NAME),
                        "ERROR\t选择器中断（界面被系统重建），请重试");
            }
        } catch (Throwable t) {
            android.util.Log.w(TAG, "onDestroy 兜底写结果失败：" + t);
        }
        super.onDestroy();
    }

    // ------------------------------------------------------------------ SAF → 真实路径

    /** 由 Python 侧诊断使用（也可独立验证映射逻辑）。 */
    public static String resolveRealPathString(String uriString) {
        try {
            return resolveRealPath(Uri.parse(uriString));
        } catch (Throwable t) {
            android.util.Log.w(TAG, "解析真实路径失败：" + t);
            return null;
        }
    }

    /**
     * SAF URI → 真实路径；无法映射时返回 null。
     *
     * 覆盖两种提供方：
     * · {@code com.android.externalstorage.documents}（本机存储 / SD 卡）：
     *   documentId 形如 {@code primary:Download/manga} 或 {@code XXXX-XXXX:dir}
     *   → {@code /storage/emulated/0/Download/manga} / {@code /storage/XXXX-XXXX/dir}；
     * · {@code com.android.providers.downloads.documents}（下载）：
     *   {@code raw:/storage/…} 直接可用；{@code msf:…} 需要查库，此处不支持。
     *
     * 云盘 / 媒体库等提供方没有真实路径 → 返回 null，由 UI 明确提示（本应用按既定
     * 方案是"全文件访问 + 真实路径直读"，不做复制到应用私有目录的兜底导入）。
     */
    private static String resolveRealPath(Uri uri) {
        if (uri == null) {
            return null;
        }
        String docId = null;
        try {
            docId = DocumentsContract.isTreeUri(uri)
                    ? DocumentsContract.getTreeDocumentId(uri)
                    : DocumentsContract.getDocumentId(uri);
        } catch (Throwable t) {
            android.util.Log.i(TAG, "读取 documentId 失败：" + t);
        }
        final String authority = uri.getAuthority();
        android.util.Log.i(TAG, "SAF authority=" + authority + " documentId=" + docId);
        if (docId == null || docId.isEmpty()) {
            return null;
        }
        if ("com.android.externalstorage.documents".equals(authority)) {
            return fromExternalStorageDocId(docId);
        }
        if ("com.android.providers.downloads.documents".equals(authority)) {
            if (docId.startsWith("raw:")) {
                return docId.substring("raw:".length());
            }
            return null;
        }
        return null;
    }

    /** {@code primary:Download/manga} ⇒ {@code /storage/emulated/0/Download/manga}。 */
    private static String fromExternalStorageDocId(String docId) {
        int sep = docId.indexOf(':');
        if (sep <= 0) {
            return null;
        }
        final String volume = docId.substring(0, sep);
        final String relative = docId.substring(sep + 1);
        String base;
        if ("primary".equalsIgnoreCase(volume)) {
            base = "/storage/emulated/0";
        } else if ("home".equalsIgnoreCase(volume)) {
            base = "/storage/emulated/0/Documents";
        } else {
            base = "/storage/" + volume;      // SD 卡 / U 盘
        }
        return relative.isEmpty() ? base : base + "/" + relative;
    }

    /** 尽力留下持久化读权限（应用重启后仍可访问；失败不影响本次使用）。 */
    private static void takePersistablePermission(Uri uri) {
        try {
            Activity activity = currentActivity();
            if (activity == null) {
                return;
            }
            activity.getContentResolver().takePersistableUriPermission(
                    uri, Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
        } catch (Throwable t) {
            android.util.Log.i(TAG, "未取得持久化授权（可忽略）：" + t);
        }
    }

    /**
     * 取当前 Activity。
     *
     * 为什么用反射：本版本 Qt 的 `QtNative` **所有方法都是包级可见**（`javap` 实测：
     * `activity()` / `getContext()` / `runAction()` 都没有 public，连 native 方法也是），
     * 跨包直接调用会被 javac 拒绝（本地用 android.jar + Qt 的 jar 编译时实测如此）。
     * 因此改为读它的私有静态字段 `m_activity`（`WeakReference<Activity>`）——字段名
     * 稳定，且我们锁定 Qt 6.11.2；取不到就返回 null，由调用方回一条可读错误给
     * Python，而不是抛异常或崩溃。
     */
    private static Activity currentActivity() {
        try {
            java.lang.reflect.Field field =
                    org.qtproject.qt.android.QtNative.class.getDeclaredField("m_activity");
            field.setAccessible(true);
            Object holder = field.get(null);
            if (holder instanceof java.lang.ref.WeakReference) {
                Object activity = ((java.lang.ref.WeakReference<?>) holder).get();
                if (activity instanceof Activity) {
                    return (Activity) activity;
                }
                android.util.Log.i(TAG, "当前没有存活的 Activity（可能正在重建）");
                return null;
            }
            android.util.Log.w(TAG, "QtNative.m_activity 类型不符：" + holder);
        } catch (Throwable t) {
            android.util.Log.w(TAG, "反射读取当前 Activity 失败：" + t);
        }
        return null;
    }

    // ------------------------------------------------------------------ 文件小工具

    private static String readAll(File file) throws Exception {
        RandomAccessFile raf = new RandomAccessFile(file, "r");
        try {
            byte[] buf = new byte[(int) raf.length()];
            raf.readFully(buf);
            return new String(buf, StandardCharsets.UTF_8);
        } finally {
            raf.close();
        }
    }

    /** 先写临时文件再 rename —— 双方都不会读到半截内容。 */
    private static void writeAtomic(File dir, String tmpName, String finalName, String text)
            throws Exception {
        File tmp = new File(dir, tmpName);
        FileOutputStream out = new FileOutputStream(tmp);
        try {
            out.write((text + "\n").getBytes(StandardCharsets.UTF_8));
            out.flush();
            out.getFD().sync();
        } finally {
            out.close();
        }
        File dest = new File(dir, finalName);
        deleteQuietly(dest);
        if (!tmp.renameTo(dest)) {
            throw new Exception("rename 失败：" + tmp + " → " + dest);
        }
    }

    private static void deleteQuietly(File file) {
        try {
            if (file != null && file.exists()) {
                file.delete();
            }
        } catch (Throwable ignored) {
            // 删除失败无需处理
        }
    }
}
