"""把 STL 渲染成图片 —— 纯标准库，不引入任何图像依赖。

为什么需要它：**所有确定性检查都通过，模型仍然可能是错的**。
实测过的例子：桌面垃圾桶的 6 根外壁加强筋里，有 4 根从外壁穿进了桶内腔。
当时 `solid_parts==1`、`watertight==True`、包围盒和体积全部正常 ——
指标全绿，用户拿到却不能用的桶。这个问题渲染一张图一眼就能看出来。

所以渲染不是为了好看，是为了**让 AI 真的看一眼**：
图片会连同实测数据一起交给多模态模型复核（见 `llm.review_geometry_image`）。

实现方式是软件光栅化（画家算法 + Z 缓冲），不依赖 OpenGL / matplotlib / Pillow：
* 投影：正交投影，方位角 + 俯仰角决定视角
* 着色：朗伯光照（视线左上方一束平行光）+ 环境光，只做灰度，
  避免颜色干扰对形体的判断
* 深度：Z 缓冲，保证近处的面盖住远处的面

大模型上会退化为「按步长抽样三角面」，这一点会在返回值里如实标出 ——
抽样会留下细小孔洞，不能让复核者以为自己看到的是完整实体。
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from .mesh import StlError, read_triangles

# 超过这个面数就抽样渲染。纯 Python 光栅化在这个量级下会慢到不可接受，
# 而本项目生成出来的模型通常在 1k~20k 面（实测 600~20000），远低于这条线。
SAMPLE_THRESHOLD = 120_000
DEFAULT_SIZE = 640
BACKGROUND = (246, 247, 249)


@dataclass
class RenderResult:
    """一次渲染的结果。"""

    path: Path
    width: int
    height: int
    view: str
    triangles_used: int
    sampled: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "width": self.width,
            "height": self.height,
            "view": self.view,
            "triangles_used": self.triangles_used,
            "sampled": self.sampled,
        }


def _write_png(path: Path, width: int, height: int, pixels: bytearray) -> None:
    """写出 8 位 RGB PNG（每行前面要有 filter 字节 0）。"""
    raw = bytearray()
    stride = width * 3
    for row in range(height):
        raw.append(0)
        raw.extend(pixels[row * stride : (row + 1) * stride])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def _rotation(azimuth_deg: float, elevation_deg: float) -> tuple[tuple[float, ...], ...]:
    """构造「把模型转到该视角」的旋转矩阵。

    先绕 Z 轴转方位角，再绕 X 轴转俯仰角；投影时取屏幕 x=第一列、y=第三列、
    深度=第二列，于是「模型的正上方」在屏幕上朝上。
    """
    az = math.radians(azimuth_deg)
    el = math.radians(elevation_deg)
    ca, sa = math.cos(az), math.sin(az)
    ce, se = math.cos(el), math.sin(el)
    # R = Rx(el) * Rz(az)
    return (
        (ca, -sa, 0.0),
        (ce * sa, ce * ca, -se),
        (se * sa, se * ca, ce),
    )


# 视角：一个前 3/4 视角能同时看到外形与侧面结构；
# 一个俯视能看清腔体开口、夹持口这类「从侧面看不出来」的结构。
VIEWS: tuple[tuple[str, float, float], ...] = (
    ("iso", -40.0, 24.0),
    ("top", -40.0, 68.0),
)


def _frame_all(points: list[tuple[float, float, float]]) -> tuple[tuple[float, float, float], float]:
    """用**整个模型**算中心与最长边，不是用抽样后的子集。

    为什么必须这样：面数超线时下面只拿抽样出来的三角面画图，
    如果包围盒也按这批点算，**最端点没被抽到就会把中心算偏、把最长边算小**，
    于是 `scale` 偏大，模型超出画布被当场裁掉一块 —— 复核图是给 AI 看「有没有穿腔、
    有没有缺件」的，自己先缺一个角就是谎报（轻则误判，重则把正确的模型说成没做对）。
    """
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    zs = [p[2] for p in points]
    center = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2)
    span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs), 1e-6)
    return center, span


def render_views(
    stl_path: Path,
    out_dir: Path,
    *,
    prefix: str = "preview",
    size: int = DEFAULT_SIZE,
) -> list[RenderResult]:
    """把 STL 渲染成若干视角的 PNG，返回渲染结果列表。

    面数过多时按步长抽样，并在 `RenderResult.sampled` 里如实标出。
    """
    points, _ = read_triangles(stl_path)
    face_count = len(points) // 3
    if face_count == 0:
        raise StlError("模型没有任何三角面片，无法渲染预览图。")

    step = 1
    sampled = False
    if face_count > SAMPLE_THRESHOLD:
        step = math.ceil(face_count / SAMPLE_THRESHOLD)
        sampled = True

    triples: list[tuple[tuple[float, float, float], ...]] = []
    for face in range(0, face_count, step):
        base = face * 3
        triples.append((points[base], points[base + 1], points[base + 2]))

    # 中心与缩放按**全量顶点**算（不是按抽样出来的这批），否则大模型会被裁掉一角
    center, span = _frame_all(points)
    scale = (size * 0.82) / span

    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[RenderResult] = []
    for name, azimuth, elevation in VIEWS:
        rot = _rotation(azimuth, elevation)
        depth_buffer = [math.inf] * (size * size)
        pixels = bytearray()
        for _ in range(size * size):
            pixels.extend(BACKGROUND)

        light = (-0.45, 0.35, 0.82)  # 左上前方一束光

        for tri in triples:
            # 旋转 + 居中 + 缩放；屏幕 y 取反（图像行号向下增大）
            projected = []
            for point in tri:
                x = point[0] - center[0]
                y = point[1] - center[1]
                z = point[2] - center[2]
                rx = rot[0][0] * x + rot[0][1] * y + rot[0][2] * z
                ry = rot[1][0] * x + rot[1][1] * y + rot[1][2] * z
                rz = rot[2][0] * x + rot[2][1] * y + rot[2][2] * z
                projected.append((rx, rz, ry))

            (x0, sy0, d0), (x1, sy1, d1), (x2, sy2, d2) = projected
            px0 = size / 2 + x0 * scale
            py0 = size / 2 - sy0 * scale
            px1 = size / 2 + x1 * scale
            py1 = size / 2 - sy1 * scale
            px2 = size / 2 + x2 * scale
            py2 = size / 2 - sy2 * scale

            min_x = max(0, int(math.floor(min(px0, px1, px2))))
            max_x = min(size - 1, int(math.ceil(max(px0, px1, px2))))
            min_y = max(0, int(math.floor(min(py0, py1, py2))))
            max_y = min(size - 1, int(math.ceil(max(py0, py1, py2))))
            if min_x > max_x or min_y > max_y:
                continue

            area = (px1 - px0) * (py2 - py0) - (px2 - px0) * (py1 - py0)
            if abs(area) < 1e-9:
                continue  # 退化面（在屏幕上是一条线），没有可填充的像素

            # 面法线（旋转后）决定明暗：朝光的亮，背光的暗
            ux, uy, uz = (x1 - x0, d1 - d0, sy1 - sy0)
            vx, vy, vz = (x2 - x0, d2 - d0, sy2 - sy0)
            nx = uy * vz - uz * vy
            ny = uz * vx - ux * vz
            nz = ux * vy - uy * vx
            length = math.sqrt(nx * nx + ny * ny + nz * nz)
            if length < 1e-12:
                continue
            nx, ny, nz = nx / length, ny / length, nz / length
            lambert = abs(nx * light[0] + ny * light[1] + nz * light[2])
            shade = int(255 * (0.28 + 0.72 * lambert))
            shade = max(40, min(255, shade))
            color = bytes((shade, shade, min(255, shade + 6)))

            inv_area = 1.0 / area
            for py in range(min_y, max_y + 1):
                fy = py + 0.5
                row_base = py * size
                for px in range(min_x, max_x + 1):
                    fx = px + 0.5
                    w0 = ((px1 - fx) * (py2 - fy) - (px2 - fx) * (py1 - fy)) * inv_area
                    if w0 < 0:
                        continue
                    w1 = ((px2 - fx) * (py0 - fy) - (px0 - fx) * (py2 - fy)) * inv_area
                    if w1 < 0:
                        continue
                    w2 = 1.0 - w0 - w1
                    if w2 < 0:
                        continue
                    depth = w0 * d0 + w1 * d1 + w2 * d2
                    index = row_base + px
                    if depth < depth_buffer[index]:
                        depth_buffer[index] = depth
                        offset = index * 3
                        pixels[offset : offset + 3] = color

        path = out_dir / f"{prefix}-{name}.png"
        _write_png(path, size, size, pixels)
        results.append(
            RenderResult(
                path=path,
                width=size,
                height=size,
                view=name,
                triangles_used=len(triples),
                sampled=sampled,
            )
        )
    return results
