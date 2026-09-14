"""程序级设置：settings.json（需求 §20.2、§30、§35、§55）。

软件级设置全部落在 程序目录/settings.json，与任务数据（.mangaproof.json）
彻底分离（需求 §58）。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mangaproof.config import paths

log = logging.getLogger("mangaproof.config.settings")

SETTINGS_VERSION = 1

# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------

DEFAULT_KEYBINDINGS: dict[str, str] = {
    "prev_psd": "Up",
    "next_psd": "Down",
    "prev_layer": "Left",
    "next_layer": "Right",
    "pass_layer": "Return",
    "fail_layer": "/",
    "toggle_compare": "Space",
    "cancel_operation": "Esc",
    "save_task": "Ctrl+S",
    "custom_comment": "Ctrl+Return",
    "open_psd": "Ctrl+O",
    "open_folder": "Ctrl+Shift+O",
    "generate_report": "Ctrl+R",
    "redraw_mode": "R",
}

# 预制问题类型（需求 §34）及其默认快捷键（需求 §35，均可配置）
DEFAULT_ISSUE_TYPES: list[dict[str, str]] = [
    {"name": "居中错误", "key": "1"},
    {"name": "字体选择错误", "key": "2"},
    {"name": "字体字重错误", "key": "3"},
    {"name": "字号错误", "key": "4"},
    {"name": "文字位置错误", "key": "5"},
    {"name": "文字间距错误", "key": "6"},
    {"name": "气泡处理错误", "key": "7"},
    {"name": "原文字擦除错误", "key": "8"},
    {"name": "背景擦除错误", "key": "9"},
    {"name": "网点对齐错误", "key": "0"},
    {"name": "网点残留", "key": "Q"},
    {"name": "修图瑕疵", "key": "W"},
    {"name": "漏翻", "key": "E"},
    {"name": "漏字", "key": "R"},
    {"name": "错字", "key": "T"},
    {"name": "翻译错误", "key": "Y"},
    {"name": "排版错误", "key": "U"},
    {"name": "文字溢出", "key": "I"},
    {"name": "其他", "key": "O"},
]

DISPLAY_RATIOS: list[float] = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
DEFAULT_DISPLAY_RATIO = 0.6

# 自动对比预设速度档位：(次/秒, 档位名)。默认"正常"= 4 次/秒，
# 即每张停留 250ms，与原硬编码行为一致（需求 §22）。
COMPARE_SPEED_TIERS: list[tuple[int, str]] = [
    (1, "慢"),
    (2, "较慢"),
    (4, "正常"),
    (5, "较快"),
    (8, "快"),
]
DEFAULT_COMPARE_SPEED_HZ = 4
DEFAULT_COMPARE_MODE = "auto"   # "auto" 自动切换 / "manual" 手动切换

# 裸滚轮（不按修饰键）行为：默认上下平移；可切换为缩放（需求 §27）。
# 触控板双指滚不受此设置影响，恒为双轴平移。
DEFAULT_WHEEL_MODE = "pan"      # "pan" 上下移动 / "zoom" 缩放

# 问题红框显示范围（需求 §39 Overlay 展示）：
# - "page"（默认）：始终显示当前 PSD（页）的全部问题——跨图层，一屏看全整页标注；
# - "layer"：只显示当前图层的问题（旧版本行为）。
ISSUE_SCOPES: tuple[str, ...] = ("page", "layer")
DEFAULT_ISSUE_SCOPE = "page"

# 返修单页面图像格式：
# - "png"（默认）：无损，体积大；
# - "jpeg"：有损压缩，体积显著变小（漫画页面常见网点/渐变），质量可调。
REPORT_IMAGE_FORMATS: tuple[str, ...] = ("png", "jpeg")
DEFAULT_REPORT_IMAGE_FORMAT = "png"
JPEG_QUALITY_CHOICES: tuple[int, ...] = (60, 70, 80, 90, 95)
DEFAULT_JPEG_QUALITY = 80

# 内存回收策略档位：宽松 / 平衡 / 激进。
# 各档预算（bg QImage 池字节上限、图层像素 LRU 字节上限）在
# mangaproof/ui/main_window.py 的 _MEMORY_POLICIES 中定义；
# 文档结构卸载（窗口外驱逐）三档一致。
MEMORY_POLICIES: tuple[str, ...] = ("relaxed", "balanced", "aggressive")
DEFAULT_MEMORY_POLICY = "balanced"


@dataclass
class Settings:
    """运行时设置对象。"""

    layer_display_ratio: float = DEFAULT_DISPLAY_RATIO
    compare_mode: str = DEFAULT_COMPARE_MODE   # "auto" / "manual"
    compare_speed_hz: int = DEFAULT_COMPARE_SPEED_HZ
    wheel_mode: str = DEFAULT_WHEEL_MODE       # "pan" / "zoom"
    issue_scope: str = DEFAULT_ISSUE_SCOPE     # "page"（当前页全部）/ "layer"（仅当前图层）
    recursive_scan: bool = False
    generate_pdf_on_complete: bool = True
    report_name: str = ""
    # 返修单页面图像：png（无损）/ jpeg（压缩，体积小）+ JPEG 质量
    report_image_format: str = DEFAULT_REPORT_IMAGE_FORMAT
    report_jpeg_quality: int = DEFAULT_JPEG_QUALITY
    # 返修单 PSD 总览表是否隐藏「全部通过且无问题」的页（默认隐藏）
    report_hide_clean_files: bool = True
    hide_console: bool = True   # 打包产物隐藏控制台（直接运行 py 时始终显示）
    keybindings: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_KEYBINDINGS))
    issue_types: list[dict[str, str]] = field(
        default_factory=lambda: [dict(t) for t in DEFAULT_ISSUE_TYPES]
    )
    custom_comment_key: str = "Ctrl+Return"
    recent_paths: list[str] = field(default_factory=list)
    # 内存回收策略：aggressive（激进）/ balanced（平衡）/ relaxed（宽松）
    memory_policy: str = DEFAULT_MEMORY_POLICY

    # -- 派生查询 ----------------------------------------------------------

    def issue_key_map(self) -> dict[str, str]:
        """问题类型名 -> 快捷键。"""
        return {t["name"]: t.get("key", "") for t in self.issue_types}

    def issue_type_names(self) -> list[str]:
        return [t["name"] for t in self.issue_types]

    def key_for_issue(self, name: str) -> str:
        return self.issue_key_map().get(name, "")

    def binding(self, action: str) -> str:
        return self.keybindings.get(action, DEFAULT_KEYBINDINGS.get(action, ""))


class SettingsManager:
    """settings.json 的读写封装。"""

    def __init__(self, path: Path | None = None):
        self._path = path if path is not None else paths.settings_path()
        self._lock = threading.Lock()
        self.settings = self._load()

    # -- 读写 --------------------------------------------------------------

    def _load(self) -> Settings:
        try:
            if not self._path.exists():
                return Settings()
            with open(self._path, "r", encoding="utf-8") as f:
                raw: dict[str, Any] = json.load(f)
            return self._from_dict(raw)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            log.warning("读取 settings.json 失败，使用默认设置：%s", exc)
            return Settings()

    def _from_dict(self, raw: dict[str, Any]) -> Settings:
        s = Settings()

        ratio = raw.get("layer_display_ratio", DEFAULT_DISPLAY_RATIO)
        try:
            ratio = float(ratio)
        except (TypeError, ValueError):
            ratio = DEFAULT_DISPLAY_RATIO
        if not (0.01 <= ratio <= 4.0):
            ratio = DEFAULT_DISPLAY_RATIO
        s.layer_display_ratio = ratio

        mode = raw.get("compare_mode", DEFAULT_COMPARE_MODE)
        s.compare_mode = mode if mode in ("auto", "manual") else DEFAULT_COMPARE_MODE

        try:
            hz = int(raw.get("compare_speed_hz", DEFAULT_COMPARE_SPEED_HZ))
        except (TypeError, ValueError):
            hz = DEFAULT_COMPARE_SPEED_HZ
        if not (1 <= hz <= 10):
            hz = DEFAULT_COMPARE_SPEED_HZ
        s.compare_speed_hz = hz

        wheel = raw.get("wheel_mode", DEFAULT_WHEEL_MODE)
        s.wheel_mode = wheel if wheel in ("pan", "zoom") else DEFAULT_WHEEL_MODE

        scope = raw.get("issue_scope", DEFAULT_ISSUE_SCOPE)
        s.issue_scope = scope if scope in ISSUE_SCOPES else DEFAULT_ISSUE_SCOPE

        s.recursive_scan = bool(raw.get("recursive_scan", False))
        s.generate_pdf_on_complete = bool(
            raw.get("generate_pdf_on_complete", True)
        )
        s.report_name = str(raw.get("report_name", "") or "")

        fmt = raw.get("report_image_format", DEFAULT_REPORT_IMAGE_FORMAT)
        s.report_image_format = (
            fmt if fmt in REPORT_IMAGE_FORMATS else DEFAULT_REPORT_IMAGE_FORMAT
        )
        try:
            quality = int(raw.get("report_jpeg_quality", DEFAULT_JPEG_QUALITY))
        except (TypeError, ValueError):
            quality = DEFAULT_JPEG_QUALITY
        if not (60 <= quality <= 95):
            quality = DEFAULT_JPEG_QUALITY
        s.report_jpeg_quality = quality
        s.report_hide_clean_files = bool(raw.get("report_hide_clean_files", True))
        s.hide_console = bool(raw.get("hide_console", True))
        s.custom_comment_key = str(
            raw.get("custom_comment_key", DEFAULT_KEYBINDINGS["custom_comment"])
        )

        kb = raw.get("keybindings", {})
        if isinstance(kb, dict):
            merged = dict(DEFAULT_KEYBINDINGS)
            for k, v in kb.items():
                if isinstance(v, str) and v.strip():
                    merged[k] = v.strip()
            s.keybindings = merged

        types = raw.get("issue_types")
        if isinstance(types, list) and types:
            cleaned = []
            seen = set()
            for item in types:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                cleaned.append({"name": name, "key": str(item.get("key", ""))})
            if cleaned:
                s.issue_types = cleaned

        recent = raw.get("recent_paths", [])
        if isinstance(recent, list):
            s.recent_paths = [str(p) for p in recent if isinstance(p, str)][:10]

        policy = raw.get("memory_policy", DEFAULT_MEMORY_POLICY)
        s.memory_policy = policy if policy in MEMORY_POLICIES else DEFAULT_MEMORY_POLICY

        return s

    def save(self) -> None:
        with self._lock:
            try:
                payload = {
                    "settings_version": SETTINGS_VERSION,
                    "layer_display_ratio": self.settings.layer_display_ratio,
                    "compare_mode": self.settings.compare_mode,
                    "compare_speed_hz": self.settings.compare_speed_hz,
                    "wheel_mode": self.settings.wheel_mode,
                    "issue_scope": self.settings.issue_scope,
                    "recursive_scan": self.settings.recursive_scan,
                    "generate_pdf_on_complete": self.settings.generate_pdf_on_complete,
                    "report_name": self.settings.report_name,
                    "report_image_format": self.settings.report_image_format,
                    "report_jpeg_quality": self.settings.report_jpeg_quality,
                    "report_hide_clean_files": self.settings.report_hide_clean_files,
                    "hide_console": self.settings.hide_console,
                    "custom_comment_key": self.settings.custom_comment_key,
                    "keybindings": self.settings.keybindings,
                    "issue_types": self.settings.issue_types,
                    "recent_paths": self.settings.recent_paths,
                    "memory_policy": self.settings.memory_policy,
                }
                tmp = self._path.with_suffix(".json.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                tmp.replace(self._path)
            except OSError as exc:
                log.warning("写入 settings.json 失败：%s", exc)

    def add_recent(self, path_str: str) -> None:
        recent = self.settings.recent_paths
        if path_str in recent:
            recent.remove(path_str)
        recent.insert(0, path_str)
        del recent[10:]
        self.settings.recent_paths = recent
        self.save()
