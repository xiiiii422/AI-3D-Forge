"""AI 3D Forge —— 打印机特性知识库。

这是本软件「懂 3D 打印」的地方。它做三件事：

1. **知识库**：内置主流机型的真实规格（成型尺寸、喷嘴、层高、封闭腔体、
   调平方式、推荐悬垂角等），并说明每一项对模型设计意味着什么。
2. **参数推导**：把「机型 + 喷嘴 + 材料」翻译成具体的工艺约束 ——
   最小可靠壁厚、最小孔径、最小凸起、桥接上限、层高上限、公差、
   表面粗糙度（决定 OpenSCAD 的 $fn）。这些数字会真正进入 AI 提示词、
   几何检查和打印性评审，而不是只当成说明文字。
3. **自定义机型**：用户可以新建/修改自己的机器，配置存在
   %APPDATA%\\AI-3D-Forge\\printers.json，与内置库合并、内置项不可删。

数据来源标注在 sources 字段里：厂商公布规格为准；厂商未公布的经验值
（例如某机型的实测最小可靠壁厚）按 FDM 通用规律推算，并明确写成估算，
不假装是官方数字。
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable

from .paths import paths

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# 生成字号时给喷嘴留的余量（见 resolve_fn 的说明）
# 这个倍数决定「圆弧面被多边形逼近时的最大偏差」= 喷嘴 / 该倍数。
#   取 8  → 0.4mm 喷嘴允许偏差 0.050mm：物理上够，但用户反馈"圆看着不圆"
#           （79 段的 Ø130 圆，弦长 5mm，轮廓看得出多边形感）；
#   取 16 → 允许偏差 0.025mm：Ø130 圆约 113 段、Ø80 约 88 段，
#           编译时间约增加 40%，换来肉眼干净的圆。
# 另外把下限从 24 提到 48：小零件（Ø20）此前会被算到 31 段，棱面很明显。
FN_TOLERANCE_DIVISOR = 16.0
FN_MIN = 48
FN_MAX = 160

MATERIAL_DENSITY: dict[str, float] = {
    "PLA": 1.24,
    "PLA-CF": 1.21,
    "PETG": 1.27,
    "ABS": 1.04,
    "ASA": 1.07,
    "TPU": 1.21,
    "PA": 1.14,
    "PA-CF": 1.16,
    "PC": 1.20,
    "PVA": 1.23,
    "HIPS": 1.04,
}

# 材料的热变形温度参考值（℃，厂商常见标注值）
MATERIAL_HEAT_DEFlection: dict[str, int] = {
    "PLA": 55,
    "PLA-CF": 60,
    "PETG": 75,
    "ABS": 98,
    "ASA": 98,
    "TPU": 65,
    "PA": 120,
    "PA-CF": 145,
    "PC": 130,
}


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------
@dataclass
class PrinterProfile:
    """一台打印机的规格 + 喷头配置。"""

    id: str
    name: str
    manufacturer: str = ""
    nozzle_mm: float = 0.4
    build_x: float = 220.0
    build_y: float = 220.0
    build_z: float = 250.0
    nozzle_options: list[float] = field(default_factory=lambda: [0.4])
    layer_min_mm: float = 0.08
    layer_max_mm: float = 0.32
    max_speed_mm_s: int = 150
    max_accel_mm_s2: int = 3000
    max_flow_mm3_s: float = 12.0
    has_heated_bed: bool = True
    max_bed_temp_c: int = 100
    max_hotend_temp_c: int = 260
    enclosure: bool = False
    materials: list[str] = field(default_factory=lambda: ["PLA", "PETG"])
    filament_diameter_mm: float = 1.75
    extruder: str = "direct"  # direct | bowden
    bed_leveling: str = "manual"  # manual | abl | auto
    bed_surface: str = ""
    kinematics: str = "cartesian"  # cartesian | corexy | delta | bedslinger
    recommended_max_overhang_deg: int = 55
    recommended_max_bridge_mm: float = 30.0
    dimensional_tolerance_mm: float = 0.25
    notes: str = ""
    builtin: bool = True
    sources: str = ""
    updated_at: str = ""

    # ---------------- 序列化 ----------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, builtin: bool | None = None) -> "PrinterProfile":
        valid = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in raw.items() if k in valid and v is not None}
        profile = cls(**kwargs)
        if builtin is not None:
            profile.builtin = builtin
        profile.id = str(profile.id).strip()
        profile.name = str(profile.name).strip() or "未命名打印机"
        profile.nozzle_mm = _positive(profile.nozzle_mm, 0.4)
        profile.build_x = _positive(profile.build_x, 220.0)
        profile.build_y = _positive(profile.build_y, 220.0)
        profile.build_z = _positive(profile.build_z, 250.0)
        profile.layer_min_mm = _positive(profile.layer_min_mm, 0.08)
        profile.layer_max_mm = max(_positive(profile.layer_max_mm, 0.32), profile.layer_min_mm)
        if not profile.nozzle_options:
            profile.nozzle_options = [profile.nozzle_mm]
        profile.nozzle_options = sorted({_positive(n, profile.nozzle_mm) for n in profile.nozzle_options})
        if profile.nozzle_mm not in profile.nozzle_options:
            profile.nozzle_options = sorted([*profile.nozzle_options, profile.nozzle_mm])
        profile.recommended_max_overhang_deg = int(max(15, min(80, profile.recommended_max_overhang_deg)))
        profile.materials = [str(m).strip() for m in profile.materials if str(m).strip()] or ["PLA"]
        return profile

    def to_public(self) -> dict[str, Any]:
        data = self.to_dict()
        data["build_volume_mm"] = {
            "x": self.build_x,
            "y": self.build_y,
            "z": self.build_z,
            "volume_cm3": round(self.build_x * self.build_y * self.build_z / 1000.0, 1),
        }
        # 覆盖语义必须显式暴露：用户改过的内置机型仍然是 builtin=True（不可真正删除，
        # 删除等于"恢复出厂参数"），前端只能靠这个字段决定按钮该写「恢复默认」还是「删除」。
        data["overridden"] = bool(self.builtin and self.updated_at)
        return data


def _positive(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


@dataclass
class ProcessConstraints:
    """把打印机规格翻译成「模型该怎么设计」的具体数字。"""

    min_wall_mm: float
    min_hole_dia_mm: float
    min_hole_dia_vertical_mm: float
    min_protrusion_mm: float
    clearance_mm: float
    max_layer_mm: float
    default_layer_mm: float
    max_horizontal_span_mm: float
    max_overhang_deg: int
    max_build_x: float
    max_build_y: float
    max_build_z: float
    max_build_diagonal_mm: float
    fn: int
    rough_min_dim_mm: float
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary_lines(self) -> list[str]:
        """给界面看的人话摘要。"""
        lines = [
            f"壁厚不小于 {self.min_wall_mm:.1f}mm",
            f"孔径不小于 {self.min_hole_dia_mm:.1f}mm"
            f"（竖孔 {self.min_hole_dia_vertical_mm:.1f}mm）",
            f"凸起/凸字不小于 {self.min_protrusion_mm:.1f}mm",
            f"活动件间隙 {self.clearance_mm:.2f}mm",
            f"层高 {self.default_layer_mm:.2f}mm（上限 {self.max_layer_mm:.2f}mm）",
            f"悬垂不超过 {self.max_overhang_deg}°",
            f"桥接不超过 {self.max_horizontal_span_mm:.0f}mm",
            f"成型范围 {self.max_build_x:.0f}×{self.max_build_y:.0f}×{self.max_build_z:.0f}mm",
            f"最小可成型尺寸 {self.rough_min_dim_mm:.1f}mm",
        ]
        return lines


# --------------------------------------------------------------------------
# 内置知识库
# --------------------------------------------------------------------------
def _p(**kwargs: Any) -> PrinterProfile:
    return PrinterProfile(builtin=True, **kwargs)


BUILTIN_PRINTERS: list[PrinterProfile] = [
    _p(
        id="generic-220-04",
        name="通用 FDM 打印机 220×220×250（0.4mm）",
        manufacturer="通用",
        nozzle_mm=0.4,
        nozzle_options=[0.2, 0.4, 0.6, 0.8],
        build_x=220, build_y=220, build_z=250,
        layer_min_mm=0.08, layer_max_mm=0.32,
        bed_surface="PEI 弹簧钢板",
        notes="不确定自己的机型时选这个。适合绝大多数桌面 FDM 打印机。",
        sources="通用桌面 FDM 规格；Ender 3 系列同类尺寸。",
    ),
    _p(
        id="bambu-x1c-04",
        name="Bambu Lab X1 Carbon（0.4mm）",
        manufacturer="Bambu Lab",
        nozzle_mm=0.4,
        nozzle_options=[0.2, 0.4, 0.6, 0.8],
        build_x=256, build_y=256, build_z=256,
        layer_min_mm=0.08, layer_max_mm=0.28,
        max_speed_mm_s=500, max_accel_mm_s2=20000, max_flow_mm3_s=32,
        has_heated_bed=True, max_bed_temp_c=120, max_hotend_temp_c=300,
        enclosure=True,
        materials=["PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PA-CF", "PC", "PLA-CF"],
        extruder="direct",
        bed_leveling="auto",
        bed_surface="PEI 弹簧钢板",
        kinematics="corexy",
        recommended_max_overhang_deg=60,
        recommended_max_bridge_mm=45,
        dimensional_tolerance_mm=0.15,
        notes="封闭腔体 + 高速 CoreXY。适合 ABS/ASA/PA 等工程材料；建议开启流量校准。",
        sources="Bambu Lab 官方规格（成型尺寸 256³、最高喷嘴 300℃、热床 120℃）。",
    ),
    _p(
        id="bambu-p1s-04",
        name="Bambu Lab P1S（0.4mm）",
        manufacturer="Bambu Lab",
        nozzle_mm=0.4,
        nozzle_options=[0.2, 0.4, 0.6, 0.8],
        build_x=256, build_y=256, build_z=256,
        layer_min_mm=0.08, layer_max_mm=0.28,
        max_speed_mm_s=500, max_accel_mm_s2=20000, max_flow_mm3_s=32,
        max_bed_temp_c=100, max_hotend_temp_c=300,
        enclosure=True,
        materials=["PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PA-CF", "PLA-CF"],
        extruder="direct",
        bed_leveling="auto",
        bed_surface="PEI 弹簧钢板",
        kinematics="corexy",
        recommended_max_overhang_deg=60,
        recommended_max_bridge_mm=45,
        dimensional_tolerance_mm=0.18,
        notes="与 X1C 同为 256³ 打印空间，热床最高 100℃。",
        sources="Bambu Lab 官方规格。",
    ),
    _p(
        id="bambu-a1-04",
        name="Bambu Lab A1（0.4mm）",
        manufacturer="Bambu Lab",
        nozzle_mm=0.4,
        nozzle_options=[0.2, 0.4, 0.6, 0.8],
        build_x=256, build_y=256, build_z=256,
        layer_min_mm=0.08, layer_max_mm=0.28,
        max_speed_mm_s=500, max_accel_mm_s2=10000, max_flow_mm3_s=24,
        max_bed_temp_c=100, max_hotend_temp_c=300,
        enclosure=False,
        materials=["PLA", "PETG", "TPU", "PLA-CF"],
        extruder="direct",
        bed_leveling="auto",
        bed_surface="PEI 弹簧钢板",
        kinematics="bedslinger",
        recommended_max_overhang_deg=55,
        recommended_max_bridge_mm=35,
        dimensional_tolerance_mm=0.20,
        notes="开放式床身，不建议 ABS/ASA（易翘边开裂）。",
        sources="Bambu Lab 官方规格。",
    ),
    _p(
        id="prusa-mk4-04",
        name="Original Prusa MK4（0.4mm）",
        manufacturer="Prusa Research",
        nozzle_mm=0.4,
        nozzle_options=[0.25, 0.4, 0.6, 0.8],
        build_x=250, build_y=210, build_z=220,
        layer_min_mm=0.05, layer_max_mm=0.30,
        max_speed_mm_s=200, max_accel_mm_s2=8000, max_flow_mm3_s=20,
        max_bed_temp_c=120, max_hotend_temp_c=290,
        enclosure=False,
        materials=["PLA", "PETG", "ASA", "TPU", "PLA-CF", "PA"],
        extruder="direct",
        bed_leveling="abl",
        bed_surface="纹理 PEI 钢板",
        kinematics="bedslinger",
        recommended_max_overhang_deg=55,
        recommended_max_bridge_mm=35,
        dimensional_tolerance_mm=0.15,
        notes="Nextruder 直驱；官方标称层高最低 0.05mm。",
        sources="Prusa Research 官方规格（250×210×220mm）。",
    ),
    _p(
        id="prusa-mini-04",
        name="Original Prusa MINI+（0.4mm）",
        manufacturer="Prusa Research",
        nozzle_mm=0.4,
        nozzle_options=[0.25, 0.4, 0.6],
        build_x=180, build_y=180, build_z=180,
        layer_min_mm=0.05, layer_max_mm=0.25,
        max_speed_mm_s=200, max_accel_mm_s2=4000, max_flow_mm3_s=12,
        max_bed_temp_c=100, max_hotend_temp_c=260,
        enclosure=False,
        materials=["PLA", "PETG", "TPU"],
        extruder="bowden",
        bed_leveling="abl",
        bed_surface="纹理 PEI 钢板",
        kinematics="bedslinger",
        recommended_max_overhang_deg=50,
        recommended_max_bridge_mm=25,
        dimensional_tolerance_mm=0.25,
        notes="180³ 小尺寸机器，模型较大时会自动提示缩小或分件。",
        sources="Prusa Research 官方规格（180×180×180mm）。",
    ),
    _p(
        id="creality-ender3-04",
        name="Creality Ender-3 系列（0.4mm）",
        manufacturer="Creality",
        nozzle_mm=0.4,
        nozzle_options=[0.3, 0.4, 0.5, 0.6, 0.8],
        build_x=220, build_y=220, build_z=250,
        layer_min_mm=0.10, layer_max_mm=0.32,
        max_speed_mm_s=80, max_accel_mm_s2=1500, max_flow_mm3_s=8,
        max_bed_temp_c=100, max_hotend_temp_c=255,
        enclosure=False,
        materials=["PLA", "PETG", "TPU"],
        extruder="bowden",
        bed_leveling="manual",
        bed_surface="磁性软磁贴/玻璃",
        kinematics="bedslinger",
        recommended_max_overhang_deg=50,
        recommended_max_bridge_mm=20,
        dimensional_tolerance_mm=0.30,
        notes="远端送料 + 手动调平；建议把层高控制在 0.2mm 左右，细小特征适当放大。",
        sources="Creality 官方规格（220×220×250mm）；悬垂/桥接为同级别机器的保守经验值。",
    ),
    _p(
        id="creality-k1-04",
        name="Creality K1 / K1 Max 系列（0.4mm）",
        manufacturer="Creality",
        nozzle_mm=0.4,
        nozzle_options=[0.4, 0.6, 0.8],
        build_x=300, build_y=300, build_z=300,
        layer_min_mm=0.08, layer_max_mm=0.30,
        max_speed_mm_s=600, max_accel_mm_s2=20000, max_flow_mm3_s=32,
        max_bed_temp_c=120, max_hotend_temp_c=300,
        enclosure=True,
        materials=["PLA", "PETG", "ABS", "ASA", "TPU", "PLA-CF"],
        extruder="direct",
        bed_leveling="auto",
        bed_surface="PEI 弹簧钢板",
        kinematics="corexy",
        recommended_max_overhang_deg=60,
        recommended_max_bridge_mm=45,
        dimensional_tolerance_mm=0.20,
        notes="此处按 K1 Max 的 300³ 空间记录；标准 K1 为 220×220×250，请按实际情况修改。",
        sources="Creality 官方规格（K1 Max 300×300×300mm）。",
    ),
    _p(
        id="voron-24-350-04",
        name="Voron 2.4 350（0.4mm）",
        manufacturer="Voron",
        nozzle_mm=0.4,
        nozzle_options=[0.25, 0.4, 0.6, 0.8],
        build_x=350, build_y=350, build_z=330,
        layer_min_mm=0.05, layer_max_mm=0.30,
        max_speed_mm_s=500, max_accel_mm_s2=10000, max_flow_mm3_s=24,
        max_bed_temp_c=120, max_hotend_temp_c=300,
        enclosure=True,
        materials=["PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PA-CF", "PC"],
        extruder="direct",
        bed_leveling="abl",
        bed_surface="PEI 弹簧钢板",
        kinematics="corexy",
        recommended_max_overhang_deg=60,
        recommended_max_bridge_mm=50,
        dimensional_tolerance_mm=0.20,
        notes="自组装机器，实际参数取决于你的配置；这里给的是 350mm 规格的常见值。",
        sources="Voron 2.4 官方设计规格（350×350×330mm）；其余为社区常见配置值。",
    ),
    _p(
        id="anycubic-kobra2-04",
        name="Anycubic Kobra 2（0.4mm）",
        manufacturer="Anycubic",
        nozzle_mm=0.4,
        nozzle_options=[0.4, 0.6],
        build_x=220, build_y=220, build_z=250,
        layer_min_mm=0.10, layer_max_mm=0.30,
        max_speed_mm_s=300, max_accel_mm_s2=5000, max_flow_mm3_s=18,
        max_bed_temp_c=110, max_hotend_temp_c=260,
        enclosure=False,
        materials=["PLA", "PETG", "TPU", "ABS"],
        extruder="direct",
        bed_leveling="auto",
        bed_surface="PEI 弹簧钢板",
        kinematics="bedslinger",
        recommended_max_overhang_deg=55,
        recommended_max_bridge_mm=30,
        dimensional_tolerance_mm=0.25,
        notes="近端送料 + 自动调平。",
        sources="Anycubic 官方规格（220×220×250mm）。",
    ),
    _p(
        id="large-500-04",
        name="大幅面打印机 500×500×500（0.4mm）",
        manufacturer="通用",
        nozzle_mm=0.4,
        nozzle_options=[0.4, 0.6, 0.8, 1.0],
        build_x=500, build_y=500, build_z=500,
        layer_min_mm=0.10, layer_max_mm=0.40,
        max_speed_mm_s=200, max_accel_mm_s2=3000, max_flow_mm3_s=15,
        max_bed_temp_c=110, max_hotend_temp_c=280,
        enclosure=False,
        materials=["PLA", "PETG", "ABS", "ASA", "TPU"],
        extruder="direct",
        bed_leveling="abl",
        bed_surface="PEI 弹簧钢板",
        kinematics="corexy",
        recommended_max_overhang_deg=55,
        recommended_max_bridge_mm=40,
        dimensional_tolerance_mm=0.35,
        notes="大件热收缩明显，长跨度建议加肋或分件；公差比小机器更宽松。",
        sources="大幅面机器通用规格；公差与桥接为经验值。",
    ),
    _p(
        id="resin-generic-405",
        name="通用光固化（树脂）打印机 405nm",
        manufacturer="通用",
        nozzle_mm=0.0,  # 树脂机没有喷嘴
        nozzle_options=[0.0],
        build_x=130, build_y=80, build_z=160,
        layer_min_mm=0.01, layer_max_mm=0.10,
        max_speed_mm_s=0, max_accel_mm_s2=0, max_flow_mm3_s=0,
        max_bed_temp_c=0, max_hotend_temp_c=0,
        enclosure=True,
        materials=["树脂"],
        filament_diameter_mm=0.0,
        extruder="resin",
        bed_leveling="manual",
        bed_surface="离型膜 + 平台",
        kinematics="resin",
        # 光固化的悬垂/桥接限制比 FDM **更严**，不是更松 —— 这一点旧值写反了。
        # 旧值 80° / 200mm 实际等于「随便悬空」，而它会作为硬约束注入 AI 提示词
        #（「悬垂不超过 80°，桥接不超过 200mm」），于是 AI 会放心地做出
        # 大悬垂、长跨度而不去加支撑/倒角，打出来就是拉丝、塌陷、脱层。
        # 依据 DfAM 工艺限值表（earthtojake/text-to-cad 的 dfam-check，
        # 汇总 Hubs / Formlabs / EOS 设计指南）：
        #   SLA/DLP 自支撑角 30°（FDM 45°）、最大无支撑桥接 5mm（FDM 10mm）。
        # 光固化是逐层固化后从离型膜上拉起，悬垂处没有可依附的上一层，
        # 所以比 FDM 更依赖支撑，角度门槛必须更小。
        recommended_max_overhang_deg=30,
        recommended_max_bridge_mm=5,
        dimensional_tolerance_mm=0.10,
        notes=(
            "光固化不属于 FDM：没有喷嘴与层内走线，因此本软件会跳过喷嘴相关的薄壁判断，"
            "改按「最小可成型细节约 0.1–0.2mm」评估，并提示需要支撑与后固化。"
            "注意悬垂门槛比 FDM 更严（30° vs 45°）：逐层固化时悬垂处没有可依附的上一层，"
            "超过 30° 就需要加支撑，否则会拉丝、塌陷或脱层。"
        ),
        sources="通用 LCD 光固化规格；悬垂 30°、无支撑桥接 5mm 取自 DfAM 工艺限值表（Formlabs / EOS 设计指南）。",
    ),
    _p(
        id="small-detail-02",
        name="精细件打印机 100×100×100（0.2mm 喷嘴）",
        manufacturer="通用",
        nozzle_mm=0.2,
        nozzle_options=[0.2, 0.3],
        build_x=100, build_y=100, build_z=100,
        layer_min_mm=0.04, layer_max_mm=0.15,
        max_speed_mm_s=60, max_accel_mm_s2=1500, max_flow_mm3_s=3,
        max_bed_temp_c=80, max_hotend_temp_c=250,
        enclosure=False,
        materials=["PLA", "PETG"],
        extruder="direct",
        bed_leveling="manual",
        bed_surface="PEI 弹簧钢板",
        kinematics="cartesian",
        recommended_max_overhang_deg=45,
        recommended_max_bridge_mm=12,
        dimensional_tolerance_mm=0.15,
        notes="小喷嘴适合模型、齿轮等精细件，但成型空间很小、速度慢。",
        sources="小喷嘴机器通用规格；桥接上限随喷嘴缩小而明显下降。",
    ),
]

_BUILTIN_BY_ID: dict[str, PrinterProfile] = {p.id: p for p in BUILTIN_PRINTERS}
DEFAULT_PRINTER_ID = "generic-220-04"


# --------------------------------------------------------------------------
# 工艺约束推导
# --------------------------------------------------------------------------
def resolve_process(profile: PrinterProfile, material: str | None = None) -> ProcessConstraints:
    """把机型 + 材料翻译成具体的模型设计约束。

    推导依据（FDM 通用规律，均写进 notes 让用户能看懂为什么）：
      * 可靠壁厚 ≈ 3 × 喷嘴直径 —— 两圈走线在层间结合处最容易开裂，三圈才稳定。
      * 竖直孔径 ≈ 2 × 喷嘴 + 0.4mm 余量 —— 孔越小，材料收缩与圆弧插补误差越容易把它堵上。
      * 水平孔（沿 Z 轴的孔在侧壁上的开口）可以小一些：不需要材料跨过孔顶。
      * 最小凸起 ≈ 1.5 × 喷嘴 —— 低于这个尺寸的凸字/凸点往往被下一层压塌。
      * 活动件间隙 ≈ 0.6 × 喷嘴，且不小于 0.2mm。
      * 桥接上限直接采用机型经验值（受冷却与材料影响远大于受几何影响）。
      * $fn：OpenSCAD 的圆柱由内接多边形逼近，允许偏差与夹范围都由 `resolve_fn` 一处决定。
        这里不重复那套数字（公式写在 `resolve_fn` 的 docstring 里）——
        旧文本在这里抄了一份 ÷2.2 / [24,160] / sqrt(8·…) 的说法，与代码里的
        ÷16 / [48,160] / sqrt(4·…) 互相矛盾，拿文档去复算 $fn 的人会得出与产品不同的数。
    """
    is_resin = profile.extruder == "resin" or profile.nozzle_mm <= 0
    material_key = (material or (profile.materials[0] if profile.materials else "PLA")).upper()
    notes: list[str] = []

    # $fn 与模型尺寸有关（模型越大，同样的允许误差需要越多分段）。
    # 这里按这台机器的典型成型尺寸的一半作为参考模型尺寸，
    # 编译时还会用真实模型尺寸再算一次（见 compiler 调用处的 fn 参数）。
    reference_size = max(
        10.0,
        min(profile.build_x, profile.build_z if profile.build_z > 0 else profile.build_x) * 0.5,
    )
    fn = resolve_fn(profile, reference_size, is_resin)

    if is_resin:
        min_wall = 0.40
        min_hole = 0.30
        min_hole_vertical = 0.30
        min_protrusion = 0.20
        clearance = 0.15
        rough_min_dim = 0.40
        notes.append("光固化机型按最小可成型细节（约 0.2mm）评估，不使用喷嘴相关规则。")
    else:
        n = profile.nozzle_mm
        # 竖直外墙的可靠壁厚 ≈ 3 × 喷嘴 —— 两圈走线在层间结合处最容易开裂，三圈才稳定。
        min_wall = 3.0 * n
        # 「零件最小外形尺寸」是另一回事：一层 2 圈走线就能成型，
        # 而且小件不打支撑、不受力，所以门槛低于壁厚要求（2 × 喷嘴）。
        # 两者必须分开，否则壁厚检查会被更严格的尺寸检查永远盖住。
        rough_min_dim = 2.0 * n
        min_hole = 2.0 * n + 0.4
        min_hole_vertical = 1.2 * n + 0.2
        min_protrusion = 1.5 * n
        clearance = max(0.6 * n, 0.20)
        notes.append(
            f"喷嘴 {n}mm：竖直外墙建议不小于 {min_wall:.1f}mm（3 倍喷嘴，层间强度才够），"
            f"零件最小外形尺寸不低于 {rough_min_dim:.1f}mm（2 倍喷嘴）。"
        )
        if n >= 0.6:
            notes.append("大喷嘴（≥0.6mm）出料快，细小文字与 0.5mm 以下细节会糊在一起，建议放大或改用浮雕。")
        if n <= 0.25:
            notes.append("小喷嘴（≤0.25mm）细节好但速度慢，桥接能力明显下降。")

    # 层高上限不能超过喷嘴直径的约 0.8 倍，否则挤出截面压不住
    max_layer = min(profile.layer_max_mm, 0.8 * profile.nozzle_mm) if not is_resin else profile.layer_max_mm
    max_layer = max(max_layer, profile.layer_min_mm)
    # 推荐层高按喷嘴的一半取（0.4 喷嘴 → 0.2mm，0.8 喷嘴 → 0.4mm），
    # 再用机型标称范围夹住。刻意不写死 0.2mm：大喷嘴用 0.2mm 层高是浪费机器能力，
    # 小喷嘴用 0.2mm 又可能超过它的合理范围。
    if is_resin:
        default_layer = round(min(max(0.05, profile.layer_min_mm * 4), max_layer), 3)
    else:
        default_layer = round(min(max(profile.nozzle_mm * 0.5, profile.layer_min_mm), max_layer), 3)

    if not is_resin and profile.layer_max_mm > 0.8 * profile.nozzle_mm:
        notes.append(
            f"机型标称最高层高 {profile.layer_max_mm:.2f}mm 超过喷嘴 {profile.nozzle_mm}mm 的 0.8 倍，"
            f"已按 {max_layer:.2f}mm 收窄以免挤出不足。"
        )

    heat = MATERIAL_HEAT_DEFlection.get(material_key)
    if heat is not None:
        if not profile.enclosure and heat >= 90:
            notes.append(
                f"{material_key} 的热变形温度约 {heat}℃，而该机型为开放式结构："
                "大件容易翘边或层间开裂，建议改用 PLA/PETG 或加装封闭罩。"
            )
        if profile.max_bed_temp_c and profile.max_bed_temp_c < heat - 40 and not is_resin:
            notes.append(f"该机型热床最高 {profile.max_bed_temp_c}℃，对 {material_key} 偏低，首层附着可能不足。")

    diagonal = (profile.build_x**2 + profile.build_y**2) ** 0.5
    if profile.kinematics == "delta":
        notes.append("Delta 机型为圆形有效区域，本软件按外接正方形的保守值判断，实际圆角处空间更小。")

    return ProcessConstraints(
        min_wall_mm=round(min_wall, 2),
        min_hole_dia_mm=round(min_hole, 2),
        min_hole_dia_vertical_mm=round(min_hole_vertical, 2),
        min_protrusion_mm=round(min_protrusion, 2),
        clearance_mm=round(clearance, 2),
        max_layer_mm=round(max_layer, 3),
        default_layer_mm=default_layer,
        max_horizontal_span_mm=round(profile.recommended_max_bridge_mm, 1),
        max_overhang_deg=int(profile.recommended_max_overhang_deg),
        max_build_x=profile.build_x,
        max_build_y=profile.build_y,
        max_build_z=profile.build_z,
        max_build_diagonal_mm=round(diagonal, 1),
        fn=fn,
        rough_min_dim_mm=round(rough_min_dim, 2),
        notes=notes,
    )


def resolve_fn(profile: PrinterProfile, model_size_mm: float, is_resin: bool | None = None) -> int:
    """按喷嘴与模型尺寸决定 OpenSCAD 的 $fn（圆柱/球体的分段数）。

    推导：OpenSCAD 用内接正多边形逼近圆，弦到弧的最大偏差
        error ≈ R · (1 − cos(π / n))，其中 R = 直径 / 2。
    反解出 n ≈ π / sqrt(4 · error / 直径)。
    取 error = 喷嘴 / FN_TOLERANCE_DIVISOR，于是喷嘴越细、模型越大，分段越多。

    实测参考（0.4mm 喷嘴、允许偏差 0.025mm）：Ø20 → 48 段（下限）、
    Ø80 → 约 88 段、Ø130 → 约 113 段。用户反馈过"圆看着不圆"，
    所以这里按**看得见的标准**取，而不是"物理上够用就行"。
    """
    if is_resin is None:
        is_resin = profile.extruder == "resin" or profile.nozzle_mm <= 0
    if is_resin:
        # 光固化没有挤出宽度概念，按像素尺寸给一个足够平滑的固定值即可
        return max(64, FN_MIN)
    tolerance = max(profile.nozzle_mm, 0.1) / FN_TOLERANCE_DIVISOR
    diameter = max(float(model_size_mm), 10.0)
    fn = 3.14159265358979 / (4.0 * tolerance / diameter) ** 0.5
    return int(max(FN_MIN, min(FN_MAX, round(fn))))


def material_density(material: str | None, fallback: float = 1.24) -> float:
    if not material:
        return fallback
    return MATERIAL_DENSITY.get(material.upper(), fallback)


# --------------------------------------------------------------------------
# 约束的紧凑表示（用于注入 AI 提示词）
# --------------------------------------------------------------------------
def compact_constraints(profile: PrinterProfile, constraints: ProcessConstraints, material: str | None) -> str:
    """生成一段简短、可直接塞进提示词的打印机约束描述。"""
    is_resin = profile.extruder == "resin" or profile.nozzle_mm <= 0
    lines = [
        f"目标打印机：{profile.name}"
        + (f"（{profile.manufacturer}）" if profile.manufacturer and profile.manufacturer != "通用" else ""),
    ]
    if is_resin:
        lines.append("工艺：光固化（树脂）—— 无喷嘴，输出实体模型即可")
    else:
        lines.append(
            f"喷嘴 {profile.nozzle_mm}mm，耗材 {profile.filament_diameter_mm}mm，"
            f"送料 {('近端直驱' if profile.extruder == 'direct' else '远端送料')}"
        )
    lines += [
        f"成型范围：{profile.build_x:.0f} × {profile.build_y:.0f} × {profile.build_z:.0f} mm"
        f"（模型三轴都不得超过）",
        f"材料：{material or (profile.materials[0] if profile.materials else 'PLA')}"
        + (f"；可用材料 {'/'.join(profile.materials)}" if profile.materials else ""),
        f"腔体：{'封闭' if profile.enclosure else '开放'}，热床最高 {profile.max_bed_temp_c}℃，"
        f"喷嘴最高 {profile.max_hotend_temp_c}℃",
        f"最小壁厚 {constraints.min_wall_mm:.1f}mm（低于此值层间强度不足）",
        f"最小孔径 {constraints.min_hole_dia_mm:.1f}mm（竖直孔可到 "
        f"{constraints.min_hole_dia_vertical_mm:.1f}mm）；凸起/浮雕不小于 {constraints.min_protrusion_mm:.1f}mm",
        f"活动件间隙 {constraints.clearance_mm:.2f}mm；配合公差 ±{profile.dimensional_tolerance_mm:.2f}mm",
        f"悬垂不超过 {constraints.max_overhang_deg}°（超过需要支撑），"
        f"桥接不超过 {constraints.max_horizontal_span_mm:.0f}mm",
        f"推荐层高 {constraints.default_layer_mm:.2f}mm（可用范围 "
        f"{profile.layer_min_mm:.2f}–{constraints.max_layer_mm:.2f}mm）",
        f"文件顶部使用 $fn = {constraints.fn};",
        f"最小可成型尺寸约 {constraints.rough_min_dim_mm:.1f}mm，避免更细的孤立特征",
    ]
    return "\n".join(f"- {line}" for line in lines)


# --------------------------------------------------------------------------
# 注册表（内置 + 用户自定义）
# --------------------------------------------------------------------------
def _sanitize_id(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\-]+", "-", (value or "").strip().lower()).strip("-")
    return cleaned[:40] or f"printer-{uuid.uuid4().hex[:8]}"


class PrinterRegistry:
    """内置机型 + 用户自定义机型。用户配置存在 %APPDATA%\\AI-3D-Forge\\printers.json。"""

    def __init__(self, store_file: Path | None = None) -> None:
        self._file = store_file or (paths().data_dir / "printers.json")
        self._custom: dict[str, PrinterProfile] = {}
        self._selected_id: str = DEFAULT_PRINTER_ID
        self._selected_material: str | None = None
        self._load()

    # ---------------- 读写 ----------------
    def _load(self) -> None:
        if not self._file.is_file():
            return
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("打印机配置损坏（%s），已忽略并使用内置机型", exc)
            return
        if not isinstance(raw, dict):
            return
        for item in raw.get("printers") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            profile = PrinterProfile.from_dict(item, builtin=False)
            if profile.id in _BUILTIN_BY_ID:
                # 用户「改过」的内置机型：以覆盖形式保存，仍标为内置（不可删）
                profile.builtin = True
            self._custom[profile.id] = profile
        selected = raw.get("selected_id")
        if isinstance(selected, str) and selected:
            self._selected_id = selected
        material = raw.get("material")
        if isinstance(material, str) and material:
            self._selected_material = material

    def _save(self) -> None:
        payload = {
            "schema": SCHEMA_VERSION,
            "selected_id": self._selected_id,
            "material": self._selected_material,
            "printers": [p.to_dict() for p in self._custom.values()],
        }
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._file)
        except OSError as exc:
            log.warning("写入打印机配置失败：%s", exc)

    # ---------------- 查询 ----------------
    def all(self) -> list[PrinterProfile]:
        merged: dict[str, PrinterProfile] = dict(_BUILTIN_BY_ID)
        for pid, profile in self._custom.items():
            merged[pid] = profile
        # 内置在前（按定义顺序），自定义在后（按名称）
        ordered = [merged[p.id] for p in BUILTIN_PRINTERS if p.id in merged]
        extras = [p for pid, p in merged.items() if pid not in _BUILTIN_BY_ID]
        extras.sort(key=lambda p: p.name)
        return ordered + extras

    def get(self, printer_id: str | None) -> PrinterProfile | None:
        if not printer_id:
            return None
        if printer_id in self._custom:
            return self._custom[printer_id]
        return _BUILTIN_BY_ID.get(printer_id)

    def selected_id(self) -> str:
        return self._selected_id

    def selected_material(self) -> str | None:
        return self._selected_material

    def selected(self) -> PrinterProfile:
        profile = self.get(self._selected_id)
        if profile is None:
            profile = _BUILTIN_BY_ID[DEFAULT_PRINTER_ID]
            self._selected_id = profile.id
        return profile

    def select(self, printer_id: str, material: str | None = None) -> PrinterProfile:
        profile = self.get(printer_id)
        if profile is None:
            raise KeyError(printer_id)
        self._selected_id = profile.id
        if material is not None:
            self._selected_material = material.strip() or None
        elif self._selected_material and self._selected_material not in profile.materials:
            # 换机后原材料不可用则回退到该机首选材料
            self._selected_material = None
        self._save()
        return profile

    def effective_material(self, material: str | None = None) -> str:
        if material:
            return material
        if self._selected_material:
            return self._selected_material
        profile = self.selected()
        return profile.materials[0] if profile.materials else "PLA"

    # ---------------- 增删改 ----------------
    def create(self, payload: dict[str, Any]) -> PrinterProfile:
        data = dict(payload or {})
        requested = _sanitize_id(str(data.get("id") or data.get("name") or ""))
        if requested in _BUILTIN_BY_ID or requested in self._custom:
            requested = f"{requested}-{uuid.uuid4().hex[:4]}"
        data["id"] = requested
        data.setdefault("name", "自定义打印机")
        profile = PrinterProfile.from_dict(data, builtin=False)
        profile.updated_at = _now()
        self._custom[profile.id] = profile
        self._save()
        return profile

    def update(self, printer_id: str, payload: dict[str, Any]) -> PrinterProfile:
        existing = self.get(printer_id)
        if existing is None:
            raise KeyError(printer_id)
        merged = existing.to_dict()
        for key, value in (payload or {}).items():
            if key in merged and key not in ("id", "builtin"):
                merged[key] = value
        profile = PrinterProfile.from_dict(merged, builtin=existing.builtin)
        profile.id = printer_id
        profile.updated_at = _now()
        self._custom[printer_id] = profile
        self._save()
        return profile

    def delete(self, printer_id: str) -> bool:
        if printer_id in _BUILTIN_BY_ID and printer_id not in self._custom:
            return False  # 纯内置机型不可删
        if printer_id not in self._custom:
            return False
        del self._custom[printer_id]
        if self._selected_id == printer_id:
            self._selected_id = DEFAULT_PRINTER_ID
        self._save()
        return True

    def reset_custom(self) -> None:
        """清空所有用户改动，恢复内置库原样（用于排障）。"""
        self._custom.clear()
        self._selected_id = DEFAULT_PRINTER_ID
        self._selected_material = None
        self._save()


def _now() -> str:
    import datetime

    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


_registry: PrinterRegistry | None = None


def registry(refresh: bool = False) -> PrinterRegistry:
    global _registry
    if _registry is None or refresh:
        _registry = PrinterRegistry()
    return _registry


def reset_for_tests() -> None:
    global _registry
    _registry = None


def material_options(profiles: Iterable[PrinterProfile] | None = None) -> list[str]:
    """所有机型可用材料的并集（供界面下拉使用）。"""
    seen: list[str] = []
    for profile in profiles or [p for p in BUILTIN_PRINTERS]:
        for material in profile.materials:
            if material not in seen:
                seen.append(material)
    return seen
