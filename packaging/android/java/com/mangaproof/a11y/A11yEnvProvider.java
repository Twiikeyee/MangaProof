package com.mangaproof.a11y;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.system.Os;

/**
 * MangaProof：在 Android 上告知系统「本应用不参与辅助功能」。
 *
 * 背景
 * ----
 * Qt 用一块 Surface 画整个界面，Android 的读屏无法从中读出结构，于是每次查询都会
 * 经 JNI 回到 Qt（`androidjniaccessibility.cpp` 的 `runInObjectContext()`），而那里是
 * `Qt::BlockingQueuedConnection` 打到 Qt 主线程。某些系统（如 HyperOS）的辅助功能
 * 在应用启动、Qt 主线程正在创建窗口时并发发起查询，就会撞上 Qt 的
 * `AndroidDeadlockProtector` 保护逻辑。
 *
 * Qt 自带官方开关
 * --------------
 * `QtAccessibilityDelegate.java` 的 `onAccessibilityStateChanged()` 里有：
 *
 *     final String isA11yOff = Os.getenv("QT_ANDROID_DISABLE_ACCESSIBILITY");
 *     if (isA11yOff != null && (isA11yOff.equalsIgnoreCase("true") || isA11yOff.equals("1")))
 *         return;
 *
 * 命中即 **直接返回**：不再创建那个覆盖在 Qt 布局上的 View、不注册无障碍代理，
 * 系统根本不会来问 Qt —— 既解决了死锁，又完全没有触碰 Qt 的死锁保护器。
 *
 * 为什么用 ContentProvider
 * ----------------------
 * 该环境变量由 **Java 侧**在「无障碍状态变化」时读取，而状态监听是在 Activity /
 * QtLayout 初始化时就注册并可能立即触发的，所以必须在**任何 Activity 之前**写入。
 * Android 的生命周期保证了这一点：
 *
 *     Application.attachBaseContext → **ContentProvider.onCreate** → Application.onCreate
 *     → Activity.onCreate
 *
 * ContentProvider 在 `ActivityThread.handleBindApplication()` 里由
 * `installContentProviders()` 安装，早于任何 Activity；本类只做一件事——写环境变量。
 * 这样我们不需要替换 `<application android:name>`（避免与 Qt 的 QtApplication 冲突），
 * 也不需要依赖 Qt 的 jar 参与编译（本类只用 framework API）。
 *
 * 其它实现细节
 * ----------
 * · `android.system.Os.setenv/getenv` 均为公开 API（API 21+），作用在**同一进程**的
 *   environ 表上，Qt 的 Java 代码用 `Os.getenv` 读得到；
 * · `Os.setenv` 只是改本进程内存中的环境变量，不涉及权限，也不会影响系统状态；
 * · provider 设 `android:exported="false"`，仅本进程使用；除 onCreate 外的回调
 *   （query/insert/update/delete/getType）永远不会被调用，全部返回空实现。
 */
public class A11yEnvProvider extends ContentProvider {

    /** 与 Qt `QtAccessibilityDelegate` 约定的开关名。 */
    private static final String KEY_DISABLE_ACCESSIBILITY = "QT_ANDROID_DISABLE_ACCESSIBILITY";

    /**
     * 设备最小宽度（dp）的环境变量名，供 Python 侧决定「界面缩放」的默认值。
     *
     * 为什么必须在这里（Java）算：界面缩放要在 **QApplication 创建之前**写成
     * `QT_SCALE_FACTOR`（Qt 只在启动时读一次），那时 Python 侧拿不到屏幕信息
     * （PySide6 没有 QJniObject，QScreen 也还不存在）。ContentProvider 运行在
     * 任何 Activity 之前、且在同一个进程里，用 DisplayMetrics 算最小宽度 dp
     * （= min(宽,高) ÷ density，即 Android 自己的 sw600dp 口径）最合适。
     */
    private static final String KEY_SW_DP = "MANGAPROOF_SW_DP";

    @Override
    public boolean onCreate() {
        try {
            Os.setenv(KEY_DISABLE_ACCESSIBILITY, "1", true);
            // 留一条日志，便于真机/客服排查：adb logcat | grep MangaProofA11y
            android.util.Log.i("MangaProofA11y",
                    KEY_DISABLE_ACCESSIBILITY + "=1 已设置（本应用不参与系统辅助功能）");
        } catch (Throwable t) {
            // 绝不能因为这一步失败而影响启动：失败了只是回到 Qt 默认行为。
            android.util.Log.w("MangaProofA11y",
                    "无法设置 " + KEY_DISABLE_ACCESSIBILITY + "，Qt 无障碍桥将保持默认行为: " + t);
        }
        reportSmallestWidthDp();
        return true;
    }

    /** 把当前显示的最小宽度（dp）写进进程环境，供 Python 侧按机型决定默认缩放。 */
    private void reportSmallestWidthDp() {
        try {
            android.util.DisplayMetrics dm = getContext().getResources().getDisplayMetrics();
            int swDp = Math.round(Math.min(dm.widthPixels, dm.heightPixels) / dm.density);
            Os.setenv(KEY_SW_DP, Integer.toString(swDp), true);
            android.util.Log.i("MangaProofA11y", KEY_SW_DP + "=" + swDp
                    + "（宽高 " + dm.widthPixels + "x" + dm.heightPixels
                    + "，density=" + dm.density + "）");
        } catch (Throwable t) {
            // 失败不影响启动：Python 侧会退回"未知机型 → 平板/折叠屏默认值"。
            android.util.Log.w("MangaProofA11y", "无法计算最小宽度 dp: " + t);
        }
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection,
                        String[] selectionArgs, String sortOrder) {
        return null;
    }

    @Override
    public String getType(Uri uri) {
        return null;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        return 0;
    }
}
