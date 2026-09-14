"""视觉中心定位（需求 §17、§18）。

选择图层后，将图层「视觉中心」（非 Bounds 几何中心）移动到视口中心。

- 有 Alpha 的图层：alpha > threshold（默认 0）的像素计算 Visual Bounding Box；
- 完全透明：使用 Layer Bounds fallback；
- Bounds 不可用：保持当前 Camera（由调用方处理）。
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

import numpy as np

from mangaproof.psd.layer_model import LayerInfo, compute_visual_bounds

log = logging.getLogger("mangaproof.camera.centering")

# 自动框选：在视觉边界（画布上的蓝色虚线框）基础上，上下左右各外扩的像素数。
AUTO_BOX_MARGIN = 5.0


def layer_visual_bounds(info: LayerInfo, alpha_threshold: float = 0.0) -> Optional[Tuple[int, int, int, int]]:
    """返回图层视觉边界（世界坐标）。

    图层的裁剪像素相对其 bbox 原点，需把结果平移到世界坐标。
    """
    vb = info.visual_bounds(alpha_threshold=alpha_threshold)
    if vb is None:
        return None
    left, top, right, bottom = vb
    ox, oy = info.bounds[0], info.bounds[1]
    return (left + ox, top + oy, right + ox, bottom + oy)


def layer_visual_center(info: LayerInfo, alpha_threshold: float = 0.0) -> Optional[Tuple[float, float]]:
    """视觉中心（世界坐标）。"""
    vb = layer_visual_bounds(info, alpha_threshold)
    if vb is None:
        return None
    return ((vb[0] + vb[2]) / 2.0, (vb[1] + vb[3]) / 2.0)


def compute_center_target(info: LayerInfo, alpha_threshold: float = 0.0) -> Tuple[float, float]:
    """定位目标点（世界坐标），带 Bounds fallback（需求 §18.1）。"""
    center = layer_visual_center(info, alpha_threshold)
    if center is not None:
        return center
    if info.width > 0 and info.height > 0:
        log.info("图层 %s 无有效像素，使用 Layer Bounds 中心", info.id)
        return info.center
    # Bounds 也不可用 → 保持当前 Camera（调用方不移动相机即可）
    return info.center


def auto_box_rect(
    info: LayerInfo,
    margin: float = AUTO_BOX_MARGIN,
    alpha_threshold: float = 0.0,
) -> Optional[Tuple[float, float, float, float]]:
    """自动框选矩形（世界坐标 x, y, w, h）。

    以图层视觉边界（画布上那个蓝色虚线框）为准，上下左右**各外扩 margin
    像素**——对称外扩，所以中心与虚线框中心完全一致。

    视觉边界为空（整层透明）时回退到 Layer Bounds；两者都不可用返回 None
    （调用方不生成红框）。
    """
    bounds = layer_visual_bounds(info, alpha_threshold)
    if bounds is None:
        left, top, right, bottom = info.bounds
        if right <= left or bottom <= top:
            return None
        bounds = (float(left), float(top), float(right), float(bottom))
    left, top, right, bottom = (float(v) for v in bounds)
    return (
        left - margin,
        top - margin,
        (right - left) + 2 * margin,
        (bottom - top) + 2 * margin,
    )
