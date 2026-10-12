"""AI 3D Forge —— 生成应用图标（build/AI-3D-Forge.ico）。

为什么要自己画图标：安装包、桌面快捷方式、开始菜单、任务栏、控制面板卸载项
都会用到它。没有图标的软件在用户眼里就是"半成品"，而且 Windows 会用默认图标，
在一堆快捷方式里根本找不到。

设计（与软件内的设计系统一致）：
  * 近黑 #111111 的等轴测立方体轮廓 —— 对应界面主色，也直观表达"3D 模型"
  * 小米橙 #FF6A00 的实体面 —— 界面强调色，点缀而非大面积
  * 圆角方形底 #FFFFFF / #F7F7F8，与界面卡片一致
  * 只画一个立方体：16×16 下仍能辨认，不做细碎装饰

只依赖标准库：手写 PNG（zlib + struct），再组装成多尺寸 ICO。
尺寸覆盖 16/24/32/48/64/128/256 —— 任务栏、资源管理器、Alt+Tab 各取所需。

用法：
    python scripts/make_icon.py
    python scripts/make_icon.py --preview   # 额外输出各尺寸 PNG 便于人眼检查
"""

from __future__ import annotations

import console_utf8  # noqa: F401,E402 —— 必须最先导入：GBK 控制台下 print 中文/符号会弄崩脚本（见 src/aiforge/stdio.py）
import argparse
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_ICO = ROOT / "build" / "AI-3D-Forge.ico"
PREVIEW_DIR = ROOT / "build" / "work" / "icon-preview"

# 设计令牌（与 webui/src/styles/tokens.css 保持一致）
BG = (255, 255, 255)
BG_SOFT = (247, 247, 248)
INK = (17, 17, 17)
BRAND = (255, 106, 0)
BRAND_DARK = (214, 88, 0)

SIZES = (16, 24, 32, 48, 64, 128, 256)

# 超采样倍率：先按 4 倍画再缩小，得到抗锯齿边缘（标准库没有绘图库，
# 这是最简单可靠的画质来源）。
SS = 4


class Canvas:
    """极简 RGBA 画布：只需要填充多边形与圆角矩形。"""

    def __init__(self, size: int) -> None:
        self.size = size
        self.pixels = bytearray(size * size * 4)

    def set(self, x: int, y: int, color: tuple[int, int, int], alpha: int = 255) -> None:
        if 0 <= x < self.size and 0 <= y < self.size:
            offset = (y * self.size + x) * 4
            existing = self.pixels[offset + 3]
            if alpha >= 255 or existing == 0:
                self.pixels[offset : offset + 4] = bytes((*color, alpha))
                return
            # 简单 alpha 合成（图标内部重叠很少，够用）
            ratio = alpha / 255.0
            for channel in range(3):
                base = self.pixels[offset + channel]
                self.pixels[offset + channel] = int(base * (1 - ratio) + color[channel] * ratio + 0.5)
            self.pixels[offset + 3] = min(255, existing + alpha)

    def rounded_rect(self, x0: float, y0: float, x1: float, y1: float, radius: float,
                     color: tuple[int, int, int]) -> None:
        r2 = radius * radius
        for y in range(int(y0), int(y1) + 1):
            for x in range(int(x0), int(x1) + 1):
                dx = 0.0
                dy = 0.0
                if x < x0 + radius:
                    dx = x0 + radius - x
                elif x > x1 - radius:
                    dx = x - (x1 - radius)
                if y < y0 + radius:
                    dy = y0 + radius - y
                elif y > y1 - radius:
                    dy = y - (y1 - radius)
                if dx * dx + dy * dy <= r2:
                    self.set(x, y, color)

    def polygon(self, points: list[tuple[float, float]], color: tuple[int, int, int]) -> None:
        """扫描线填充凸/凹多边形。"""
        if len(points) < 3:
            return
        ys = [p[1] for p in points]
        y_min = max(0, int(min(ys)))
        y_max = min(self.size - 1, int(max(ys)) + 1)
        for y in range(y_min, y_max + 1):
            scan = y + 0.5
            crossings: list[float] = []
            for index in range(len(points)):
                x1, y1 = points[index]
                x2, y2 = points[(index + 1) % len(points)]
                if (y1 <= scan < y2) or (y2 <= scan < y1):
                    t = (scan - y1) / (y2 - y1)
                    crossings.append(x1 + t * (x2 - x1))
            crossings.sort()
            for index in range(0, len(crossings) - 1, 2):
                x_start = max(0, int(crossings[index] + 0.5))
                x_end = min(self.size - 1, int(crossings[index + 1] - 0.5) + 1)
                for x in range(x_start, x_end + 1):
                    self.set(x, y, color)

    def polyline(self, points: list[tuple[float, float]], color: tuple[int, int, int],
                 width: float) -> None:
        """按给定宽度画折线（用一串小方块近似圆头笔触）。"""
        half = width / 2.0
        for index in range(len(points) - 1):
            x1, y1 = points[index]
            x2, y2 = points[index + 1]
            length = max(1.0, ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5)
            steps = int(length * 2) + 1
            for step in range(steps + 1):
                t = step / steps
                cx = x1 + (x2 - x1) * t
                cy = y1 + (y2 - y1) * t
                for dy in range(int(-half), int(half) + 1):
                    for dx in range(int(-half), int(half) + 1):
                        if dx * dx + dy * dy <= half * half + half:
                            self.set(int(cx + dx), int(cy + dy), color)

    def downsample(self, factor: int) -> "Canvas":
        """按 factor 倍缩小并做盒式平均 —— 这就是抗锯齿。"""
        size = self.size // factor
        out = Canvas(size)
        area = factor * factor
        for y in range(size):
            for x in range(size):
                r = g = b = a = 0
                for sy in range(factor):
                    for sx in range(factor):
                        offset = ((y * factor + sy) * self.size + (x * factor + sx)) * 4
                        r += self.pixels[offset]
                        g += self.pixels[offset + 1]
                        b += self.pixels[offset + 2]
                        a += self.pixels[offset + 3]
                out.pixels[(y * size + x) * 4 : (y * size + x) * 4 + 4] = bytes(
                    (r // area, g // area, b // area, a // area)
                )
        return out

    def to_png(self) -> bytes:
        raw = bytearray()
        for y in range(self.size):
            raw.append(0)  # filter type 0
            raw.extend(self.pixels[y * self.size * 4 : (y + 1) * self.size * 4])
        compressed = zlib.compress(bytes(raw), 9)

        def chunk(tag: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + tag
                + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
            )

        header = struct.pack(">IIBBBBB", self.size, self.size, 8, 6, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", compressed)
            + chunk(b"IEND", b"")
        )


def draw_icon(size: int) -> Canvas:
    """在 size×size 上作画（内部按 SS 倍超采样）。"""
    canvas = Canvas(size * SS)
    unit = size * SS

    # 底板：圆角方形。小尺寸时不描边（会糊），大尺寸加极细描边增加精致度。
    inset = unit * 0.035
    radius = unit * 0.22
    canvas.rounded_rect(inset, inset, unit - inset, unit - inset, radius, BG)

    # 立方体几何：等轴测投影
    cx, cy = unit * 0.5, unit * 0.52
    half_w = unit * 0.29
    half_h = unit * 0.165
    height = unit * 0.30

    top = (cx, cy - height * 0.62)
    right = (cx + half_w, cy - height * 0.62 + half_h)
    left = (cx - half_w, cy - height * 0.62 + half_h)
    mid = (cx, cy - height * 0.62 + half_h * 2)

    bottom_left = (left[0], left[1] + height)
    bottom_mid = (mid[0], mid[1] + height)
    bottom_right = (right[0], right[1] + height)

    # 面：顶面浅、左右用品牌色的深浅两档
    canvas.polygon([top, right, mid, left], INK if size <= 32 else BG_SOFT)
    canvas.polygon([left, mid, bottom_mid, bottom_left], BRAND_DARK if size <= 32 else BRAND)
    canvas.polygon([mid, right, bottom_right, bottom_mid], BRAND if size <= 32 else BRAND_DARK)

    # 轮廓线宽随尺寸变化；小尺寸要更粗才看得见
    width = unit * (0.055 if size <= 32 else 0.038)
    outline = INK
    canvas.polyline([top, right, mid, left, top], outline, width)
    canvas.polyline([left, bottom_left], outline, width)
    canvas.polyline([mid, bottom_mid], outline, width)
    canvas.polyline([right, bottom_right], outline, width)
    canvas.polyline([bottom_left, bottom_mid, bottom_right], outline, width)

    return canvas.downsample(SS)


def build_ico(canvases: list[tuple[int, bytes]]) -> bytes:
    """把多尺寸 PNG 组装成 ICO（Vista+ 支持 PNG 压缩的 ICO）。"""
    count = len(canvases)
    header = struct.pack("<HHH", 0, 1, count)
    directory = b""
    offset = 6 + count * 16
    payload = b""
    for size, png in canvases:
        dimension = 0 if size >= 256 else size
        directory += struct.pack(
            "<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(png), offset
        )
        payload += png
        offset += len(png)
    return header + directory + payload


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 AI 3D Forge 图标")
    parser.add_argument("--preview", action="store_true", help="同时输出各尺寸 PNG 以便人工检查")
    args = parser.parse_args()

    OUT_ICO.parent.mkdir(parents=True, exist_ok=True)
    canvases: list[tuple[int, bytes]] = []
    for size in SIZES:
        canvas = draw_icon(size)
        png = canvas.to_png()
        canvases.append((size, png))
        if args.preview:
            PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
            (PREVIEW_DIR / f"icon-{size}.png").write_bytes(png)

    OUT_ICO.write_bytes(build_ico(canvases))
    size_kb = OUT_ICO.stat().st_size / 1024
    print(f"已生成 {OUT_ICO}（{len(SIZES)} 个尺寸，{size_kb:.1f} KB）")
    print(f"尺寸：{', '.join(str(s) for s in SIZES)}")
    if args.preview:
        print(f"预览图目录：{PREVIEW_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
