"""AI 3D Forge —— STL 网格分析（3D 模型引擎自检的核心）。

不依赖任何第三方库：直接解析二进制/ASCII STL，用拓扑与几何量算出「能不能打印」的客观依据，
再把结论连同数据一起交给 AI 做解释与建议 —— 这样 AI 的打印性检查有真实数据支撑，
而不是凭空编造。

指标含义：
  watertight        每条边恰好被 2 个三角面共享 → 封闭实体，切片软件不会报错
  volume_mm3        散度定理算出的有向体积；为正说明面片法向朝外
  bbox              包围盒尺寸，用于判断是否能放进打印机、是否贴平台
"""

from __future__ import annotations

import bisect
import math
import random
import re
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class StlError(RuntimeError):
    def __init__(self, message: str, *, hint: str | None = None, code: str = "OPENSCAD_FAILED") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code


@dataclass
class MeshMetrics:
    triangles: int = 0
    vertices: int = 0
    bbox: tuple[float, float, float] = (0.0, 0.0, 0.0)
    bbox_min: tuple[float, float, float] = (0.0, 0.0, 0.0)
    bbox_max: tuple[float, float, float] = (0.0, 0.0, 0.0)
    volume_mm3: float = 0.0
    surface_area_mm2: float = 0.0
    watertight: bool = False
    watertight_ratio: float = 0.0
    boundary_edges: int = 0
    non_manifold_edges: int = 0
    degenerate_triangles: int = 0
    sits_on_plate: bool = False
    is_ascii: bool = False
    issues: list[str] = field(default_factory=list)
    # 触发了哪一类尺寸问题（便于界面与测试区分「太薄」和「太细」）
    too_thin_rough: bool = False
    too_thin_wall: bool = False
    exceeds_build_volume: bool = False
    # 实测到的螺旋螺纹牙深（mm）；None = 没有检测到螺纹。
    # 为什么要单独记：细牙螺纹的牙尖只有零点几毫米，用普通壁厚判据看会误报成
    # 「壁太薄、无法打印」——实测一个完全合规的 M72×0.75 转接环就被这么报过。
    thread_depth_mm: float | None = None
    # 螺纹是不是真螺旋（False = 同心环，看着有纹路但拧不上）；None = 没测/没螺纹
    thread_is_helix: bool | None = None
    # 模型由几个互不相连的实体组成。
    # 为什么必须测这个：AI 很爱把「一个成品」做成「几个零件的组合」——
    # 实测用户的桌面垃圾桶需求，它给出的是「一个夹子 + 一个普通垃圾桶」，
    # 两件分开、打印完还得自己拼。这是「模型不贴合描述」最常见的形态，
    # 而包围盒/体积这些指标完全看不出来（两个零件合起来包围盒照样对）。
    solid_parts: int = 0
    # 其余零件的体积（用来判断是不是掉了个小碎片）
    stray_parts: int = 0
    # 散件的具体位置与尺寸：只告诉 AI「有 80 个散件」它没法定位是哪段代码，
    # 给出「哪一件、多大、在什么位置」它才能对回自己写的 for 循环。
    # 每项：{"center": [x,y,z], "size": [dx,dy,dz], "volume_mm3": v, "faces": n}
    stray_details: list[dict[str, object]] = field(default_factory=list)
    # 腔体内部的孤岛：水平截面里「被空腔完全包围的材料块」。
    # 为什么必须测这个：AI 给容器类零件加「外壁加强筋」时，常写成
    # 「rotate 一圈再摆到固定半径」，而矩形桶身到中轴的距离随角度变化 ——
    # 于是筋在底端埋进壁里、到顶端就飘进桶内腔。整体仍然是**一个**连通实体，
    # 所以「零件数」检查完全看不出问题，但用户拿到的是一个内壁长出横板的桶。
    # 元素为 (z 高度 mm, 该层孤岛数量)。
    cavity_islands: list[tuple[float, int]] = field(default_factory=list)
    # 射线法实测的壁厚分布（真正反映「容器壁有多厚」的那个指标）。
    # 旧的「包围盒最小边」对空心件完全失效：桌面垃圾桶 110×186×110、壁厚 2.4mm，
    # 包围盒最小边 110mm，壁做得再薄也不会报警 —— 而容器正是本软件的主要用途。
    wall: "WallThickness | None" = None
    # 射线法实测的孔径（在模型中部高度，从轴线向两侧走到撞上材料）。
    # 为什么必须实测（HANDOVER P0）：改孔径时若源码把孔写成表达式（如 d=outer-2*wall），
    # 参数核对根本看不到孔变没变；而几何明明能量。None = 该高度轴线上是材料（无孔）。
    hole_diameter_mm: float | None = None
    # 三条轴上「被材料夹住的空档」最大值（夹口/插槽/内腔那类减法特征的指纹）。
    # 为什么必须由外部按需补测（jobs 在源码声明了间隙尺寸时才测）：
    # 刀具没切到实体时，模型是一整块实心料 —— 零件数 1、封闭、壁厚「很充足」、
    # 包围盒也对，所有既有指标全绿，只有「找不到任何空隙」能揭穿它。
    interior_gaps: "InteriorGaps | None" = None
    # 源码顶层参数里声明的间隙/夹口/插槽：(标签, mm, 参数名)。由 jobs 填入，
    # 与 interior_gaps 配成一条实测判定（判据是「自洽」：AI 自己声明的要它自己做到）。
    declared_gaps: list[tuple[str, float, str]] = field(default_factory=list)
    # 多个高度的实测孔径剖面（六个探针高度，自底向上）。
    # 为什么不只量中部：一个模型可能同时有「腔体」和「底部小孔」，只量中部会
    # 把腔体直径当成孔径（实测杯子：中部量到的是腔、排水孔在底部）。改动校验
    # 拿剖面去匹配用户声明的孔径——**任一高度匹配即视为做到**，避免误报。
    # 探针里两个贴底、一个贴顶（`_HOLE_PROFILE_PLAN`）：只按比例取会漏掉薄底座里的孔。
    hole_profile: list[float | None] = field(default_factory=list)
    # 贴底/贴顶那一层里「足迹内部是材料」的比例（0~1）。判“实心底”有没有被挖成开口管。
    # 为什么需要它（实测，31 份真模型）：AI 写的笔筒经常用
    # `difference(){ 外坯; cylinder(r=inner, h=height+0.01); }` —— 内腔圆柱从 z=0 就开挖，
    # 于是**底部整个是开的**（只留下一圈壁厚）。件数 1、封闭、包围盒对、壁厚不报、
    # 孔径剖面全 None（因而“量不到就不报”），一切现有指标全绿，而笔放进去就漏。
    # 实测 31 份历史版本交付时底是开口的有 18 份，产品当时对用户说的是“改好了”。
    # 两端都要量（不只是底）：盖子/罩壳这类件**天生开口朝下**，只看底会把正确设计
    # 报成缺陷；“两端都开”才是无论怎么放都装不了东西的开口管。
    bottom_coverage: float | None = None
    top_coverage: float | None = None
    # 支撑量估算与耗材用量估算。
    # support 在 analyze() 里算（只需要自支撑角）；usage 需要材料密度，
    # 由调用方（jobs）拿到用户选的材质后再算。
    support: "SupportEstimate | None" = None
    # 支撑占比偏高时算出来的「更省支撑的摆放方向」（六个轴向里最优的那个）。
    # 只说「换个方向」用户不知道往哪换，必须给出具体方向和预期省多少。
    orientation_hint: "dict[str, object] | None" = None
    usage: "UsageEstimate | None" = None

    @property
    def volume_cm3(self) -> float:
        return self.volume_mm3 / 1000.0

    def to_dict(self) -> dict[str, object]:
        return {
            "triangles": self.triangles,
            "vertices": self.vertices,
            "bbox": {"x": round(self.bbox[0], 2), "y": round(self.bbox[1], 2), "z": round(self.bbox[2], 2)},
            "bbox_min": [round(v, 2) for v in self.bbox_min],
            "bbox_max": [round(v, 2) for v in self.bbox_max],
            "volume_mm3": round(self.volume_mm3, 2),
            "volume_cm3": round(self.volume_cm3, 3),
            "surface_area_mm2": round(self.surface_area_mm2, 2),
            "watertight": self.watertight,
            "watertight_ratio": round(self.watertight_ratio, 4),
            "boundary_edges": self.boundary_edges,
            "non_manifold_edges": self.non_manifold_edges,
            "degenerate_triangles": self.degenerate_triangles,
            "sits_on_plate": self.sits_on_plate,
            "solid_parts": self.solid_parts,
            "stray_parts": self.stray_parts,
            "stray_details": self.stray_details[:12],
            "cavity_islands": [
                {"z": round(z, 2), "count": n} for z, n in self.cavity_islands
            ],
            "wall": self.wall.to_dict() if self.wall else None,
            "hole_diameter_mm": (
                round(self.hole_diameter_mm, 2) if self.hole_diameter_mm is not None else None
            ),
            "interior_gaps": self.interior_gaps.to_dict() if self.interior_gaps else None,
            "declared_gaps": [
                {"label": label, "mm": round(float(mm), 2), "param": name}
                for label, mm, name in self.declared_gaps
            ],
            "hole_profile": [
                round(d, 2) if d is not None else None for d in self.hole_profile
            ],
            "bottom_coverage_pct": (
                round(self.bottom_coverage * 100.0, 1) if self.bottom_coverage is not None else None
            ),
            "top_coverage_pct": (
                round(self.top_coverage * 100.0, 1) if self.top_coverage is not None else None
            ),
            "support": self.support.to_dict() if self.support else None,
            "orientation_hint": self.orientation_hint,
            "usage": self.usage.to_dict() if self.usage else None,
            "issues": list(self.issues),
        }


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
def _looks_binary(head: bytes) -> bool:
    """判断 STL 是二进制还是 ASCII。

    注意：不能只看开头是不是 "solid" —— OpenSCAD 生成的二进制 STL
    头部恰好就是 "solid ..."（实测），只看前缀会把二进制文件误判成 ASCII。
    因此改为用「文件长度 = 84 + 50 × 面片数」这个自校验条件做判决。
    """
    if len(head) < 84:
        return False
    (count,) = struct.unpack_from("<I", head, 80)
    expected = 84 + 50 * count
    if expected == len(head):
        return True
    # 少数写出过程被中断的文件长度略有出入，允许 50 字节以内的偏差
    if count > 0 and abs(expected - len(head)) < 50:
        return True
    if not head[:5].lower().startswith(b"solid"):
        return True
    # 以 "solid" 开头、长度又对不上：可能是 ASCII，也可能是**被写了一半的二进制**。
    # 后者是真实会发生的（OpenSCAD 被杀掉/磁盘满），而这两种文件的报错完全不同：
    # 判成 ASCII 会得到含糊的「无法解析模型文件内容」，
    # 判成二进制才能说出「头部声明 N 面、实际只有 M 面，OpenSCAD 没写完」。
    # 区分办法：二进制的面数域后面紧跟着浮点数据，几乎必然出现 0x00 字节；
    # ASCII 文本里不该有 NUL。
    if b"\x00" in head[80:160]:
        return True
    # 以 "solid" 开头但长度对不上、又没有 NUL：很可能是 ASCII，交给 ASCII 解析器
    return False


def read_triangles(path: Path) -> tuple[list[tuple[float, float, float]], bool]:
    """读出全部三角面片顶点。返回 (顶点列表, 是否 ASCII)。

    顶点列表长度为 9 * 面片数，按 [ax,ay,az, bx,by,bz, cx,cy,cz] 排列。
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise StlError(
            f"无法读取生成的模型文件：{exc.strerror or exc}",
            hint="请重试；若反复失败，请检查磁盘空间与杀毒软件是否拦截了文件写入。",
        ) from exc

    if not raw:
        raise StlError("生成的模型文件是空的，说明 OpenSCAD 没有成功输出几何体。")

    if _looks_binary(raw):
        return _parse_binary(raw), False
    return _parse_ascii(raw), True


def _parse_binary(raw: bytes) -> list[tuple[float, float, float]]:
    if len(raw) < 84:
        raise StlError("模型文件不完整（缺少二进制 STL 头信息）。")
    (count,) = struct.unpack_from("<I", raw, 80)
    available = (len(raw) - 84) // 50
    if count == 0:
        raise StlError("模型文件里没有任何三角面片，说明模型是空的。")
    if count > available:
        # 头部声明的面数比文件里实际有的多 = 文件没写完（OpenSCAD 被杀掉、
        # 磁盘满、断电都会这样）。
        #
        # 以前这里**悄悄**按实际长度截断就继续算，后果比报错严重得多：
        # 一份只有 76% 长度的文件会被整段错位解析（每 50 字节切一刀，
        # 但真实步长不是 50），读出来的坐标是垃圾 —— 实测出现过 NaN 和 3e34，
        # 一路走到 `int(round(nan))` 才炸成 ValueError，用户看到的是
        # 「cannot convert float NaN to integer」这种和建模毫无关系的话。
        # 就算不炸，也是一份残缺网格被当成正常模型分析：
        # 封闭性、零件数、壁厚全是错的，报告会说「模型有 300 个散件」。
        #
        # 只容忍极小的尾部差额（写入收尾那一瞬间的竞态），其余如实报错。
        if count - available > 16:
            raise StlError(
                f"模型文件不完整：头部声明 {count:,} 个三角面，实际只有 {available:,} 个。",
                hint="OpenSCAD 可能没有写完（被中断、磁盘满或被安全软件拦截）。请重新生成。",
            )
        count = available
    vertices: list[tuple[float, float, float]] = []
    unpack = struct.unpack_from
    offset = 84
    for _ in range(count):
        values = unpack("<12f", raw, offset)
        # 每面只算一次「和是否有限」：NaN 会污染整条加法链，inf 也会，
        # 所以一次 isfinite 就能把损坏的坐标挡住，代价是每面一次而不是每坐标一次。
        if not math.isfinite(
            values[3] + values[4] + values[5] + values[6]
            + values[7] + values[8] + values[9] + values[10] + values[11]
        ):
            raise StlError(
                "模型文件里的坐标不是有效数字（NaN/无穷），文件已损坏。",
                hint="请重新生成；若反复出现，请检查磁盘空间与安全软件是否拦截了写入。",
            )
        vertices.extend(((values[3], values[4], values[5]),
                         (values[6], values[7], values[8]),
                         (values[9], values[10], values[11])))
        offset += 50
    return vertices


_ASCII_VERTEX_RE = re.compile(
    r"vertex\s+(-?[\d.eE+]+)\s+(-?[\d.eE+]+)\s+(-?[\d.eE+]+)", re.IGNORECASE
)


def _parse_ascii(raw: bytes) -> list[tuple[float, float, float]]:
    text = raw.decode("utf-8", errors="replace")
    vertices: list[tuple[float, float, float]] = []
    for match in _ASCII_VERTEX_RE.finditer(text):
        try:
            point = (float(match.group(1)), float(match.group(2)), float(match.group(3)))
        except ValueError:
            continue
        # `1e999` 这种字面量会解析成 inf，同样属于损坏数据（二进制那条路径也挡）
        if not all(math.isfinite(value) for value in point):
            raise StlError(
                "模型文件里的坐标不是有效数字（NaN/无穷），文件已损坏。",
                hint="请重新生成；若反复出现，请检查磁盘空间与安全软件是否拦截了写入。",
            )
        vertices.append(point)
    if not vertices:
        raise StlError("无法解析模型文件内容，可能文件已损坏。")

    # 完整性自校验（二进制路径早就有，ASCII 一直是个缺口）。
    #
    # 为什么必须补：OpenSCAD 在这里默认写的是 **ASCII STL**（实测：同一模型
    # 171,329 字节 / 1,024 个 facet），而写一半就被中断的 ASCII 文件能被上面
    # 那个循环**完好地解析出一堆合法坐标** —— 没有 NaN、没有错位，只是少了
    # 后面的面。于是一份残缺网格被当成正常模型送去分析，所有判据都建在
    # 切掉一半的几何上（实测过一次同一 SCAD 量出 512 面而其余 12 次都是 1024，
    # 那一轮的孔径判据直接翻了：hole=None vs 17.60）。
    #
    # 三条都是「文件自己与自己矛盾」，不会误伤正规导出器：
    #   1. 每个 facet 恰好 3 个 vertex → 顶点数必为 3 的倍数；
    #   2. `facet normal` 的个数 = 顶点数 / 3（切在 facet 中间时两条一起错，
    #      刚好切在边界上时只有第二条与第三条错）；
    #   3. 规范要求以 `endsolid` 收尾，写完才有这一行。
    if len(vertices) % 3:
        raise StlError(
            f"模型文件不完整：顶点数 {len(vertices):,} 不是 3 的倍数，最后某个三角面被截断了。",
            hint="OpenSCAD 可能没写完（被中断、磁盘满或被安全软件拦截）。请重新生成。",
        )
    facet_count = text.count("facet normal")
    if facet_count != len(vertices) // 3:
        raise StlError(
            f"模型文件不完整：声明了 {facet_count:,} 个三角面，实际只读到 "
            f"{len(vertices) // 3:,} 个。",
            hint="OpenSCAD 可能没写完（被中断、磁盘满或被安全软件拦截）。请重新生成。",
        )
    if "endsolid" not in text:
        raise StlError(
            "模型文件不完整：ASCII STL 缺少收尾的 endsolid 行。",
            hint="OpenSCAD 可能没写完（被中断、磁盘满或被安全软件拦截）。请重新生成。",
        )
    return vertices


# --------------------------------------------------------------------------
# 分析
# --------------------------------------------------------------------------
@dataclass
class AnalysisLimits:
    """做几何判断时用到的「打印机相关」阈值。

    默认值对应一台常见的 220×220×250 / 0.4mm 喷嘴 FDM 打印机，
    但真实判断应当来自用户选择的机型（见 printers.resolve_process）。
    """

    build_x: float = 220.0
    build_y: float = 220.0
    build_z: float = 250.0
    nozzle_mm: float = 0.4
    min_wall_mm: float = 1.2
    min_protrusion_mm: float = 0.6
    max_overhang_deg: int = 55
    max_bridge_mm: float = 30.0
    # 「零件最小外形尺寸」= 2 × 喷嘴 = 一层两圈走线，0.4mm 喷嘴对应 0.8mm。
    # 这里必须与 printers.resolve_process 的规则一致：那边算的就是 2×喷嘴。
    # 旧版这里写成 4.0，与自己的规则矛盾十倍，而且会让「1.2mm 的薄片」
    # 被判成「连一层走线都放不下」—— 1.2mm 在 0.4mm 喷嘴上明明是三层。
    # 与 DfAM 参考值也对得上：FDM 的最小正向特征（min positive feature）是 0.8mm
    # （见 earthtojake/text-to-cad 的 dfam-check 工艺限值表）。
    rough_min_dim_mm: float = 0.8
    max_triangles: int = 400_000
    printer_name: str = "通用 FDM 打印机"
    is_resin: bool = False

    @property
    def build_volume(self) -> tuple[float, float, float]:
        return (self.build_x, self.build_y, self.build_z)


def limits_from_printer(profile: Any, material: str | None = None) -> AnalysisLimits:
    """从打印机配置推导几何判断阈值。

    刻意不 import printers —— 由调用方传入 profile，保持本模块零依赖、可单独测试。
    用鸭子类型读取属性，避免循环导入。
    """
    from .printers import resolve_process  # 局部导入，避免模块级循环

    constraints = resolve_process(profile, material)
    is_resin = getattr(profile, "extruder", "") == "resin" or getattr(profile, "nozzle_mm", 0.4) <= 0
    return AnalysisLimits(
        build_x=constraints.max_build_x,
        build_y=constraints.max_build_y,
        build_z=constraints.max_build_z,
        nozzle_mm=getattr(profile, "nozzle_mm", 0.4),
        min_wall_mm=constraints.min_wall_mm,
        min_protrusion_mm=constraints.min_protrusion_mm,
        max_overhang_deg=constraints.max_overhang_deg,
        max_bridge_mm=constraints.max_horizontal_span_mm,
        rough_min_dim_mm=constraints.rough_min_dim_mm,
        printer_name=getattr(profile, "name", "未指定打印机"),
        is_resin=is_resin,
    )


def measure_thread_depth(points: list[tuple[float, float, float]]) -> float | None:
    """判断这个模型上有没有**螺旋螺纹**，有就返回实测牙深（mm）。

    为什么要单独判一次：细牙螺纹的牙尖只有零点几毫米，用普通「壁厚」判据去看，
    一个完全合规的 M72×0.75 转接环会被报成"壁厚 0.05mm，远低于 1.2mm，无法打印" ——
    这是**误报**，用户会以为模型废了。

    判据（实测定标）：
      * 螺纹在固定高度上半径随角度周期变化，周期 ≈ 螺距对应的角度（0.75mm 螺距、
        半径 36mm 时只有约 1.2°），比网格角度步长还小 —— 所以**不能**按角度分箱量，
        那样会把牙顶牙底平均掉（实测只量出 0.07mm，真值 0.46mm）；
      * 正确做法：取一个**跨一个螺距的高度带**（相位走满一圈），量该带内
        **外表面**（半径较大的一半）的半径极差 —— 光滑圆筒≈0，真螺纹≈牙深；
      * 高度带取模型高度的 1/8 且不超过 1.2mm；样本太少时返回 None（不猜）。

    已知螺距范围 0.5~3mm 都覆盖得住（0.5mm 螺距时带高 0.5mm 也够走满一圈相位）。
    """
    if not points:
        return None
    zs = [p[2] for p in points]
    if not zs:
        return None
    lo, hi = min(zs), max(zs)
    height = hi - lo
    if height < 0.6:
        return None
    band = min(1.2, max(0.5, height / 8.0))
    # 多带扫描：螺纹不一定布满全高（例如升口径转接环翻转后外螺纹只在顶部，
    # 固定单带会采到光滑的外扩台肩或跨到挡肩过渡区，量出 0 或 2.4mm 的假值）。
    # 逐带取「外表面半径极差」，只接受 plausible 的牙深（0.15~2.0mm：
    # 0.5mm 螺距牙深约 0.3mm，3mm 螺距牙深约 1.8mm；跨过渡区的假值通常 >2mm），
    # 取其中最大者；所有带都不 plausible 才返回 None（不猜）。
    best: float | None = None
    for frac in (0.15, 0.35, 0.55, 0.75, 0.9):
        z0 = lo + height * frac
        z1 = min(z0 + band, hi)
        if z1 <= z0:
            continue
        radii = [math.hypot(p[0], p[1]) for p in points if z0 <= p[2] <= z1]
        if len(radii) < 64:
            continue
        # 内外分界的极值必须**只算一次**拿出去。
        # 写成 `[r for r in radii if r > (max(radii) + min(radii)) / 2]` 看上去没毛病，
        # 但那个条件对**每个元素**都重算一遍全表 max/min —— 代价从 O(N) 变成 O(N²)。
        # 实测（本机，cProfile）：24,016 面的模型上本函数吃 **41.4s**，
        # 占整段 analyze 47.8s 的 86%（`max` 32.4 万次 + `min` 34.8 万次）；
        # 而它在每个生成作业里都无条件跑 —— 手机支架、水杯架这些根本没有螺纹。
        r_lo, r_hi = min(radii), max(radii)
        outer = [r for r in radii if r > (r_lo + r_hi) / 2]
        if len(outer) < 32:
            continue
        depth = max(outer) - min(outer)
        # 阈值 0.15mm：光滑圆筒的镶嵌噪声远小于它；上限 2.0mm 排除跨过渡区的假值
        if 0.15 <= depth <= 2.0 and (best is None or depth > best):
            best = depth
    return round(best, 3) if best is not None else None


def detect_helix_thread(points: list[tuple[float, float, float]]) -> bool | None:
    """判断螺纹到底是**真螺旋**还是「一圈一圈的同心环」。

    为什么必须分清（实测踩到的事故）：用 `rotate_extrude` 把一个牙型绕轴转一圈，
    得到的是**同心环**——半径随高度变化、但同一高度上各个角度完全一样。
    这种零件看着有"螺纹纹理"，实际根本拧不进去（滤镜转接环就是这么翻车的）。
    上面那个 measure_thread_depth 只量"半径随高度有没有起伏"，
    同心环照样能过 —— 所以再加这一条。

    判据（不依赖已知螺距，也不怕角度采样比牙距粗）：
      真螺旋：同一个**薄片高度**里，牙顶只出现在某一小段角度上 → 这些点的角度
              高度集中（圆统计量 R = |mean(e^{iθ})| 接近 1）；
      同心环：同一个高度上四周到处都有牙顶 → 角度均匀分布 → R 接近 0。
    返回 True/False；样本不足时返回 None（不猜）。
    """
    if len(points) < 200:
        return None
    radii = [math.hypot(p[0], p[1]) for p in points]
    r_max = max(radii)
    if r_max <= 0:
        return None
    zs = [p[2] for p in points]
    lo, hi = min(zs), max(zs)
    if hi - lo < 0.6:
        return None
    slice_h = max(0.05, min(0.25, (hi - lo) / 60.0))
    best: float | None = None
    for frac in (0.25, 0.4, 0.55, 0.7):
        z0 = lo + (hi - lo) * frac
        in_slice = [
            (math.hypot(p[0], p[1]), math.atan2(p[1], p[0]))
            for p in points
            if z0 <= p[2] <= z0 + slice_h
        ]
        if len(in_slice) < 8:
            continue
        radii_in = [r for r, _a in in_slice]
        span = max(radii_in) - min(radii_in)
        if span <= 0.05:
            # 这一片里半径几乎没有变化（同心环的环带内部就是这样），没有牙顶可分
            continue
        # 取这一片里**最靠外**的一小撮点（按分位数取，而不是按距最大半径的差值）：
        # 切片里往往同时含内孔与外表面，用"最大半径 - 25% span"会把整个外表面都算进来，
        # 于是螺旋也被算成"四周到处都有牙顶"（实测踩过这个坑）。
        ordered = sorted(radii_in)
        cutoff = ordered[int(len(ordered) * 0.85)]
        crest = [a for r, a in in_slice if r >= cutoff]
        if len(crest) < 5:
            continue
        sx = sum(math.cos(a) for a in crest) / len(crest)
        sy = sum(math.sin(a) for a in crest) / len(crest)
        concentration = math.hypot(sx, sy)
        best = concentration if best is None else max(best, concentration)
    if best is None:
        return None
    return best >= 0.5


def analyze(
    path: Path,
    limits: AnalysisLimits | None = None,
    declared_gaps: list[tuple[str, float, str]] | None = None,
) -> MeshMetrics:
    """完整分析一个 STL 文件。

    limits 决定「薄壁」「放不下」这类判断的阈值；不传则用通用 FDM 默认值。

    declared_gaps（(标签, mm, 参数名)，由 jobs 从源码顶层参数里提出来）非空时才会
    去测内部空隙：本函数已经在内存里握着 points，顺手测比让调用方回头重读一次
    STL 便宜得多；而「声明了什么间隙」只有拿着源码的调用方知道。
    """
    active = limits or AnalysisLimits()
    if not path.is_file():
        raise StlError("没有找到生成的 STL 文件，OpenSCAD 可能未能完成编译。")

    points, is_ascii = read_triangles(path)
    triangle_count = len(points) // 3
    metrics = MeshMetrics(triangles=triangle_count, is_ascii=is_ascii)

    if triangle_count == 0:
        raise StlError("模型没有任何三角面片。")

    min_x = min_y = min_z = math.inf
    max_x = max_y = max_z = -math.inf
    volume6 = 0.0  # 6 倍体积，最后除以 6
    area2 = 0.0
    degenerate = 0
    edges: dict[tuple[tuple[float, float, float], tuple[float, float, float]], int] = {}
    unique: set[tuple[float, float, float]] = set()

    for index in range(0, triangle_count * 3, 3):
        pa = points[index]
        pb = points[index + 1]
        pc = points[index + 2]
        ax, ay, az = pa
        bx, by, bz = pb
        cx, cy, cz = pc

        for x, y, z in (pa, pb, pc):
            if x < min_x:
                min_x = x
            if y < min_y:
                min_y = y
            if z < min_z:
                min_z = z
            if x > max_x:
                max_x = x
            if y > max_y:
                max_y = y
            if z > max_z:
                max_z = z

        # 量化键「一面算一次、三处复用」：顶点去重、边统计、包围盒都用同一组。
        # 以前是每用一次就重新 round 一遍（3 个顶点 × 3 坐标 × 3 处 = 27 次/面），
        # 100 万面的模型上就是 2700 万次 round()，实测这是本函数最大的一笔开销。
        # 表达式与原来逐字一致（round(x, 5)），所以结果完全相同。
        ka = (round(ax, 5), round(ay, 5), round(az, 5))
        kb = (round(bx, 5), round(by, 5), round(bz, 5))
        kc = (round(cx, 5), round(cy, 5), round(cz, 5))
        unique.add(ka)
        unique.add(kb)
        unique.add(kc)

        # 有向体积（散度定理）
        volume6 += (
            ax * (by * cz - bz * cy)
            - ay * (bx * cz - bz * cx)
            + az * (bx * cy - by * cx)
        )

        # 面积
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length <= 1e-12:
            degenerate += 1
        else:
            area2 += length

        # 边统计（用于封闭性判断）。键用「拼平」的 6 元组而不是嵌套元组：
        # 同样能唯一标识一条无向边，但哈希开销小得多。
        for p, q in ((ka, kb), (kb, kc), (kc, ka)):
            edge = p + q if p <= q else q + p
            edges[edge] = edges.get(edge, 0) + 1

    metrics.vertices = len(unique)
    metrics.bbox_min = (min_x, min_y, min_z)
    metrics.bbox_max = (max_x, max_y, max_z)
    metrics.bbox = (max_x - min_x, max_y - min_y, max_z - min_z)
    metrics.volume_mm3 = abs(volume6) / 6.0
    metrics.surface_area_mm2 = area2 / 2.0
    metrics.degenerate_triangles = degenerate

    total_edges = len(edges)
    boundary = sum(1 for count in edges.values() if count == 1)
    non_manifold = sum(1 for count in edges.values() if count > 2)
    metrics.boundary_edges = boundary
    metrics.non_manifold_edges = non_manifold
    metrics.watertight = total_edges > 0 and boundary == 0 and non_manifold == 0
    metrics.watertight_ratio = (
        (total_edges - boundary - non_manifold) / total_edges if total_edges else 0.0
    )
    # STL 的坐标轴：OpenSCAD 的 Z 轴向上；贴平台 = 底面在 z≈0
    metrics.sits_on_plate = abs(min_z) < 0.05

    solid_parts, stray_parts, stray_details = _count_parts(points, triangle_count)
    metrics.solid_parts = solid_parts
    metrics.stray_parts = stray_parts
    metrics.stray_details = stray_details
    metrics.cavity_islands = _detect_cavity_islands(points, triangle_count, metrics)
    metrics.wall = measure_wall_thickness(points, triangle_count, metrics)
    # 螺纹检测：细牙的牙尖只有零点几毫米，不知道这一点的话，
    # 一个完全合规的 M72×0.75 转接环会被"壁厚"判据误报成无法打印。
    metrics.thread_depth_mm = measure_thread_depth(points)
    if metrics.thread_depth_mm is not None:
        # 有牙型起伏还不够 —— 还要确认它是**螺旋**，而不是同心环（拧不进去的那种）
        metrics.thread_is_helix = detect_helix_thread(points)
    # 孔径实测：参数核对看不到「把孔写成表达式」的改法，几何能。
    # 七个高度（1 个主测量 + 6 个剖面）**共用一张面网格与一个时间预算**：
    # 网格构建是 O(面数)（实测 31k 面 0.14s），旧写法每次调用重建一遍 = 白付 7 次；
    # 预算共用则把「测一个孔」的代价锁在 _HOLE_PHASE_BUDGET_S 内，测不完就如实 None。
    # 剖面高度见 `_hole_profile_heights()`：其中两个贴底、一个贴顶，为了照到薄底座里的孔。
    mid_z = (metrics.bbox_min[2] + metrics.bbox_max[2]) / 2.0
    hole_center = (
        (metrics.bbox_min[0] + metrics.bbox_max[0]) / 2.0,
        (metrics.bbox_min[1] + metrics.bbox_max[1]) / 2.0,
    )
    hole_deadline = time.monotonic() + _HOLE_PHASE_BUDGET_S
    hole_grid = _FaceGrid(points, triangle_count, 1.0) if triangle_count else None
    metrics.hole_diameter_mm = measure_hole_diameter(
        points,
        triangle_count,
        mid_z,
        center=hole_center,
        deadline=hole_deadline,
        grid=hole_grid,
    )
    # 多高度剖面：供改动校验判断「用户声明的孔径到底做没做到」。
    # 高度取自 `_hole_profile_heights()`（含贴底/贴顶的绝对探针，见那里的实测理由）。
    metrics.hole_profile = [
        measure_hole_diameter(
            points,
            triangle_count,
            z,
            center=hole_center,
            deadline=hole_deadline,
            grid=hole_grid,
        )
        for z in _hole_profile_heights(metrics.bbox_min[2], metrics.bbox_max[2])
    ]
    # 两端材料占比：贴底与贴顶各量一层（判“实心底被挖穿了”）。
    # 代价实测（本仓 31 份真模型里最大的 30,980 面）：3~5 条扫线 0.005~0.05s，
    # 比一个孔径探针还便宜，所以给**自己的一份小预算**而不是挤用孔测量剩下的时间：
    # 孔那边超预算时不能连这条也顺带变 None（那会把一个真需求从清单上抹掉）。
    # 两端各用自己的 deadline：只量底会把开口朝下的盖子误判成缺陷（见函数 docstring）。
    metrics.bottom_coverage = measure_end_coverage(
        points,
        triangle_count,
        metrics,
        grid=hole_grid,
        deadline=time.monotonic() + _END_BUDGET_S,
    )
    metrics.top_coverage = measure_end_coverage(
        points,
        triangle_count,
        metrics,
        at_top=True,
        grid=hole_grid,
        deadline=time.monotonic() + _END_BUDGET_S,
    )
    metrics.support = estimate_support(points, triangle_count, metrics, active.max_overhang_deg)
    # 支撑占比高时顺带算一次「哪个方向更省」。
    # 注意：六个方向的支撑量用的是同一个粗糙模型，所以**绝对值不可靠、相对比较才有意义** ——
    # 这正是把结果当成「换个方向能省多少」而不是「一定要这么多支撑」的原因。
    if metrics.support is not None and metrics.support.ratio_pct >= 30.0:
        candidates = orientation_candidates(
            points, triangle_count, metrics, active.max_overhang_deg
        )
        if candidates:
            # 比较必须**同口径**：候选方向用的是便宜的棱柱法，
            # 那就拿「当前方向的棱柱值」去比，而不是拿射线精化后的那个数。
            # 否则是拿两把尺子量出来的数比大小，结论没有意义。
            areas, normals, centroids = _face_stats(points, triangle_count)
            baseline, _ = (
                _support_for_orientation(areas, normals, centroids, active.max_overhang_deg)
                if areas
                else (0.0, 0.0)
            )
            best = min(candidates, key=lambda item: float(item["support_volume_mm3"]))
            if baseline > 0 and float(best["support_volume_mm3"]) < baseline * 0.8:
                metrics.orientation_hint = best

    _collect_issues(metrics, active)
    # 内部空隙实测：只在源码声明了间隙/夹口时才测（为什么放在这里见 analyze 的 docstring）。
    # 放到所有测量都定稿之后：本段要用 bbox/壁厚等已完成的结果，而追加的 issue 行
    # 也需要排在其它实测问题后面（用户从上往下读就是这个优先级）。
    # 测不出东西本身就是要报的缺陷（那一刀完全没切到），因此把结论也进 issues：
    # 否则「程序测量未发现明显几何问题」这句话会继续骗人（实测的假绿就是这么来的）。
    if declared_gaps:
        wanted = [float(mm) for _label, mm, _name in declared_gaps if float(mm) > 0]
        if wanted:
            metrics.declared_gaps = list(declared_gaps)
            metrics.interior_gaps = measure_interior_gaps(
                points, triangle_count, metrics, min_gap_mm=min(wanted)
            )
            gaps = metrics.interior_gaps
            if gaps is not None and gaps.max_mm < min(wanted) * 0.7:
                solid_axis = gaps.solid_axis
                metrics.issues.append(
                    f"源码声明了间隙/夹口（最小 {min(wanted):g}mm），但网格实测三轴最大被夹空档只有 "
                    f"{gaps.max_mm:.2f}mm（沿 {'XYZ'[solid_axis]} 最长连续材料 "
                    f"{gaps.longest_material[solid_axis]:.1f}mm）—— 那一刀很可能没切到实体"
                )
    return metrics


def _count_parts(
    points: list[tuple[float, float, float]], face_count: int
) -> tuple[int, int, list[dict[str, object]]]:
    """数一数模型由几个互不相连的实体组成（并查集，顶点按坐标焊接）。

    返回 (零件数, 除最大零件外还有几个, 散件明细)。
    为什么要做：AI 极容易把「一个成品」做成「一个夹子 + 一个桶」这样的零件组合。
    用户在预览里一眼就看出「这不是我要的东西」，但包围盒、体积、封闭性全部正常。
    实测用户的桌面垃圾桶需求正是这样翻车的（得到「桌面卡口 + 普通垃圾桶」）。

    STL 里同一个顶点会被重复存储，所以先按 1e-4mm 精度量化去重再合并 ——
    否则每个三角面都是独立零件，数出来等于面数。

    **封闭内腔不算零件**：一个密封空心件（比如密封浮球）的曲面是两块 ——
    外壳和内腔面，它们在拓扑上确实互不相连，但它们属于同一个实体。
    光看「连通分量个数」会把这种正常模型报成「2 个零件」，实测确实会误报。
    判别方法用的是面朝向：内腔面的法线指向空腔内部，它自己围出的有向体积为**负**，
    而实体零件的为正。为稳妥再叠一条几何条件 —— 该分量必须被另一个分量的
    包围盒完全包住 —— 两条同时成立才认定为内腔面。
    """
    if face_count <= 0:
        # 必须返回**三个**值（零件数 / 散件数 / 散件明细）：调用方按三元组解包。
        # 以前这里只回两个，空网格时直接 ValueError 崩在分析步里。
        return 0, 0, []

    parent: dict[tuple[int, int, int], tuple[int, int, int]] = {}

    def find(node: tuple[int, int, int]) -> tuple[int, int, int]:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:  # 路径压缩
            parent[node], node = root, parent[node]
        return root

    def union(a: tuple[int, int, int], b: tuple[int, int, int]) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    scale = 10_000.0  # 1e-4 mm
    # 同一个顶点会被好几个面共用（水密网格里通常是 6 个），
    # 而「坐标 → 量化键」这一步和面无关。缓存下来，一座 100 万面的模型
    # 就能把 900 万次 (乘 + round + int) 降到大约几十万次。
    # 量化表达式逐字未改，所以焊接结果与以前完全一致。
    quantized: dict[tuple[float, float, float], tuple[int, int, int]] = {}
    triangles: list[tuple[tuple[int, int, int], ...]] = []
    for face in range(face_count):
        base = face * 3
        keys = []
        for corner in range(3):
            point = points[base + corner]
            key = quantized.get(point)
            if key is None:
                key = (
                    int(round(point[0] * scale)),
                    int(round(point[1] * scale)),
                    int(round(point[2] * scale)),
                )
                quantized[point] = key
            parent.setdefault(key, key)
            keys.append(key)
        union(keys[0], keys[1])
        union(keys[1], keys[2])
        triangles.append((keys[0], keys[1], keys[2]))

    # 每个零件用「三角面数」衡量大小，避免只连到一个顶点的碎片被当成主体；
    # 同时累计有向体积与包围盒，用于识别「内腔面不是零件」。
    sizes: dict[tuple[int, int, int], int] = {}
    volumes: dict[tuple[int, int, int], float] = {}
    boxes: dict[tuple[int, int, int], list[float]] = {}
    for index, keys in enumerate(triangles):
        root = find(keys[0])
        sizes[root] = sizes.get(root, 0) + 1
        base = index * 3
        ax, ay, az = points[base]
        bx, by, bz = points[base + 1]
        cx, cy, cz = points[base + 2]
        volumes[root] = volumes.get(root, 0.0) + (
            ax * (by * cz - bz * cy)
            - ay * (bx * cz - bz * cx)
            + az * (bx * cy - by * cx)
        )
        box = boxes.get(root)
        if box is None:
            box = [ax, ay, az, ax, ay, az]
            boxes[root] = box
        # 三个顶点都要算进去：只取每个面的第一个顶点会漏掉大量轮廓点，
        # 包围盒偏小会让「内腔面被外壳包住」这条判据失效。
        for px, py, pz in ((ax, ay, az), (bx, by, bz), (cx, cy, cz)):
            if px < box[0]:
                box[0] = px
            if py < box[1]:
                box[1] = py
            if pz < box[2]:
                box[2] = pz
            if px > box[3]:
                box[3] = px
            if py > box[4]:
                box[4] = py
            if pz > box[5]:
                box[5] = pz

    # 少于 4 个面的连通块几乎不可能是实体（一个四面体最少 4 面），视为碎片
    candidates = [root for root, count in sizes.items() if count >= 4]
    if not candidates:
        return 0, 0, []

    margin = 0.05  # mm
    parts: list[tuple[int, tuple[int, int, int]]] = []
    for root in candidates:
        volume6 = volumes.get(root, 0.0)
        box = boxes[root]
        swallowed = False
        if volume6 < 0:
            for other in candidates:
                if other == root:
                    continue
                outer = boxes[other]
                inner_inside_outer = (
                    outer[0] - margin <= box[0]
                    and outer[1] - margin <= box[1]
                    and outer[2] - margin <= box[2]
                    and outer[3] + margin >= box[3]
                    and outer[4] + margin >= box[4]
                    and outer[5] + margin >= box[5]
                )
                # 必须是真的「被包住」，不能是两边互相包含（等大或重叠）
                outer_inside_inner = (
                    box[0] - margin <= outer[0]
                    and box[1] - margin <= outer[1]
                    and box[2] - margin <= outer[2]
                    and box[3] + margin >= outer[3]
                    and box[4] + margin >= outer[4]
                    and box[5] + margin >= outer[5]
                )
                if inner_inside_outer and not outer_inside_inner:
                    swallowed = True
                    break
        if not swallowed:
            parts.append((sizes[root], root))

    if not parts:
        return 0, 0, []
    parts.sort(key=lambda item: item[0], reverse=True)

    # 主体之外的部分附上位置/尺寸/体积，供 AI 定位到具体结构
    details: list[dict[str, object]] = []
    for faces, root in parts[1:]:
        box = boxes[root]
        details.append(
            {
                "center": [
                    round((box[0] + box[3]) / 2, 1),
                    round((box[1] + box[4]) / 2, 1),
                    round((box[2] + box[5]) / 2, 1),
                ],
                "size": [
                    round(box[3] - box[0], 1),
                    round(box[4] - box[1], 1),
                    round(box[5] - box[2], 1),
                ],
                "volume_mm3": round(abs(volumes.get(root, 0.0)) / 6.0, 1),
                "faces": faces,
            }
        )
    # 大的排前面：散件越大越可能是「一整块结构忘了接上」
    details.sort(key=lambda item: float(item["volume_mm3"]), reverse=True)
    return len(parts), len(parts) - 1, details


def _slice_loops(
    points: list[tuple[float, float, float]],
    face_count: int,
    z: float,
    *,
    tol: float = 1e-3,
) -> list[list[tuple[float, float]]] | None:
    """用一个水平面 z=常数 切模型，返回截面上闭合环的列表。

    做法是标准的「三角面与平面求交 → 把线段接成环」：
    每条被切到的三角面贡献一条线段，把所有线段的端点按坐标焊接后，
    每个端点恰好应当只连两条线段；顺着走下去就得到闭合环。

    端点度数不等于 2 说明网格局部不干净（退化面、非流形），
    这时返回 None 表示「这一层不可信」，宁可不报也不要误报。
    """
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for face in range(face_count):
        base = face * 3
        ax, ay, az = points[base]
        bx, by, bz = points[base + 1]
        cx, cy, cz = points[base + 2]

        zs = (az, bz, cz)
        if min(zs) >= z or max(zs) <= z:
            continue  # 整块在平面之上或之下（含共面），不产生交线

        # 顶点恰好落在平面上时统一当作「在上方」，避免同一条边被算两次
        above = (az >= z, bz >= z, cz >= z)
        xs = (ax, bx, cx)
        ys = (ay, by, cy)
        hits: list[tuple[float, float]] = []
        for i, j in ((0, 1), (1, 2), (2, 0)):
            if above[i] == above[j]:
                continue
            t = (z - zs[i]) / (zs[j] - zs[i])
            hits.append((xs[i] + (xs[j] - xs[i]) * t, ys[i] + (ys[j] - ys[i]) * t))
        if len(hits) < 2:
            continue
        p, q = hits[0], hits[1]
        if abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol:
            continue  # 退化成一点
        segments.append((p, q))

    if not segments:
        return []

    # 端点焊接：不能用「按网格取整」去重 —— CSG 出来的模型里，
    # 一个图元的边常常落在另一个图元顶点的 1~3µm 之外，取整会把这两个点分到
    # 相邻格子里，于是接环时出现「度数为 1」的断头（实测真实模型上就会发生）。
    # 改成空间哈希 + 邻域（3×3 格）搜索，把 eps 内的点真正合并成一个节点。
    eps = 1e-2  # 10µm：远小于任何有意义的打印特征，又足以容纳网格离散误差
    eps2 = eps * eps
    cell = eps
    buckets: dict[tuple[int, int], list[int]] = {}
    nodes: list[tuple[float, float]] = []
    node_of_segment: list[tuple[int, int]] = []

    def node_index(point: tuple[float, float]) -> int:
        cx = int(math.floor(point[0] / cell))
        cy = int(math.floor(point[1] / cell))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for existing in buckets.get((cx + dx, cy + dy), ()):
                    ex, ey = nodes[existing]
                    if (ex - point[0]) ** 2 + (ey - point[1]) ** 2 <= eps2:
                        return existing
        index = len(nodes)
        nodes.append(point)
        buckets.setdefault((cx, cy), []).append(index)
        return index

    for p, q in segments:
        ip, iq = node_index(p), node_index(q)
        if ip == iq:
            continue  # 焊完之后退化成一点
        node_of_segment.append((ip, iq))

    if not node_of_segment:
        return []

    neighbours: dict[int, list[int]] = {}
    for ip, iq in node_of_segment:
        neighbours.setdefault(ip, []).append(iq)
        neighbours.setdefault(iq, []).append(ip)

    if any(len(v) != 2 for v in neighbours.values()):
        return None

    loops: list[list[tuple[float, float]]] = []
    # 按「无向边」去重：同一条边只属于一个环，两个方向都走一遍会把同一个环
    # 收成两条（一条顺时针一条逆时针），后面按圈数嵌套判断就会全部重复计数。
    visited: set[tuple[int, int]] = set()

    def edge_key(a: int, b: int) -> tuple[int, int]:
        return (a, b) if a <= b else (b, a)

    for start in neighbours:
        for second in neighbours[start]:
            if edge_key(start, second) in visited:
                continue
            loop: list[tuple[float, float]] = []
            current, previous = start, None
            ok = True
            while True:
                loop.append(nodes[current])
                nxt = None
                for candidate in neighbours[current]:
                    if candidate != previous and edge_key(current, candidate) not in visited:
                        nxt = candidate
                        break
                if nxt is None:
                    ok = False
                    break
                visited.add(edge_key(current, nxt))
                previous, current = current, nxt
                if current == start:
                    break
                if len(loop) > len(neighbours) + 2:
                    ok = False
                    break
            if ok and len(loop) >= 3:
                loops.append(loop)
    return loops


def _signed_area(loop: list[tuple[float, float]]) -> float:
    """鞋带公式。只用来量面积大小，不用于判断内外（环的绕向不可靠）。"""
    total = 0.0
    for i, (x1, y1) in enumerate(loop):
        x2, y2 = loop[(i + 1) % len(loop)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _point_in_polygon(point: tuple[float, float], loop: list[tuple[float, float]]) -> bool:
    """射线法判断点是否在多边形内部（环的顶点顺序无关）。"""
    x, y = point
    inside = False
    for i, (x1, y1) in enumerate(loop):
        x2, y2 = loop[(i + 1) % len(loop)]
        if (y1 > y) != (y2 > y):
            cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if cross > x:
                inside = not inside
    return inside


# 切片层数：太少会漏掉细长的错误特征，太多则在大模型上白花时间
_ISLAND_LEVELS = 4
# 面数上限：切片是纯 Python 循环，超过这个量级就跳过（大模型本来也不会是这种错误）
_ISLAND_MAX_FACES = 400_000


def _detect_cavity_islands(
    points: list[tuple[float, float, float]],
    face_count: int,
    metrics: MeshMetrics,
) -> list[tuple[float, int]]:
    """找出「被空腔完全包围的材料块」出现在哪些高度。

    判据用的是截面上闭合环的**嵌套层数**（奇偶规则），而不是环的绕向：
    STL 的环绕向要额外保证三角面朝向一致，实测不可靠；而「这个环被几个
    别的环套住」是纯几何事实 ——
      0 层 = 材料外轮廓，1 层 = 空腔，2 层 = **空腔里的材料孤岛**，3 层 = 更内层的空腔……
    所以嵌套层数为偶数且 ≥ 2 的环，就是「本该是空的腔体里长出来的东西」。

    这正是「外壁加强筋飘进桶里」那类缺陷的几何指纹 —— 模型整体仍然只有一个
    连通实体，所以零件数检查看不到它。

    只报告、不直接判死：有些设计确实会在容器内部做凸台（比如底部螺柱）。
    程序给出「哪一层、有几块」，由 AI 复核判断是不是设计意图。
    """
    if face_count <= 0 or face_count > _ISLAND_MAX_FACES:
        return []
    z_min, z_max = metrics.bbox_min[2], metrics.bbox_max[2]
    height = z_max - z_min
    if height <= 1.0:
        return []

    found: list[tuple[float, int]] = []
    for index in range(1, _ISLAND_LEVELS + 1):
        # 偏移一个非整数增量，避开顶点正好落在切面上的退化情形
        z = z_min + height * index / (_ISLAND_LEVELS + 1) + 0.137
        if z >= z_max:
            continue
        loops = _slice_loops(points, face_count, z)
        if not loops:
            continue
        # 只保留有实际面积的环：退化成一个点的环会让嵌套判断出错
        real = [lp for lp in loops if _signed_area(lp) > 0.01]
        if len(real) < 2:
            continue
        depths = []
        for i, loop in enumerate(real):
            depth = 0
            for j, other in enumerate(real):
                if i != j and _point_in_polygon(loop[0], other):
                    depth += 1
            depths.append(depth)
        count = sum(1 for d in depths if d >= 2 and d % 2 == 0)
        if count:
            found.append((z, count))
    return found


@dataclass
class WallThickness:
    """射线法测出来的壁厚分布。

    为什么必须真的去测，而不是看包围盒：旧版把「包围盒最小边」当成壁厚，
    对**空心容器完全失效** —— 实测的桌面垃圾桶是 110×186×110mm、壁厚 2.4mm，
    包围盒最小边 110mm，于是无论壁做得多薄都不会报警。
    而容器恰好是这个软件最主要的用途。
    """

    min_mm: float
    p05_mm: float
    median_mm: float
    samples: int
    # 最薄的若干处：([x, y, z], 厚度mm)。给出坐标才能告诉 AI 具体改哪里。
    thinnest: list[tuple[tuple[float, float, float], float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "min_mm": round(self.min_mm, 2),
            "p05_mm": round(self.p05_mm, 2),
            "median_mm": round(self.median_mm, 2),
            "samples": self.samples,
            "thinnest": [
                {"location": [round(v, 1) for v in point], "thickness_mm": round(t, 2)}
                for point, t in self.thinnest
            ],
        }


class _FaceGrid:
    """三角面的均匀网格索引：让「沿射线找交点」不用把每个面都试一遍。

    纯 Python 里逐面对几百条射线求交是 射线数×面数 次运算，
    两万面就是上百万次，慢到不可接受。把面按空间分桶之后，
    每条射线只走它经过的格子，候选面通常只剩几十个。
    """

    def __init__(self, points: list[tuple[float, float, float]], face_count: int, cell: float) -> None:
        self.cell = cell
        self.buckets: dict[tuple[int, int, int], list[int]] = {}
        # 跨很多格子的大三角面单独放一边，不做分桶。
        # 为什么必须这样：分桶会让「一个面」在每个覆盖到的格子里都存一份。
        # 一个横跨整个模型的大面（立方体的面就是），在 20×20×20 的网格里
        # 会被存 8000 次 —— 面上千个这样的面，内存和建表时间都会爆掉。
        # 放进 oversized 后每次射线多测这几个面，代价远小于桶爆炸。
        self.oversized: list[int] = []
        for face in range(face_count):
            base = face * 3
            xs = (points[base][0], points[base + 1][0], points[base + 2][0])
            ys = (points[base][1], points[base + 1][1], points[base + 2][1])
            zs = (points[base][2], points[base + 1][2], points[base + 2][2])
            x0, x1 = int(min(xs) // cell), int(max(xs) // cell)
            y0, y1 = int(min(ys) // cell), int(max(ys) // cell)
            z0, z1 = int(min(zs) // cell), int(max(zs) // cell)
            if (x1 - x0 + 1) * (y1 - y0 + 1) * (z1 - z0 + 1) > 64:
                self.oversized.append(face)
                continue
            for cx in range(x0, x1 + 1):
                for cy in range(y0, y1 + 1):
                    for cz in range(z0, z1 + 1):
                        self.buckets.setdefault((cx, cy, cz), []).append(face)

    def cell_of(self, point: tuple[float, float, float]) -> tuple[int, int, int]:
        cell = self.cell
        return (int(point[0] // cell), int(point[1] // cell), int(point[2] // cell))

    def candidates(self, cell: tuple[int, int, int]) -> list[int]:
        return self.buckets.get(cell, [])


class _LineCrossings:
    """固定高度 z 的水平线（沿 X）：扫一遍收齐表面交点，之后按 x 回答判据。

    为什么必须这样改（实测，不是推测）：旧写法每问一次「(x, y) 在不在材料里」
    都要从 x 重新走到模型最右端把所有交点数一遍 —— 而量一个孔要问 ≈50 次，
    搜一次孔轴要问几千次，代价 = 采样点数 × 射线路径长度。实测用户真实模型
    （5,346 面）**单个高度**就要 2.3~16.5s，7 个高度合计 38.9s；结果是时间预算
    把后面 6 个剖面高度全部饿死 —— 明明量得到却报「没量到」，又回到误判。

    关键观察：同一条线（y 固定）上的交点集合与要问多少个点无关。扫一遍把
    交点收齐排序，之后「这个点在材料里吗」「这个空洞有多宽」都退化成一次
    二分查找。搜索阶段每条 y 线会被问几十次，于是少算几十倍。

    奇偶口径与旧 `inside()` **完全一致**（同一个 `_ray_triangle`、同一个三角面
    去重、同一个采样抖动），所以这是「少算」而不是「改判据」。
    """

    def __init__(
        self,
        points: list[tuple[float, float, float]],
        grid: _FaceGrid,
        z: float,
        x_lo: float,
        x_hi: float,
    ) -> None:
        self._points = points
        self._grid = grid
        # 采样线稍微抬一点、挪一点：正好落在顶点/棱上时交点会被数两次或漏掉，
        # 奇偶就反了（实测把「6mm 实心底」读成「从 z=0 就是空的」）。
        self._z = z + 0.0137
        self._x_lo = x_lo
        self._x_hi = x_hi
        self._cache: dict[float, list[float]] = {}

    def _sweep(self, yy: float) -> list[float]:
        """这条线上所有表面交点的 x（升序）。"""
        points, grid = self._points, self._grid
        origin = (self._x_lo, yy, self._z)
        xs: list[float] = []
        seen: set[int] = set()

        def consider(other: int) -> None:
            if other in seen:
                return
            seen.add(other)
            base = other * 3
            t = _ray_triangle(
                origin, (1.0, 0.0, 0.0), points[base], points[base + 1], points[base + 2]
            )
            if t is not None and t > 1e-6:
                x = self._x_lo + t
                if x <= self._x_hi:
                    xs.append(x)

        for other in grid.oversized:
            consider(other)
        visited: set[tuple[int, int, int]] = set()
        travelled = 0.0
        limit = (self._x_hi - self._x_lo) + 1.0
        while travelled <= limit:
            cell = grid.cell_of((self._x_lo + travelled, yy, self._z))
            if cell not in visited:
                visited.add(cell)
                for other in grid.candidates(cell):
                    consider(other)
            travelled += grid.cell
        xs.sort()
        return xs

    def crossings(self, y: float) -> list[float]:
        yy = y + 0.0173
        got = self._cache.get(yy)
        if got is None:
            got = self._sweep(yy)
            self._cache[yy] = got
        return got

    def _interval(self, x: float, y: float) -> tuple[int, int]:
        """x 落在那一段：返回 (不大于 x 的交点个数 j, 交点总数 n)。"""
        xs = self.crossings(y)
        return bisect.bisect_right(xs, x + 1e-6), len(xs)

    def inside(self, x: float, y: float) -> bool:
        """这个点在材料里吗（它右侧的交点数是奇数）—— 与旧 `inside()` 同一口径。"""
        j, n = self._interval(x, y)
        return (n - j) % 2 == 1

    def void_span(self, x: float, y: float) -> tuple[float, float] | None:
        """x 所在**空洞**区间的左右墙面位置；在材料里、或那是模型外空气时 None。

        「某一侧没有交点」必须是空气而不是孔：旧实现靠「走不出 window 就返回 None」
        间接得到这个结论，这里直接判，不靠运气的采样范围。
        """
        j, n = self._interval(x, y)
        if (n - j) % 2 == 1:
            return None  # 在材料里
        if j == 0 or j >= n:
            return None  # 左边或右边没有墙面 → 那不是孔
        xs = self.crossings(y)
        return xs[j - 1], xs[j]

def _ray_triangle(
    origin: tuple[float, float, float],
    direction: tuple[float, float, float],
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
) -> float | None:
    """Möller–Trumbore 求交，返回射线参数 t（正数表示在前方），不相交返回 None。"""
    e1 = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    e2 = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    px = direction[1] * e2[2] - direction[2] * e2[1]
    py = direction[2] * e2[0] - direction[0] * e2[2]
    pz = direction[0] * e2[1] - direction[1] * e2[0]
    det = e1[0] * px + e1[1] * py + e1[2] * pz
    if -1e-12 < det < 1e-12:
        return None  # 射线与三角面平行
    inv = 1.0 / det
    tx, ty, tz = origin[0] - a[0], origin[1] - a[1], origin[2] - a[2]
    u = (tx * px + ty * py + tz * pz) * inv
    if u < 0.0 or u > 1.0:
        return None
    qx = ty * e1[2] - tz * e1[1]
    qy = tz * e1[0] - tx * e1[2]
    qz = tx * e1[1] - ty * e1[0]
    v = (direction[0] * qx + direction[1] * qy + direction[2] * qz) * inv
    if v < 0.0 or u + v > 1.0:
        return None
    t = (e2[0] * qx + e2[1] * qy + e2[2] * qz) * inv
    return t if t > 0 else None


def measure_wall_thickness(
    points: list[tuple[float, float, float]],
    face_count: int,
    metrics: MeshMetrics,
    *,
    samples: int = 400,
    seed: int = 42,
) -> WallThickness | None:
    """射线法测壁厚：从表面采样点朝材料内部打一条射线，量到对面表面的距离。

    这是**唯一**能反映「容器壁有多厚」的测法。旧的包围盒法对空心件完全无效
    （桌面垃圾桶 110×186×110、壁厚 2.4mm，包围盒最小边 110mm，永远不报警）。

    做法照 DfAM 检查的通行做法（earthtojake/text-to-cad 的 dfam-check）：
      1. 按面积加权随机采样三角面（大面被采到的概率更高，采样才代表真实表面）；
      2. 从采样点往材料内部挪一点点，沿**面法线反向**打射线；
      3. 第一个交点就是对面表面，距离即该处局部厚度；
      4. 报 min / p05 / 中位数，并给出最薄处的坐标。

    为什么同时报 p05 和中位数：`min` 可能是采样离群值（噪声三角面造成的假薄壁），
    而 p05 偏低说明**有一片区域**真的偏薄 —— 比单个 min 更值得报警。

    只测量、不下结论：判定交给调用方，因为阈值来自用户选的打印机与工艺。
    """
    if face_count <= 0:
        return None
    diag = math.sqrt(sum(v * v for v in metrics.bbox))
    if diag <= 1e-6:
        return None

    areas: list[float] = []
    normals: list[tuple[float, float, float]] = []
    centroids: list[tuple[float, float, float]] = []
    for face in range(face_count):
        base = face * 3
        ax, ay, az = points[base]
        bx, by, bz = points[base + 1]
        cx, cy, cz = points[base + 2]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length <= 1e-12:
            continue  # 退化面没有法线，无法定义「往材料内部」
        areas.append(length / 2.0)
        normals.append((nx / length, ny / length, nz / length))
        centroids.append(((ax + bx + cx) / 3.0, (ay + by + cy) / 3.0, (az + bz + cz) / 3.0))

    total_area = sum(areas)
    if total_area <= 0 or not areas:
        return None

    grid = _FaceGrid(points, face_count, max(diag / 20.0, 1.0))
    rng = random.Random(seed)
    count = min(samples, len(areas))

    # 面积加权采样：累积分布 + 二分查找，避免构造几十万项的权重列表
    cumulative: list[float] = []
    running = 0.0
    for area in areas:
        running += area
        cumulative.append(running)

    thicknesses: list[float] = []
    locations: list[tuple[float, float, float]] = []
    step = grid.cell / 2.0
    for _ in range(count):
        pick = rng.random() * total_area
        lo, hi = 0, len(cumulative) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if cumulative[mid] < pick:
                lo = mid + 1
            else:
                hi = mid
        origin = centroids[lo]
        nx, ny, nz = normals[lo]
        # 往材料内部挪一点点，避免射线立刻打中它自己所在的那个面
        start = (origin[0] - nx * 1e-4, origin[1] - ny * 1e-4, origin[2] - nz * 1e-4)
        direction = (-nx, -ny, -nz)

        best: float | None = None

        def consider(other: int) -> None:
            """测一条射线与某个三角面是否相交，维护最近命中。"""
            nonlocal best
            base = other * 3
            t = _ray_triangle(start, direction, points[base], points[base + 1], points[base + 2])
            if t is not None and t > 1e-4 and (best is None or t < best):
                best = t

        # 横跨很多格子的大面（立方体的面就是）不做分桶，每条射线单独测一次。
        # 它们数量极少（通常 0~12 个），但漏掉就会把「穿透大平面」当成没有交点。
        for other in grid.oversized:
            consider(other)

        visited: set[tuple[int, int, int]] = set()
        travelled = 0.0
        while travelled <= diag:
            probe = (
                start[0] + direction[0] * travelled,
                start[1] + direction[1] * travelled,
                start[2] + direction[2] * travelled,
            )
            cell = grid.cell_of(probe)
            if cell not in visited:
                visited.add(cell)
                for other in grid.candidates(cell):
                    consider(other)
            if best is not None and travelled > best:
                break  # 已越过最近交点，再走也不可能更近
            travelled += step

        if best is None:
            continue
        # 比对角线还长的「厚度」说明射线是从开口穿出去了，不是壁厚
        if 1e-3 < best < diag:
            thicknesses.append(best)
            locations.append(start)

    if not thicknesses:
        return None

    order = sorted(range(len(thicknesses)), key=lambda i: thicknesses[i])
    values = [thicknesses[i] for i in order]
    p05 = values[max(0, int(round(0.05 * (len(values) - 1))))]
    median = values[len(values) // 2]
    thinnest = [(locations[order[i]], values[i]) for i in range(min(5, len(values)))]
    return WallThickness(
        min_mm=values[0],
        p05_mm=p05,
        median_mm=median,
        samples=len(values),
        thinnest=thinnest,
    )


def measure_hole_diameter(
    points: list[tuple[float, float, float]],
    face_count: int,
    z: float,
    *,
    window: float = 15.0,
    center: tuple[float, float] | None = None,
    deadline: float | None = None,
    grid: Any | None = None,
) -> float | None:
    """在给定高度量孔的内径：从**孔轴线**向两侧走，直到撞上材料（射线奇偶法）。

    为什么并入 mesh.py（HANDOVER P0）：改孔径时若源码把孔写成表达式
    （如 d = outer - 2*wall），参数核对根本看不到孔变没变；而实测几何明明能量。
    旧实现只存在于测试脚本 verify_modify_matrix，产品判据用不上。

    为什么用射线而不是找顶点（实测踩过）：孔是一次 linear_extrude 出来的，
    孔壁只有最上/最下两圈顶点，采样高度落在中间时一个顶点都找不到 ——
    一个完全正确的 Ø8 孔被量成 None，于是报「孔径没改到」。

    为什么从孔轴往外走、而不是「取最靠中的空点差」：后者量到的是**半径**
    （实测 Ø10 量成 5.0、Ø8 量成 4.0），差一个因子 2 却看起来「有数」。

    返回 None 表示该高度没有找到孔（轴线上是材料，或走出 window 仍未撞上材料）。

    --- 孔轴必须**找**，不能假设在 (0,0) ---
    旧实现直接从 `(0, 0)` 出发走射线，于是只对「**同心**的竖直孔」成立。
    实测两个都是 Ø10 的孔（`cube(center=true)` 减 `cylinder(d=10)`）：
      * 孔在中心   → 量得 9.8 ✅
      * 孔偏心 12mm → 量得 **None** ❌（(0,0) 落在实体里，直接判「这个高度没有孔」）
    后果很重：判据据此说「孔径没改到」，而 AI 其实改对了 —— 用户的正确修改
    被判失败，正是本项目最不想出现的那类误判。偏心孔在真实模型里很常见
    （`translate([x,y,0]) cylinder(...)`、面板上的安装孔、阵列孔都是）。
    现在先**搜出孔轴**再量：居中孔算出来的轴仍是 (0,0)，结果与旧实现逐位一致
    （不回归），偏心孔则能正确定位。

    --- 锚点也不能只给 (0,0)：模型本身常常不以原点为中心 ---
    上面那一修只照到了「同心孔」这一半。实测另一半的形状：
    **完全同心的孔 + 从原点角上长出来的模型**（`cube([60,40,8])` 不写 center，
    孔在包围盒中心 (30,20)）—— (0,0) 是**顶角**，在模型外，走不出材料对，
    于是旧实现同样返回 None。拿真实量级的 3,616 面模型实测：
        当前代码（只试 (0,0)）  → None，0.006s（直接碰着面数上限就退出）
        改试包围盒中心       → 9.8，0.110s
    所以快路径现在**多试一个锚点**：(0,0)（旧口径，逐位不变）与包围盒中心。
    一个锚点只是 300 次射线（~0.1s 量级），比整段搜索便宜一个数量级，
    而它把「能测」的范围从 0% 拉回了真实模型的绝大多数。

    `center` 是模型在 XY 上的包围盒中心（调用方已有 bbox，不必重算）。
    `deadline` 是 `time.monotonic()` 意义上的绝对截止时刻：超预算就如实
    返回 None（作业是前台跑的，宁可不报也不把界面卡住）。
    `grid` 让调用方把面网格复用给多次同高度外的测量（analyze 要测 7 个高度）。

    --- 代价的形状：交点按**线**缓存，不是按采样点重走 ---
    墙的位置不再靠「按 0.1mm 一步步走」找，而是把整条水平线的表面交点扫
    一遍存下来（`_LineCrossings`），之后每个查询只是一次二分。一个锚点从
    ≈50 次全程射线降到 1 次扫线；搜索阶段几十条 y 线各扫一次，而不是每个
    采样点扫一次。实测 23,296 面：单高度 1.2~2.1s → 0.1s 量级，7 个高度能全部
    走完而不是被预算饿死后 6 个。副产品：墙面取的是**精确交点**而不是
    「最后一个还空的采样点」，Ø10 不再系统性读成 9.8。
    """
    if face_count <= 0:
        return None
    if deadline is None:
        deadline = time.monotonic() + _HOLE_SEARCH_BUDGET_S
    if grid is None:
        grid = _FaceGrid(points, face_count, 1.0)
    # 一次遍历把 XY 范围拿全（扫线要从几何左边开始，(0,0) 在不在模型内也要用它判）
    x_lo = y_lo = math.inf
    x_hi = y_hi = -math.inf
    for px, py, _pz in points[: face_count * 3]:
        if px < x_lo:
            x_lo = px
        if px > x_hi:
            x_hi = px
        if py < y_lo:
            y_lo = py
        if py > y_hi:
            y_hi = py
    if not math.isfinite(x_lo) or x_hi <= x_lo:
        return None

    lines = _LineCrossings(points, grid, z, x_lo - 1.0, x_hi + 1.0)

    def hole_at(ax: float, ay: float) -> float | None:
        """把 (ax, ay) 当成孔轴试一下：命中就返回实测内径，否则 None。"""
        if time.monotonic() > deadline or lines.inside(ax, ay):
            return None
        box = lines.void_span(ax, ay)
        if box is None:
            return None
        # 两侧都必须在 window 内撞上墙面，否则那是凹槽/开口而不是孔
        if ax - box[0] > window or box[1] - ax > window:
            return None
        width = box[1] - box[0]
        # 小于 _HOLE_MIN_DIAMETER_MM 不算孔（锚点落在重合面/刀刃之间的微缝隙）；
        # 不直接返回而是**继续走**：真正偏心的孔还得靠后面的搜索去找。
        if width < _HOLE_MIN_DIAMETER_MM:
            return None
        return width

    # --- 快路径：先试已知的候选轴位，不需要任何搜索 ---
    # (0,0) 排在第一个，所以**以原点为中心的模型**结果与以前逐位相同（不回归）；
    # 但它落在模型外时直接跳过（那是个顶角，不可能量出孔）。
    anchors: list[tuple[float, float]] = []
    if x_lo + 0.5 <= 0.0 <= x_hi - 0.5 and y_lo + 0.5 <= 0.0 <= y_hi - 0.5:
        anchors.append((0.0, 0.0))
    if center is not None:
        cxy = (float(center[0]), float(center[1]))
        if cxy not in anchors:
            anchors.append(cxy)
    for ax, ay in anchors:
        if time.monotonic() > deadline:
            return None
        got = hole_at(ax, ay)
        if got is not None:
            return got

    # --- 慢路径：锚点都在材料里（或只量到微缝隙），去搜 ---
    # 代价按**时间**封顶而不是按面数：测得完就报实测值，测不完就返回 None。
    # 判据只能来自实测，宁可"没测出来"也不能给猜的数。
    axis = _find_hole_axis(
        lines,
        x_lo=x_lo, x_hi=x_hi, y_lo=y_lo, y_hi=y_hi, window=window, deadline=deadline,
    )
    if axis is None:
        return None
    return hole_at(axis[0], axis[1])


def _find_hole_axis(
    lines: _LineCrossings,
    *,
    x_lo: float,
    x_hi: float,
    y_lo: float,
    y_hi: float,
    window: float,
    deadline: float,
) -> tuple[float, float] | None:
    """在给定高度找出孔的轴线位置（孔内空点聚类后取形心）。

    为什么需要：孔不一定在模型中心（见 `measure_hole_diameter` 的说明）。
    做法：在包围盒内按粗步长采样，凡是「自身是空、且两侧墙面都在 window 内」
    的点就算孔内点；把这些点按粗网格做连通聚类，取**最大的一簇**的形心。
    取最大簇而不是全部平均：模型上常有好几处凹槽/缺口，只认最大的那个孔
    才不会被浅表的凹陷带偏。

    「空 + 两侧有墙」直接问 `_LineCrossings.void_span`：一次二分就拿精确墙面。
    旧写法是按 0.6mm 往左右各走一遍，每走一步重算一次「这个点右侧几个交点」——
    而同一个高度只有一条线要扫，走 50 步等于把同一条线的信息重复算 50 次。
    顺带修掉一个近似误差：薄于一个探测步长的墙旧写法会跨过去（当成更远的墙），
    现在取的是真实交点。

    采样分辨率保持不动（还是 ≤400 个点、粗步长 ≥0.5mm）：这一轮要证明的是
    「少算」能把代价降下来，不是「算得更细」。调分辨率必须另配实测代价曲线。

    代价必须封顶：时间花完就如实返回 **None**，不返回半程筛出来的簇 ——
    那样可能只扫到一个小孔而漏掉更大的那个，把「量到哪个」变成运气，
    而这套判据要拿来宣布「用户的改动没做到」。宁可 None 也不猜。
    """
    # 粗采样步长：保证采样点总数不超过上限，同时比最小孔径细得多。
    span = max(x_hi - x_lo, y_hi - y_lo, 1.0)
    max_samples = 400
    coarse = max(span / 40.0, 0.5)
    while True:
        nx = int((x_hi - x_lo) / coarse) + 1
        ny = int((y_hi - y_lo) / coarse) + 1
        if nx * ny <= max_samples or coarse >= span:
            break
        coarse *= 1.5

    void_pts: list[tuple[float, float]] = []
    x = x_lo
    while x <= x_hi:
        y = y_lo
        while y <= y_hi:
            if time.monotonic() > deadline:
                return None
            box = lines.void_span(x, y)
            if box is not None and x - box[0] <= window and box[1] - x <= window:
                # 自身是空还不够：两侧都得在 window 内有墙面，否则那是模型外侧
                # 空气（void_span 已经排掉「某一侧没有交点」）或贯穿的开口。
                void_pts.append((x, y))
            y += coarse
        x += coarse

    if not void_pts:
        return None

    # 粗网格连通聚类（8 邻域）。**所有**簇都留着，按大小依次验证（见下面的说明）：
    # 只取最大簇会被「假孔」抢位 —— 一个 Ø74 内腔顶上那段月牙形空档，采样点比
    # 旁边那个真 Ø10 孔的还多，于是最大簇是月牙，真孔一辈子轮不到。
    buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for px, py in void_pts:
        buckets.setdefault((int(px / coarse), int(py / coarse)), []).append((px, py))
    seen_cells: set[tuple[int, int]] = set()
    clusters: list[list[tuple[float, float]]] = []
    for cell in buckets:
        if cell in seen_cells:
            continue
        stack = [cell]
        seen_cells.add(cell)
        cluster: list[tuple[float, float]] = []
        while stack:
            cx, cy = stack.pop()
            cluster.extend(buckets.get((cx, cy), ()))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nb = (cx + dx, cy + dy)
                    if nb in buckets and nb not in seen_cells:
                        seen_cells.add(nb)
                        stack.append(nb)
        clusters.append(cluster)
    clusters.sort(key=len, reverse=True)

    for cluster in clusters:
        if time.monotonic() > deadline:
            return None  # 预算花完就 None：不把半程验过的簇当成结论
        axis = (
            sum(p[0] for p in cluster) / len(cluster),
            sum(p[1] for p in cluster) / len(cluster),
        )
        if _hole_prism_ok(lines, axis[0], axis[1], y_lo=y_lo, y_hi=y_hi, deadline=deadline):
            return axis
    return None


def _hole_prism_ok(
    lines: _LineCrossings,
    ax: float,
    ay: float,
    *,
    y_lo: float,
    y_hi: float,
    deadline: float,
) -> bool:
    """确认 (ax, ay) 那段「两侧有墙的空隙」是一段**孔**，而不是腔体顶上的月牙。

    为什么需要这一条（本机实测，不是推测）：Ø80 笔筒（内腔 Ø74、底座 5mm、
    底座正中一个 Ø10 排水孔）在 **z=15 这个根本没有孔的高度**上报出过孔径 **11.36**。
    搜索的准入条件是「自身是空 + 两侧墙面都在 window 内」，而贴着多边形腔壁的那条
    月牙**完全满足**它 —— 单看一条水平线，月牙两侧确实都是材料，2D 上无从分辨。
    后果是【问题 8】那句「要求 10mm，实测 11.36」：**一个做对了的改动被判失败**，
    还会驱动修复循环去改一个本来没坏的东西。

    区分办法用孔的本质：孔是**竖直棱柱**，沿 Y 离开轴线一点，宽度只按圆弧缓慢变化
    （圆孔在 ±d/3 处宽 0.87d）；月牙的宽度在同一距离上会直接翻倍或者根本没有空腔。
    所以再问两条线（同一个 `_LineCrossings` 对象换个 y，各一次扫线）验宽度稳定。

    代价：只在**慢路径**（已经付过一次搜索）每个候选簇 +1~2 次扫线，锚点快路径不受影响。
    判不准就返回 False（少报一个高度），绝不放行一个错数 —— 错数会直接对用户说假话，
    少报只是判据保持沉默。
    """
    box = lines.void_span(ax, ay)
    if box is None:
        return False  # 形心落在材料里（非凸簇、或根本没有空腔）
    width = box[1] - box[0]
    if width < _HOLE_MIN_DIAMETER_MM:
        return False
    off = max(width / 3.0, 0.5)
    checked = 0
    for y2 in (ay + off, ay - off):
        if not y_lo <= y2 <= y_hi:
            continue  # 这一侧已经出了模型包围盒，没什么可验的
        if time.monotonic() > deadline:
            return False
        span2 = lines.void_span(ax, y2)
        if span2 is None:
            return False
        width2 = span2[1] - span2[0]
        if not 0.5 * width <= width2 <= 2.0 * width:
            return False  # 宽度在 d/3 内就翻倍/消失 → 那是缝隙不是孔
        checked += 1
    return checked == 2  # 上下两条都必须在：只验一条等于没验


# 内部空隙扫描的面数上限与最大采样密度：纯 Python 射线扫描，必须给代价封顶。
# 超过面数上限就不测（只当「没测出来」，绝不拿猜测当结论）。
_GAP_MAX_FACES = 200_000
_GAP_MAX_LINES = 21
# 小于这个厚度的材料段视为网格噪声（共面/相切处的退化面）
_GAP_MIN_MATERIAL_MM = 1e-4

# 找孔轴的代价封顶（纯 Python 射线扫描，代价随面数与采样点**相乘**）。
#
# 为什么用**墙钟预算**而不是面数上限（实测数据，本机）：
#   旧上限 `_HOLE_AXIS_MAX_FACES = 1200` 是在探测步长还是 0.1mm 的那一版量出来的。
#   步长优化到 0.6mm 之后没人重取这个数 —— 于是它把「能测」的范围压到了
#   用户真实模型的 0%：本机 14 份真实项目 STL 的面数是 3,041 ~ 122,771，
#   **全部**超过 1200，也就是每一份真实模型的孔都是「量不到」。
#   而刚过线的那一件（1,202 面）解除上限后只花 0.19s 就量出了 Ø9.9。
# 优化后的实测代价曲线（单次整段搜索）：
#   1,216 面 → 0.17s    3,616 面 → 0.67s    9,616 面 → 3.16s   24,016 面 → 7.34s
#   （凸台分散的真实分布更便宜：10,820 面 → 1.26s，30,980 面 → 2.70s）
# 锚点快路径比这个便宜得多（粗探+二分后 5,056 面单次只 0.12s）。
# 按时间封顶比按面数封顶诚实：**测得完就报实测值，测不完就如实返回 None**，
# 不会因为一个过期的常数把本来 0.2 秒能测出来的模型判成「没测出来」。
_HOLE_SEARCH_BUDGET_S = 0.45   # 单次找孔轴的墙钟预算
_HOLE_PHASE_BUDGET_S = 2.2     # analyze 里 7 次孔径测量共用的总预算
#
# 2.2s 这个数是照着实测代价定的：一个锚点 ≈ 16 次射线（5,056 面时 0.12s），
# 7 个高度都要试锚点，再加上中部那一次可能的整段搜索。
# 取小了会把**后面几个剖面高度**饿死（它们本来 0.1s 就能量到），
# 取大了会让复杂件的分析变慢 —— 分析跑在前台作业里，界面会冻。
#
# `analyze` 会在中部高度先做一次（可能付钱搜索），剩下的剖面高度共用同一个
# 截止时间：预算花完就返回 None，绝不把作业卡住（前台作业，界面会冻）。

# 小于这个直径的「孔」不当孔看（mm）。为什么要这一条（实测踩到）：
# 用户的真实模型 5,346 面上报过 **孔径 = 0.02mm**。那不是孔，是锚点刚好
# 落在两个重合面/刀刃之间那一丝网格缝隙里；而二分细化把精度做到 0.02mm，
# 恰好让这个假读数从前的「0.0」变成了一个**看起来像测量值**的数。
# 危害具体：这个数会进 `to_dict()`/AI 摘要，也会出现在【问题 8】那句
# 「实测剖面为 0.02、……」里 —— 对一个按 0.2mm 喷嘴打印的产品，
# 这句话本身就是假的。而 0.5mm 以下根本不可能是用户点名的孔（最小打印
# 孔径在毫米级），宁可走搜索也不能拿一个微噪声当结论。
_HOLE_MIN_DIAMETER_MM = 0.5

# 剖面高度：不能**只**按比例取。本项目的注释早就写着「一个模型可能同时有腔体和
# 底部小孔，只量中部会把腔体直径当成孔径」，但旧的六个高度是
# (0.15, 0.3, 0.45, 0.6, 0.75, 0.9) —— **最低的也在 15% 处**。
# 实测 Ø80 笔筒（总高 100、底座 5mm、底座正中 Ø10 排水孔）：
#   孔只存在于 z=0~5，而最低探针在 z=15 → 六个高度全部量不到那个孔。
# 底座上的排水孔/走线孔/安装孔就是这个形状，所以探针里必须有**贴底/贴顶的绝对高度**。
# 总高度数不变（还是 6 个），代价与旧剖面同量级（本机实测：31k 面 1.28s → 1.3~1.5s）。
_HOLE_PROFILE_PLAN: tuple[tuple[str, float], ...] = (
    ("lo", 0.6),
    ("lo", 2.5),
    ("frac", 0.3),
    ("frac", 0.5),
    ("frac", 0.7),
    ("hi", 0.6),
)


def _hole_profile_heights(z_lo: float, z_hi: float) -> list[float]:
    """按 `_HOLE_PROFILE_PLAN` 把六个探针高度算出来（自底向上）。

    贴底/贴顶的探针按**绝对毫米数**取（这样才能照到薄底座里的孔），但极薄的
    模型（总高几毫米）不能让它们跑到中面以外，所以各自封顶在总高的 20%。
    """
    height = max(z_hi - z_lo, 0.0)
    band = min(height * 0.2, 4.0)
    out: list[float] = []
    for kind, value in _HOLE_PROFILE_PLAN:
        if kind == "lo":
            out.append(z_lo + min(value, band))
        elif kind == "hi":
            out.append(z_hi - min(value, band))
        else:
            out.append(z_lo + height * value)
    return out


# 贴边探针离表面的距离：必须**贴边**（实体底/顶的第一层材料），否则边上方/下方的
# 内壁会把结果抬高。0.3mm 在 0.4 喷嘴的层高量级里安全，且对总高很薄的件会再收敛一层。
_END_PROBE_MM = 0.3
# 占比低于这个值就算“这一端是开的”。留出大孔的余量：本机 OpenSCAD 实测
# Ø80 壳 / 5mm 底板 / 底座正中 Ø10 排水孔 → 贴底 0.958、贴顶 0.085（顶部敞开），
# 而内腔从 z=0 开挖的开口管两端都只有 0.085 —— 封底与开底差一个数量级，
# 50% 落在中间，不是凑的数。
# （0.958 而不是按面积算的 0.875：`span` 取**每条扫线自己**的最外交点，Ø10 的
# 排水孔只落在正中那条线上，左右两条线整条都是料 → 1.0，三条取均值。）
_END_MIN_COVERAGE = 0.5
# 每一端自己的时间预算（实测最坏 0.05s/3 条线，给 0.35s 已是十倍余量）。
_END_BUDGET_S = 0.35


def measure_end_coverage(
    points: list[tuple[float, float, float]],
    face_count: int,
    metrics: MeshMetrics,
    *,
    at_top: bool = False,
    grid: Any | None = None,
    deadline: float | None = None,
    fracs: tuple[float, ...] = (0.5, 0.3, 0.7),
) -> float | None:
    """在贴底（或贴顶）那一层量“足迹里有多少是材料”（3 条扫线取均值，0~1）。量不了时 None。

    为什么只靠扫线而不是“看有没有底面三角形”：STL 里“底面有一圈环”与“底完整”
    无法用面数区分，而奇偶射线直接回答“这条线上哪几段是料”。

    为什么不拿包围盒体积/壁厚等现有指标凑：它们对开口管**全部正常**
    （实测见 `MeshMetrics.bottom_coverage` 那段注释）：31 份真模型交付版本里
    底是开口的占多数，而零件数/封闭/包围盒/壁厚/孔径剖面没一项报出事 —— 这正是
    本指标要堵的洞。

    为什么两端都要量：只看底会把**天生开口朝下**的件（盖子、罩壳、倒扣的壳）
    报成缺陷，而修复循环会真去把底封上 —— 那是把对的改成错的。有了另一端，
    “两端都开”才是无论怎么放都装不住东西的开口管 —— 只有这种情况才值得拦住修改。

    取 3 条 y 线而不是 1 条：只过中心那一条会被“底上刚好有筋/凸台”带偏，
    多取两条横向往两侧错开就能抗住；代价实测 0.005~0.05s（最大 30,980 面），
    所以不是“多取几条贵很多”那种权衡，3 条是本机实测下白送的数量级。

    “足迹”按那条线上**最外两个交点**算（不是包围盒）：这样一个偏心模型
    也不会因为包围盒比实体大而偷高分数。材料长度取奇配对：最左交点以右是料，
    所以下标 (0,1)、(2,3)… 的区间就是材料（与 `_LineCrossings.inside` 同一口径）。
    """
    if face_count <= 0:
        return None
    if deadline is None:
        deadline = time.monotonic() + _END_BUDGET_S
    if grid is None:
        grid = _FaceGrid(points, face_count, 1.0)
    x_lo, x_hi = metrics.bbox_min[0], metrics.bbox_max[0]
    y_lo, y_hi = metrics.bbox_min[1], metrics.bbox_max[1]
    height = metrics.bbox_max[2] - metrics.bbox_min[2]
    if x_hi <= x_lo or y_hi <= y_lo or height <= 0:
        return None
    band = min(_END_PROBE_MM, max(height * 0.05, 0.05))
    # 贴顶那一层要往下退一个 band（`_LineCrossings` 自己只往上抖 0.0137）。
    z = metrics.bbox_max[2] - band if at_top else metrics.bbox_min[2] + band
    lines = _LineCrossings(points, grid, z, x_lo - 1.0, x_hi + 1.0)
    ratios: list[float] = []
    for frac in fracs:
        if time.monotonic() > deadline:
            break
        yy = y_lo + (y_hi - y_lo) * frac
        xs = lines.crossings(yy)
        if len(xs) < 2:
            continue
        span = xs[-1] - xs[0]
        if span <= 1e-6:
            continue
        material = sum(xs[i + 1] - xs[i] for i in range(0, len(xs) - 1, 2))
        ratios.append(min(material / span, 1.0))
    if not ratios:
        return None
    return sum(ratios) / len(ratios)


@dataclass
class InteriorGaps:
    """三条轴上「两侧都被材料夹住」的空档最大值（mm）。

    为什么必须能测这个：夹口、插槽、卡座这类**减法功能特征**在参数、包围盒、
    连通分量里完全看不出来 —— 刀具没切到时模型就是一整块实心料：
    `solid_parts=1`、`watertight=True`、包围盒对，而壁厚反而因为「根本没有腔」
    显得“很充足”。实测交付给用户的那版 Pocket3 手机夹就是这样：
    源码声明 `clamp_gap=14` 的夹口，网格沿 Y 实测内部空隙 **0.00mm**，
    而需求核对表 9 项全绿、打印性判定 pass/85 分。

    这个测量只回答**空隙存不存在**（不回答位置对不对）：夹口/卡槽/内腔都会被检出；
    三个方向都不被材料夹住的完全对穿开口不在本判据适用范围内（那种由孔径剖面去量）。
    """

    per_axis: tuple[float, float, float]
    longest_material: tuple[float, float, float]
    lines: int

    @property
    def max_mm(self) -> float:
        return max(self.per_axis)

    @property
    def max_axis(self) -> int:
        return list(self.per_axis).index(max(self.per_axis))

    @property
    def solid_axis(self) -> int:
        """最长连续材料所在的轴 —— 「这一刀本该切进去却没切」的那个方向。

        注意它和 max_axis 不是一回事：前者看的是**实心长度**，后者看的是**空隙**，
        两个数组的值域方向相反，拿一个去查另一个的下标会直接 ValueError。
        """
        return max(range(len(self.longest_material)), key=lambda i: self.longest_material[i])

    def to_dict(self) -> dict[str, object]:
        return {
            "per_axis_mm": [round(v, 2) for v in self.per_axis],
            "max_mm": round(self.max_mm, 2),
            "max_axis": "XYZ"[self.max_axis],
            "longest_material_mm": [round(v, 1) for v in self.longest_material],
            "lines": self.lines,
        }


def measure_interior_gaps(
    points: list[tuple[float, float, float]],
    face_count: int,
    metrics: MeshMetrics,
    *,
    lines: int = 9,
    min_gap_mm: float | None = None,
) -> InteriorGaps | None:
    """沿三条轴打扫描线，量「两侧都有材料夹着」的空档最大长度。

    做法：从包围盒外一点沿轴打射线，收集**全部**交点，按奇偶配对得到材料段；
    相邻材料段之间的空档就是一个被夹住的空隙（夹口/卡槽/内腔）。

    `min_gap_mm` 用来自适应采样密度：扫描线间距必须**小于要找的空隙**，
    否则会整列都恰好避开它，把存在的夹口扫成「没找到」——
    这是这条判据唯一的假失败来源，必须按目标尺寸加密。

    只测量、不下结论：要不要算缺陷由调用方拿声明值去比。
    """
    if face_count <= 0 or face_count > _GAP_MAX_FACES:
        return None
    lo, hi = metrics.bbox_min, metrics.bbox_max
    spans = tuple(hi[a] - lo[a] for a in range(3))
    if min(spans) <= 0.5:
        return None
    if min_gap_mm and min_gap_mm > 0:
        lines = max(
            lines, min(_GAP_MAX_LINES, int(max(spans) / (float(min_gap_mm) * 0.6)) or 0)
        )
    lines = max(3, min(lines, _GAP_MAX_LINES))
    diag = math.sqrt(sum(v * v for v in spans))
    grid = _FaceGrid(points, face_count, max(diag / 20.0, 1.0))
    step = grid.cell / 2.0

    per_axis = [0.0, 0.0, 0.0]
    longest = [0.0, 0.0, 0.0]
    for axis in (0, 1, 2):
        other_u = (axis + 1) % 3
        other_v = (axis + 2) % 3
        direction = [0.0, 0.0, 0.0]
        direction[axis] = 1.0
        origin = [0.0, 0.0, 0.0]
        origin[axis] = lo[axis] - 1.0  # 从包围盒外出发，第一次撞上就是表面
        reach = spans[axis] + 2.0
        for i in range(1, lines + 1):
            fu = lo[other_u] + spans[other_u] * i / (lines + 1)
            # 非整数偏移：避开「采样线正好落在顶点/棱上」的退化情形（奇偶会反）
            fu += 0.0137
            for j in range(1, lines + 1):
                fv = lo[other_v] + spans[other_v] * j / (lines + 1) + 0.0173
                origin[other_u] = fu
                origin[other_v] = fv
                start = tuple(origin)
                hits: list[float] = []
                seen: set[int] = set()
                visited: set[tuple[int, int, int]] = set()
                travelled = 0.0
                while travelled <= reach:
                    probe = list(start)
                    probe[axis] += travelled
                    cell = grid.cell_of(tuple(probe))
                    if cell not in visited:
                        visited.add(cell)
                        for other in grid.candidates(cell) + grid.oversized:
                            if other in seen:
                                continue  # 同一个面存在多个桶里，去重才能保奇偶正确
                            seen.add(other)
                            base = other * 3
                            t = _ray_triangle(
                                start,
                                tuple(direction),
                                points[base],
                                points[base + 1],
                                points[base + 2],
                            )
                            if t is not None:
                                hits.append(t)
                    if travelled + step > reach:
                        break
                    travelled += step
                if len(hits) < 2:
                    continue
                hits.sort()
                uniq = [hits[0]]
                for t in hits[1:]:
                    if t - uniq[-1] > 1e-6:
                        uniq.append(t)
                runs = [
                    (uniq[k], uniq[k + 1])
                    for k in range(0, len(uniq) - 1, 2)
                    if uniq[k + 1] - uniq[k] > _GAP_MIN_MATERIAL_MM
                ]
                if not runs:
                    continue
                longest[axis] = max(longest[axis], max(b - a for a, b in runs))
                for (a, _b), (c, _d) in zip(runs, runs[1:]):
                    if c - _b > per_axis[axis]:
                        per_axis[axis] = c - _b
    return InteriorGaps(
        per_axis=(per_axis[0], per_axis[1], per_axis[2]),
        longest_material=(longest[0], longest[1], longest[2]),
        lines=lines,
    )


def _face_stats(
    points: list[tuple[float, float, float]], face_count: int
) -> tuple[list[float], list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    """一次算出所有面的 (面积, 单位法线, 重心)。支撑估算与摆放建议都要用。"""
    areas: list[float] = []
    normals: list[tuple[float, float, float]] = []
    centroids: list[tuple[float, float, float]] = []
    for face in range(face_count):
        base = face * 3
        ax, ay, az = points[base]
        bx, by, bz = points[base + 1]
        cx, cy, cz = points[base + 2]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length <= 1e-12:
            continue
        areas.append(length / 2.0)
        normals.append((nx / length, ny / length, nz / length))
        centroids.append(((ax + bx + cx) / 3.0, (ay + by + cy) / 3.0, (az + bz + cz) / 3.0))
    return areas, normals, centroids


# 六个轴向摆放：名 → 坐标变换。用坐标置换实现，比构造旋转矩阵再乘一遍更直接。
_ORIENTATIONS: tuple[tuple[str, str, str], ...] = (
    ("as_is", "x", "y", "z"),          # 原样
    ("flip_z", "x", "-y", "-z"),       # 绕 X 翻 180°
    ("x_plus_90", "x", "-z", "y"),
    ("x_minus_90", "x", "z", "-y"),
    ("y_plus_90", "z", "y", "-x"),
    ("y_minus_90", "-z", "y", "x"),
)

_ORIENTATION_LABELS = {
    "as_is": "保持原样",
    "flip_z": "上下翻转",
    "x_plus_90": "绕 X 轴正转 90°",
    "x_minus_90": "绕 X 轴反转 90°",
    "y_plus_90": "绕 Y 轴正转 90°",
    "y_minus_90": "绕 Y 轴反转 90°",
}


def _axis_plan(axes: tuple[str, str, str]) -> tuple[tuple[int, float], ...]:
    """把 ('z', '-x', 'y') 这样的轴描述**只编译一次**成 (分量下标, 符号)。

    为什么要单独抽出来：原来是每算一个分量就现建一次 `{"x":0,"y":1,"z":2}` 字典、
    再做一次字符串切片和 startswith。一个面要算 6 个方向 × 2 组（法线+重心）× 3 分量
    = 36 次，实测 100 万面的模型上这就是 **3600 万次字典构造 + 字符串操作**，
    单这一项占掉整条分析链路 45% 的时间（90 秒里的 72 秒）。
    朝向只有几个、而且每次调用都一样，编译一次就没有这些开销了。
    """
    slots = {"x": 0, "y": 1, "z": 2}
    plan: list[tuple[int, float]] = []
    for axis in axes:
        plan.append((slots[axis[-1]], -1.0 if axis.startswith("-") else 1.0))
    return tuple(plan)


def _transform_faces(
    axes: tuple[str, str, str],
    normals: list[tuple[float, float, float]],
    centroids: list[tuple[float, float, float]],
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]], float]:
    """按坐标置换把法线/重心转到新朝向，并把模型落到平台上。

    面积不跟着转：刚体旋转不改变任何一个面的面积，所以本函数**不**接 `areas`
    （以前接了却一行没用，调用方会以为传进去的那列也被重排了）。
    """
    (i0, s0), (i1, s1), (i2, s2) = _axis_plan(axes)

    new_normals = [(s0 * n[i0], s1 * n[i1], s2 * n[i2]) for n in normals]
    new_centroids = [(s0 * c[i0], s1 * c[i1], s2 * c[i2]) for c in centroids]
    if not new_centroids:
        return new_normals, new_centroids, 0.0
    drop = min(c[2] for c in new_centroids)
    new_centroids = [
        (c[0], c[1], max(0.0, c[2] - drop)) for c in new_centroids
    ]
    return new_normals, new_centroids, drop


def _support_for_orientation(
    areas: list[float],
    normals: list[tuple[float, float, float]],
    centroids: list[tuple[float, float, float]],
    angle_limit: float,
) -> tuple[float, float]:
    """返回 (支撑体积估算 mm³, 需要支撑的下向投影面积 mm²)。

    做法与 DfAM 检查一致（earthtojake/text-to-cad 的 dfam-check）：
    判据分三步 ——
      1. 只算**朝下**的面（法线 z 为负）；
      2. 排除已经贴在平台上的面（重心 z ≈ 0），那些不需要支撑；
      3. 只保留「与水平面夹角 < 自支撑角」的面 —— 竖直面（90°）不需要支撑。

    体积用「投影面积 × 该面重心到平台的高度」这个**棱柱估算**：
    它假设面下方全是空的，因此是**偏大的上界**，不是切片精确值。
    真正的支撑量要等切片器算，这里只用于估算耗材与比较摆放方向。
    """
    volume = 0.0
    area_sum = 0.0
    for index, normal in enumerate(normals):
        nz = normal[2]
        if nz >= -1e-6:
            continue  # 不朝下
        centroid = centroids[index]
        if centroid[2] < 0.1:
            continue  # 已经坐在平台上
        # 与水平面的夹角：法线朝正下方是 0°（水平悬垂），法线水平是 90°（竖直壁）
        surface_angle = 90.0 - math.degrees(math.asin(min(1.0, max(0.0, -nz))))
        if surface_angle >= angle_limit:
            continue
        projected = areas[index] * abs(nz)
        area_sum += projected
        volume += projected * centroid[2]
    return volume, area_sum


def estimate_support(
    points: list[tuple[float, float, float]],
    face_count: int,
    metrics: MeshMetrics,
    angle_limit: float,
    *,
    samples: int = 400,
    seed: int = 42,
) -> "SupportEstimate | None":
    """估算模型按当前朝向摆放时需要多少支撑。

    比 dfam-check 的原始棱柱法更紧一点：那个方法假设悬垂面下方**全是空的**，
    于是把「面到平台的高度」当支撑柱高。对空腔上方的悬垂面这是对的，
    但对「下面本来就有材料」的地方就严重高估 ——
    实测一个 80×88×100 的手机支架被算成需要 267cm³ 支撑（本体 73cm³ 的 3.6 倍），
    而它下面大部分是实心底座，切片器根本不会生成那么多支撑。

    改法：从悬垂面重心往下打一条射线，量到**下面第一个表面**的距离当柱高；
    射线打不到东西才用「到平台的高度」。这样得到的仍然是不小于真实值的上界
    （支撑最多填满面与下方材料之间的空隙），但不再把实心部分也算成支撑。

    面数多时按面积加权抽样（与壁厚测量同一套思路），并如实标注是估算。
    """
    if face_count <= 0:
        return None
    areas, normals, centroids = _face_stats(points, face_count)
    if not areas:
        return None

    # 先筛出「需要支撑」的面，顺带算出总面积（抽样时要用它还原整体）
    needing: list[tuple[float, tuple[float, float, float], float]] = []
    total_projected = 0.0
    for index, normal in enumerate(normals):
        nz = normal[2]
        if nz >= -1e-6:
            continue
        centroid = centroids[index]
        if centroid[2] < 0.1:
            continue
        surface_angle = 90.0 - math.degrees(math.asin(min(1.0, max(0.0, -nz))))
        if surface_angle >= angle_limit:
            continue
        projected = areas[index] * abs(nz)
        total_projected += projected
        needing.append((projected, centroid, centroid[2]))

    part = metrics.volume_mm3
    if not needing:
        return SupportEstimate(
            angle_limit_deg=angle_limit,
            support_volume_mm3=0.0,
            support_area_mm2=0.0,
            part_volume_mm3=part,
            ratio_pct=0.0,
        )

    grid = _FaceGrid(points, face_count, max(math.sqrt(sum(v * v for v in metrics.bbox)) / 20.0, 1.0))
    rng = random.Random(seed)
    count = min(samples, len(needing))
    total_weight = sum(item[0] for item in needing)

    volume = 0.0
    step = grid.cell / 2.0
    for _ in range(count):
        pick = rng.random() * total_weight
        running = 0.0
        chosen = needing[-1]
        for item in needing:
            running += item[0]
            if running >= pick:
                chosen = item
                break
        projected, centroid, height = chosen
        # 从面重心正下方一点点开始往下打，找第一个表面
        start = (centroid[0], centroid[1], centroid[2] - 1e-4)
        direction = (0.0, 0.0, -1.0)
        best: float | None = None
        visited: set[tuple[int, int, int]] = set()
        travelled = 0.0
        while travelled <= height:
            probe = (start[0], start[1], start[2] - travelled)
            cell = grid.cell_of(probe)
            if cell not in visited:
                visited.add(cell)
                for other in grid.candidates(cell) + grid.oversized:
                    base = other * 3
                    t = _ray_triangle(
                        start, direction, points[base], points[base + 1], points[base + 2]
                    )
                    if t is not None and t > 1e-3 and (best is None or t < best):
                        best = t
            if best is not None and travelled > best:
                break
            travelled += step
        column = min(height, best) if best is not None else height
        volume += max(0.0, column)

    # 抽样是「按面积加权 + 有放回」的，所以同一个面会被反复抽到 ——
    # 不能用「样本权重之和」还原整体：那个和会**超过**总面积
    #（实测 133231 vs 总面积 2770），于是「按比例放大」的分支永远不会触发，
    # 直接把未还原的体积累加值当成结果，体积被放大 40 倍（11943cm³ vs 实际 267cm³）。
    # 正确的估计量是：
    #     支撑体积 = 悬垂投影总面积 × 柱高的面积加权平均
    # 样本本身已经按面积加权抽出，所以柱高的样本均值就是这个加权平均。
    if count > 0:
        volume = total_projected * (volume / count)

    return SupportEstimate(
        angle_limit_deg=angle_limit,
        support_volume_mm3=volume,
        support_area_mm2=total_projected,
        part_volume_mm3=part,
        ratio_pct=(100.0 * volume / part) if part > 0 else 0.0,
        sampled=count < len(needing),
        sampled_faces=count,
    )


def orientation_candidates(
    points: list[tuple[float, float, float]],
    face_count: int,
    metrics: MeshMetrics,
    angle_limit: float,
) -> list[dict[str, object]]:
    """六个轴向摆放各自的支撑量与成型高度，用于给「换个方向更省料」的建议。

    只做**轴向**旋转（六个）：这是用户在前端能一眼看懂、也能直接照做的摆法。
    任意角度的最优摆放属于切片器的活，这里不做。
    """
    if face_count <= 0:
        return []
    areas, normals, centroids = _face_stats(points, face_count)
    if not areas:
        return []
    out: list[dict[str, object]] = []
    for name, ax, ay, az in _ORIENTATIONS:
        t_normals, t_centroids, _ = _transform_faces((ax, ay, az), normals, centroids)
        volume, _ = _support_for_orientation(areas, t_normals, t_centroids, angle_limit)
        height = max((c[2] for c in t_centroids), default=0.0)
        out.append(
            {
                "orientation": name,
                "label": _ORIENTATION_LABELS[name],
                "support_volume_mm3": round(volume, 1),
                "support_ratio_pct": round(
                    (100.0 * volume / metrics.volume_mm3) if metrics.volume_mm3 > 0 else 0.0, 1
                ),
                "build_height_mm": round(height, 1),
            }
        )
    return out


def estimate_usage(
    metrics: MeshMetrics,
    *,
    material: str | None = None,
    density_g_cm3: float,
    support: "SupportEstimate | None" = None,
    wall_lines: int = 2,
    line_width_mm: float = 0.4,
    infill_pct: float = 15.0,
    filament_diameter_mm: float = 1.75,
) -> "UsageEstimate":
    """估算耗材用量：模型本体 + 支撑，给出克数与线材长度。

    **为什么不能直接把「实体体积 × 密度」当用量**：那等于假设 100% 填充。
    实测参照物（用户给的 3MF）用的就是「0.16mm 层高、2 层墙、15% 填充」，
    照实体体积算会高估好几倍，估算就失去意义了。

    这里用的是通行的「外壳 + 填充」近似：
        外壳体积 ≈ 表面积 × 壁厚（壁厚 = 墙圈数 × 走线宽度）
        内部体积 ≈ max(0, 实体体积 − 外壳体积)
        实际耗料 ≈ 外壳体积 + 内部体积 × 填充率
    薄壁件会出现「外壳体积 ≥ 实体体积」，此时按实体体积算（等于实心），
    不会算出比实体还多的荒唐结果。

    这是**估算**，不是切片结果：真实值取决于切片器的走线重叠、补强、回抽等。
    所有假设都写进 `notes`，随结果一起交给用户，不藏起来。
    """
    density = max(0.01, density_g_cm3)
    solid_cm3 = metrics.volume_mm3 / 1000.0
    shell_thickness = max(0.0, wall_lines * line_width_mm)
    shell_cm3 = (metrics.surface_area_mm2 * shell_thickness) / 1000.0
    shell_cm3 = min(shell_cm3, solid_cm3)
    core_cm3 = max(0.0, solid_cm3 - shell_cm3)
    printed_cm3 = shell_cm3 + core_cm3 * max(0.0, min(1.0, infill_pct / 100.0))

    support_cm3 = (support.support_volume_mm3 / 1000.0) if support else 0.0
    support_printed_cm3 = support_cm3 * max(0.0, min(1.0, infill_pct / 100.0))

    cross_section = math.pi * (filament_diameter_mm / 2.0) ** 2  # mm²
    total_cm3 = printed_cm3 + support_printed_cm3
    grams = total_cm3 * density

    notes = [
        f"按「{wall_lines} 层墙 × {line_width_mm}mm 走线 + {infill_pct:g}% 填充」估算；"
        "换切片参数会变。",
        "支撑量由悬垂面往下打射线量到下方表面得到，仍是偏大的上界，不是切片精确值。",
    ]
    # 单一数字会假装很精确，而这里本质是个区间：
    #   下限 = 外壳 + 内部按填充率；
    #   上限 = 实体等效（100% 填充），薄壁件会逼近这一端 ——
    #   因为壁本身就只有几毫米厚，切片器会把整面墙打成实心，
    #   而不是「两层皮 + 中间 15% 填充」。
    # 用实测壁厚中位数判断更靠近哪一端，把判断依据一起写出来。
    if metrics.wall is not None and metrics.wall.median_mm > 0:
        ratio = (2.0 * shell_thickness) / metrics.wall.median_mm
        if ratio >= 1.0:
            notes.append(
                f"实测壁厚中位数只有 {metrics.wall.median_mm:.2f}mm，比两层墙还薄 —— "
                "这种零件切片后基本是实心的，实际用量靠近**上限**。"
            )
        elif ratio >= 0.6:
            notes.append(
                f"实测壁厚中位数 {metrics.wall.median_mm:.2f}mm，偏薄 —— "
                "实际用量会明显靠近上限，按上限备料更稳妥。"
            )
        else:
            notes.append(
                f"实测壁厚中位数 {metrics.wall.median_mm:.2f}mm，属于厚实件 —— "
                "实际用量靠近下限。"
            )
    if support is None or support.support_volume_mm3 <= 0:
        notes.append("该朝向不需要支撑。")
    if solid_cm3 > 0 and printed_cm3 / solid_cm3 < 0.35:
        notes.append(
            f"实体等效体积为 {solid_cm3:.1f}cm³，按填充估算约为它的 "
            f"{printed_cm3 / solid_cm3 * 100:.0f}%。"
        )

    return UsageEstimate(
        material=material or "未知材料",
        density_g_cm3=density,
        model_solid_cm3=solid_cm3,
        model_printed_cm3=printed_cm3,
        model_grams=printed_cm3 * density,
        support_cm3=support_cm3,
        support_grams=support_printed_cm3 * density,
        total_grams=grams,
        total_grams_upper=(solid_cm3 + support_printed_cm3) * density,
        filament_length_m=(total_cm3 * 1000.0) / cross_section / 1000.0 if cross_section else 0.0,
        support_ratio_pct=(
            100.0 * support_cm3 / solid_cm3 if solid_cm3 > 0 else 0.0
        ),
        filament_diameter_mm=filament_diameter_mm,
        wall_lines=wall_lines,
        line_width_mm=line_width_mm,
        infill_pct=infill_pct,
        notes=notes,
    )


@dataclass
class SupportEstimate:
    """支撑量估算（偏大的上界：支撑最多填满悬垂面与下方材料之间的空隙）。"""

    angle_limit_deg: float
    support_volume_mm3: float
    support_area_mm2: float
    part_volume_mm3: float
    ratio_pct: float
    # 悬垂面很多时是抽样估算的，如实标出来
    sampled: bool = False
    sampled_faces: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "angle_limit_deg": round(self.angle_limit_deg, 1),
            "support_volume_mm3": round(self.support_volume_mm3, 1),
            "support_area_mm2": round(self.support_area_mm2, 1),
            "ratio_pct": round(self.ratio_pct, 1),
            "sampled": self.sampled,
            "method": (
                "悬垂面向下打射线量到下方第一个表面，取其高度作支撑柱高；"
                "射线打不到才用「面到平台的高度」。仍是偏大的上界，不是切片精确值"
            ),
        }


@dataclass
class UsageEstimate:
    """耗材用量估算：模型本体 + 支撑。

    刻意把「实体等效体积」和「按实际填充估算的体积」都留下 ——
    只给一个数字会让人误以为它是切片结果。
    """

    material: str
    density_g_cm3: float
    model_solid_cm3: float
    model_printed_cm3: float
    model_grams: float
    support_cm3: float
    support_grams: float
    total_grams: float
    # 上限：按实体等效（100% 填充）算出来的用量。薄壁件实际会逼近这个值，
    # 备料时用它更稳妥。
    total_grams_upper: float
    filament_length_m: float
    support_ratio_pct: float
    filament_diameter_mm: float
    wall_lines: int
    line_width_mm: float
    infill_pct: float
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "material": self.material,
            "density_g_cm3": round(self.density_g_cm3, 3),
            "model_solid_cm3": round(self.model_solid_cm3, 2),
            "model_printed_cm3": round(self.model_printed_cm3, 2),
            "model_grams": round(self.model_grams, 1),
            "support_cm3": round(self.support_cm3, 2),
            "support_grams": round(self.support_grams, 1),
            "total_grams": round(self.total_grams, 1),
            "total_grams_upper": round(self.total_grams_upper, 1),
            "filament_length_m": round(self.filament_length_m, 2),
            "support_ratio_pct": round(self.support_ratio_pct, 1),
            "filament_diameter_mm": self.filament_diameter_mm,
            "assumptions": {
                "wall_lines": self.wall_lines,
                "line_width_mm": self.line_width_mm,
                "infill_pct": self.infill_pct,
            },
            "notes": list(self.notes),
        }


def _collect_issues(metrics: MeshMetrics, limits: AnalysisLimits) -> None:
    """把客观数据翻译成用户能理解的注意点（阈值来自选中的打印机）。"""
    x, y, z = metrics.bbox
    printer = limits.printer_name

    if not metrics.watertight:
        if metrics.boundary_edges:
            metrics.issues.append(
                f"模型不是封闭实体（有 {metrics.boundary_edges} 条开放边），"
                "切片时可能出现空洞或无法自动修复，建议让 AI 重新生成。"
            )
        if metrics.non_manifold_edges:
            metrics.issues.append(
                f"存在 {metrics.non_manifold_edges} 条非流形边（超过两个面共享同一条边），"
                "部分切片软件会报错。"
            )

    if metrics.volume_mm3 <= 0:
        metrics.issues.append("模型体积为 0，说明它可能只是一个平面或空壳，无法作为实体打印。")

    # 散件检测：一个成品被做成了互不相连的几个零件。
    # 这是「模型不贴合描述」最常见的形态 —— 包围盒、体积、封闭性全都正常，
    # 但用户要的是「一件东西」，拿到的是「一堆东西」。
    if metrics.stray_parts > 0:
        metrics.issues.append(
            f"模型由 {metrics.solid_parts} 个互不相连的零件组成（不是一整件），"
            "打印出来需要自己拼装或粘接。"
        )
    # 腔体内部的孤岛：容器内腔里出现了不贴内壁的材料截面。
    # 与「散件」互补 —— 散件是几块分开的东西，这个是**一整件**但特征长错了位置，
    # 用户在预览里看到「桶的内壁伸出一块板」，却没有任何指标提示他。
    #
    # 措辞刻意留了余地：程序只能从截面看到「腔里有独立的材料环」，
    # 分不清它是「外壁加强筋穿腔」（缺陷）还是「刻意做的内凸台」（合法）——
    # 这两者的截面指纹完全一样（实测：贴底的内凸台也会被检出）。
    # 所以这里如实陈述观察到的事实 + 最可能的原因，把判断权交给 AI 复核与用户。
    if metrics.cavity_islands:
        heights = "、".join(f"{z:.0f}mm" for z, _ in metrics.cavity_islands)
        worst = max(n for _, n in metrics.cavity_islands)
        metrics.issues.append(
            f"在内腔范围内检测到 {worst} 处不与内壁相连的材料截面（高度约 {heights}）。"
            "如果那是本该做在外壁上的加强筋/凸台，说明它没有贴合外形轮廓、从外壁穿进了腔体，"
            "放东西时会被挡住 —— 请让 AI 检查这一处。"
        )

    if metrics.degenerate_triangles:
        metrics.issues.append(f"有 {metrics.degenerate_triangles} 个退化三角面（面积为零），建议简化模型。")

    # 支撑占比偏高：DfAM 检查的经验是「支撑超过模型体积约 30% 就值得换摆放方向」。
    # 措辞刻意留了余地 —— 这个估算是**上界**，而且分不清「外部悬垂」和
    # 「向下的内腔顶面」，所以内部有空腔的零件会被算得很高。
    # 可靠的用法是**比较不同摆放方向**（同一套偏差对每个方向都成立），而不是看绝对值。
    if metrics.support is not None and metrics.support.ratio_pct >= 30.0:
        message = (
            f"按当前摆放方向，保守估算需要约 {metrics.support.support_volume_mm3 / 1000:.1f}cm³ 支撑"
            f"（约为模型本体的 {metrics.support.ratio_pct:.0f}%）。"
        )
        if metrics.orientation_hint is not None:
            hint = metrics.orientation_hint
            message += (
                f"换成「{hint['label']}」摆法预计只要 "
                f"{float(hint['support_volume_mm3']) / 1000:.1f}cm³，能省不少料和时间。"
            )
        else:
            message += "支撑越多越费料、越费时间，也越容易在打印中被碰倒，建议换一个摆放方向。"
        message += (
            "（这是偏大的估算：它按「悬垂面下方是空的」来算，"
            "零件内部有向下开口的空腔时会算得偏高，请以方向之间的**比较**为准。）"
        )
        metrics.issues.append(message)

    smallest = min(v for v in (x, y, z) if v > 0) if any(v > 0 for v in (x, y, z)) else 0.0
    rough_fired = False
    if smallest:
        # 「零件整体太细」：包围盒最小边连一层走线都放不下，成型都困难。
        # 注意这与「壁厚」是两件事，不要混为一谈 —— 一个 110mm 高的桶，
        # 包围盒最小边 110mm，但它的壁可能只有 0.8mm。壁厚由下面独立测。
        if smallest < limits.rough_min_dim_mm:
            rough_fired = True
            unit = "最小可成型细节" if limits.is_resin else f"喷嘴 {limits.nozzle_mm}mm 的最小可成型尺寸"
            metrics.too_thin_rough = True
            metrics.issues.append(
                f"模型最薄方向只有 {smallest:.2f}mm，低于{unit}（约 {limits.rough_min_dim_mm:.1f}mm），"
                "这个方向连一层走线都放不下，打印后容易断裂或根本成型不了，请加厚。"
            )

    # 壁厚：必须用射线法实测，不能用包围盒最小边代替。
    # 旧版拿包围盒最小边当壁厚，对空心容器**完全失效** ——
    # 实测的桌面垃圾桶 110×186×110mm、壁 2.4mm，包围盒最小边 110mm，
    # 于是壁做得多薄都不会报警，而容器正是这个软件最主要的用途。
    if metrics.wall is not None:
        wall = metrics.wall
        # 薄片类零件（板的厚度就是壁厚）会让上面两条检查说的是**同一件事**，
        # 那就只留「整体太细」这一条，避免对同一个事实报两次、把用户绕晕。
        # 判据：包围盒最小边已经判过太细，且实测壁厚与它基本相等。
        same_fact = rough_fired and abs(wall.p05_mm - smallest) <= max(0.05, 0.1 * smallest)
        # 判据用 p05 而不是 min：min 可能是采样离群值（噪声三角面造成的假薄壁），
        # p05 偏低说明有**一片区域**真的偏薄，这才值得报警。两个值都报给用户。
        if wall.p05_mm < limits.min_wall_mm and not same_fact:
            metrics.too_thin_wall = True
            where = ""
            if wall.thinnest:
                point, value = wall.thinnest[0]
                where = (
                    f"最薄处在坐标 ({point[0]:.0f}, {point[1]:.0f}, {point[2]:.0f}) 附近，"
                    f"约 {value:.2f}mm。"
                )
            stats = (
                f"实测壁厚：5% 分位 {wall.p05_mm:.2f}mm、中位数 {wall.median_mm:.2f}mm、"
                f"最薄 {wall.min_mm:.2f}mm（共 {wall.samples} 处采样）"
            )
            thread_depth = metrics.thread_depth_mm
            if thread_depth is not None:
                # 螺纹件的「薄」是牙型本身，不是壁做薄了 —— 必须分开说。
                # 实测：一个完全合规的 M72×0.75 转接环，射线法量出来最薄 0.05mm、
                # 中位数 0.47mm；照普通薄壁判据就会报"远低于 1.2mm 无法打印"，
                # 而它其实是可以打的（0.1mm 层高），只是牙尖细。
                # 用户看到这种结论会以为模型废了 —— 这是**误报**，要按螺纹口径讲清楚。
                metrics.thread_depth_mm = thread_depth
                metrics.issues.append(
                    f"检测到螺旋螺纹（实测牙深约 {thread_depth:.2f}mm，即 0.75mm 细牙）："
                    f"上方那条「壁厚」统计量的是**牙尖**，不是壁做得薄。"
                    f"细牙在 {limits.nozzle_mm}mm 喷嘴上建议用 0.1mm 层高打印；"
                    f"若只想粗用，可把牙型换成粗牙（例如 M72×2）。{where}"
                )
            elif limits.is_resin:
                metrics.issues.append(
                    f"{stats}。该机型建议的最小细节厚度为 {limits.min_wall_mm:.2f}mm，"
                    f"当前有一片区域偏薄，脱模与后固化时易变形。{where}"
                )
            else:
                metrics.issues.append(
                    f"{stats}。低于该机型（{printer}）建议的壁厚 {limits.min_wall_mm:.1f}mm"
                    f"（3 倍喷嘴 {limits.nozzle_mm}mm）：壁太薄时层间结合强度不足，"
                    f"受力会从层间开裂。建议加厚到 {limits.min_wall_mm:.1f}mm 以上或加筋。{where}"
                )

    bx, by, bz = limits.build_volume
    overflow = []
    if x > bx:
        overflow.append(f"X {x:.0f}>{bx:.0f}")
    if y > by:
        overflow.append(f"Y {y:.0f}>{by:.0f}")
    if z > bz:
        overflow.append(f"Z {z:.0f}>{bz:.0f}")
    if overflow:
        metrics.exceeds_build_volume = True
        metrics.issues.append(
            f"模型尺寸 {x:.0f}×{y:.0f}×{z:.0f}mm 超出该机型（{printer}）的成型范围"
            f" {bx:.0f}×{by:.0f}×{bz:.0f}mm：{'、'.join(overflow)}。"
            "需要缩小、旋转摆放或分件打印。"
        )

    if not metrics.sits_on_plate:
        metrics.issues.append(
            f"模型底面不在地面上（最低点为 {metrics.bbox_min[2]:.1f}mm），"
            "切片软件里可能需要手动放平，或让 AI 把模型落到平台上。"
        )

    if metrics.triangles > limits.max_triangles:
        metrics.issues.append(
            f"模型包含 {metrics.triangles:,} 个三角面，文件较大，"
            "预览与切片可能较慢，可让 AI 降低细节精度。"
        )


def _gap_line(metrics: MeshMetrics) -> str:
    """给 AI 的一句话：源码声明了多大间隙 vs 网格上实际量到多大被夹空档。

    为什么也要写进「给 AI 的客观数据」：不给它这条，它会对着一个根本没夹口的模型
    继续说「可以打印」，程序只能在它说完之后把判定改回来。
    没声明间隙时返回空串（不占提示词篇幅）。
    """
    if not metrics.declared_gaps:
        return ""
    declared = "、".join(
        f"{label}={float(mm):g}mm" for label, mm, _name in metrics.declared_gaps[:4]
    )
    gaps = metrics.interior_gaps
    if gaps is None:
        measured = "本次未测（面数或尺寸超出上限）"
    else:
        measured = "三轴最大被夹空档 " + "、".join(
            f"{axis}向 {float(v):.2f}mm" for axis, v in zip("XYZ", gaps.per_axis)
        )
    return (
        f"间隙实测：源码声明 {declared}，而网格{measured}"
        " —— 声明值明显大于实测量到的空档时，difference 的刀具根本没切到实体"
        "（依然能编译、STL 非空、没 warning）。"
    )


def summarize_for_ai(metrics: MeshMetrics, filename: str, limits: AnalysisLimits | None = None) -> str:
    """给 AI 的客观数据摘要（打印性检查的事实依据）。"""
    active = limits or AnalysisLimits()
    lines = [
        f"文件名：{filename}",
        f"三角面数：{metrics.triangles}",
        f"顶点数：{metrics.vertices}",
        f"包围盒尺寸(mm)：{metrics.bbox[0]:.2f} × {metrics.bbox[1]:.2f} × {metrics.bbox[2]:.2f}",
        f"包围盒最小点(mm)：({metrics.bbox_min[0]:.2f}, {metrics.bbox_min[1]:.2f}, {metrics.bbox_min[2]:.2f})",
        f"包围盒最大点(mm)：({metrics.bbox_max[0]:.2f}, {metrics.bbox_max[1]:.2f}, {metrics.bbox_max[2]:.2f})",
        f"体积(mm³)：{metrics.volume_mm3:.2f}（约 {metrics.volume_cm3:.2f} cm³）",
        f"表面积(mm²)：{metrics.surface_area_mm2:.2f}",
        f"封闭实体：{'是' if metrics.watertight else '否'}（开放边 {metrics.boundary_edges}，"
        f"非流形边 {metrics.non_manifold_edges}）",
        f"最低点是否贴平台：{'是' if metrics.sits_on_plate else '否'}",
        f"退化三角面：{metrics.degenerate_triangles}",
        # 让 AI 评审也能看到「是不是一整件」——它比程序更懂「这个品类本该是一件」
        f"实体零件数：{metrics.solid_parts}"
        + (
            "（**多个互不相连的零件**：请判断该品类是否本该是一件整体，"
            "若是则必须明确指出）"
            if metrics.stray_parts > 0
            else "（整体一件）"
        ),
        # 给出散件的位置与尺寸，AI 才能把「哪一件」对回自己写的某段循环/某个 module。
        # 只说「有 80 个散件」时，实测它根本改不动。
        "散件明细（位置为包围盒中心，单位 mm）："
        + (
            "；".join(
                f"#{i} 中心({d['center'][0]},{d['center'][1]},{d['center'][2]}) "
                f"尺寸{d['size'][0]}×{d['size'][1]}×{d['size'][2]} 体积{d['volume_mm3']}mm³"
                for i, d in enumerate(metrics.stray_details[:8], 1)
            )
            + (f"…共 {metrics.stray_parts} 件" if metrics.stray_parts > 8 else "")
            if metrics.stray_details
            else "无"
        ),
        # 截面上的腔体孤岛：整件连通但特征长进了腔体内部。
        # 这是「参数全对、几何不对」的典型，零件数检查看不到，必须单独给 AI。
        "腔体孤岛检查："
        + (
            "在 "
            + "、".join(f"z≈{z:.0f}mm 处 {n} 块" for z, n in metrics.cavity_islands)
            + " 检测到被空腔包围的悬空材料；请结合源码判断是设计意图"
            "（如内腔螺柱、隔板）还是特征没贴合外壁导致的穿腔"
        )
        if metrics.cavity_islands
        else "截面未发现被空腔包围的悬空材料",
        # 壁厚是射线法**实测**出来的，不是从包围盒推的。
        # 必须单独给 AI：决定能不能打印的是壁厚，而不是包围盒。
        "壁厚（射线法实测）："
        + (
            f"5% 分位 {metrics.wall.p05_mm:.2f}mm、中位数 {metrics.wall.median_mm:.2f}mm、"
            f"最薄 {metrics.wall.min_mm:.2f}mm（{metrics.wall.samples} 处采样）"
            + (
                "；最薄处："
                + "、".join(
                    f"({p[0]:.0f},{p[1]:.0f},{p[2]:.0f}) 处 {t:.2f}mm"
                    for p, t in metrics.wall.thinnest[:3]
                )
                if metrics.wall.thinnest
                else ""
            )
            if metrics.wall is not None
            else "未测出（网格可能不封闭或过于退化）"
        ),
        # 支撑与耗材：用户最关心的两个实际数字（要花多少料、要打多久的支撑）。
        # 强调是估算，避免被当成切片结果。
        "支撑估算（偏大的上界，不是切片精确值）："
        + (
            f"约 {metrics.support.support_volume_mm3 / 1000:.1f}cm³，"
            f"占模型本体 {metrics.support.ratio_pct:.0f}%"
            f"（按自支撑角 {metrics.support.angle_limit_deg:.0f}° 判定）"
            if metrics.support is not None
            else "未测出"
        ),
        "耗材估算："
        + (
            f"{metrics.usage.total_grams:.1f}–{metrics.usage.total_grams_upper:.1f}g"
            f"（模型 {metrics.usage.model_grams:.1f}g"
            + (f" + 支撑 {metrics.usage.support_grams:.1f}g" if metrics.usage.support_grams > 0 else "")
            + f"），约 {metrics.usage.filament_length_m:.2f}m 线材（{metrics.usage.material}）；"
            f"按 {metrics.usage.wall_lines} 层墙 × {metrics.usage.line_width_mm}mm 走线 + "
            f"{metrics.usage.infill_pct:g}% 填充估算，实体等效体积 {metrics.usage.model_solid_cm3:.1f}cm³"
            if metrics.usage is not None
            else "未估算（缺少材料信息）"
        ),
    ]
    # 把「按这台机器判断」的阈值也交给 AI，否则它会用通用经验值下结论，
    # 与用户实际选择的机型不符。
    lines += [
        "—— 判定基准（来自用户选择的打印机）——",
        f"机型：{active.printer_name}",
        f"成型范围(mm)：{active.build_x:.0f} × {active.build_y:.0f} × {active.build_z:.0f}",
        f"喷嘴直径(mm)：{active.nozzle_mm if not active.is_resin else '不适用（光固化）'}",
        f"该机型最小可靠壁厚(mm)：{active.min_wall_mm}",
        f"该机型最小凸起/浮雕(mm)：{active.min_protrusion_mm}",
        f"该机型建议最大悬垂(°)：{active.max_overhang_deg}",
        f"该机型建议最大桥接(mm)：{active.max_bridge_mm}",
        f"该机型最小可成型尺寸(mm)：{active.rough_min_dim_mm}",
    ]
    if metrics.issues:
        lines.append("按上述基准自动检测到的问题：" + "；".join(metrics.issues))
    else:
        lines.append("按上述基准自动检测：未发现明显几何问题。")
    gap_line = _gap_line(metrics)
    if gap_line:
        lines.append(gap_line)
    return "\n".join(lines)
