"""螺纹构造实验 2：`linear_extrude(twist=...)` 螺旋牙（把牙型做成略宽于一个螺距，
让相邻两圈**重叠**而不是刚好贴面 —— 贴面会产生非流形边，实测 16 条）。

变体：
  t1: 牙型轴向宽度 = 1.00 × pitch（刚好相接）
  t2: 1.05 × pitch（重叠 5%）
  t3: 1.20 × pitch（重叠 20%）
  t4: 1.05 × pitch + 外螺纹单独与本体 union、内螺纹 difference（完整转接环）
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from pathlib import Path

# parents[2]：probes → scripts → 仓库根（原来写 parents[3] 指到盘符根，aiforge 导进来就是空跑）
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aiforge import mesh  # noqa: E402

OPENSCAD = Path(
    os.environ.get("AIFORGE_OPENSCAD", "openscad")
)
WORK = Path(__file__).resolve().parent / "twist"


def thread_module(overlap: float) -> str:
    return f"""
// 螺旋牙：linear_extrude(twist) —— 牙型略宽于一个螺距，让相邻圈重叠
module helical_thread(d, pitch, length, fn = 64, slices_per_turn = 24, overlap = {overlap}) {{
    depth   = pitch * 0.61;
    r_major = d / 2;
    r_minor = r_major - depth;
    turns   = length / pitch;
    slices  = max(16, ceil(turns * slices_per_turn));
    half    = pitch * overlap / 2;
    union() {{
        cylinder(r = r_minor, h = length, $fn = fn);
        linear_extrude(height = length, twist = -360 * turns, slices = slices, convexity = 10)
            polygon(points = [
                [r_minor, -half],
                [r_major, -pitch * 0.14],
                [r_major,  pitch * 0.14],
                [r_minor,  half]
            ]);
    }}
}}

module adapter(fn = 64, slices_per_turn = 24, overlap = {overlap}) {{
    difference() {{
        union() {{
            cylinder(d = 72 - 2 * 0.61 * 0.75, h = 10, $fn = fn);
            helical_thread(72, 0.75, 8.5, fn, slices_per_turn, overlap);
        }}
        translate([0, 0, 1.6])
            helical_thread(67.7, 0.75, 8.4, fn, slices_per_turn, overlap);
        translate([0, 0, -0.01]) cylinder(d = 65, h = 1.62, $fn = fn);
    }}
}}
"""


VARIANTS = {
    "t1_overlap_1.00": (1.00, False),
    "t2_overlap_1.05": (1.05, False),
    "t3_overlap_1.20": (1.20, False),
    "t4_ring_1.05": (1.05, True),
}


def modulation(points, z_lo: float, z_hi: float, bins: int = 72) -> float | None:
    per_bin: dict[int, float] = {}
    for x, y, z in points:
        if not (z_lo <= z <= z_hi):
            continue
        index = int(((math.atan2(y, x) + math.pi) / (2 * math.pi)) * bins) % bins
        per_bin[index] = max(per_bin.get(index, 0.0), math.hypot(x, y))
    if len(per_bin) < bins // 2:
        return None
    values = list(per_bin.values())
    return round(max(values) - min(values), 3)


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    print(f"{'变体':<18}{'编译s':>7}{'包围盒':>16}{'封闭':>7}{'件数':>5}{'非流形':>7}{'牙深mm':>9}  结论")
    for name, (overlap, full_ring) in VARIANTS.items():
        scad = WORK / f"{name}.scad"
        stl = WORK / f"{name}.stl"
        body = "adapter();" if full_ring else "helical_thread(72, 0.75, 8.5);"
        scad.write_text(thread_module(overlap) + f"\n{body}\n", encoding="utf-8")
        started = time.perf_counter()
        proc = subprocess.run(
            [str(OPENSCAD), "-o", str(stl), str(scad)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.perf_counter() - started
        errors = [ln for ln in (proc.stdout + proc.stderr).splitlines() if "ERROR" in ln]
        if not stl.is_file():
            print(f"{name:<18}{elapsed:>7.1f} 没有产出 STL：{errors[:1]}")
            continue
        m = mesh.analyze(stl)
        points, _ = mesh.read_triangles(stl)
        zs = [p[2] for p in points]
        mid = (min(zs) + max(zs)) / 2
        depth = modulation(points, mid - 0.3, mid + 0.3)
        ok = m.watertight and m.solid_parts == 1 and (depth or 0) > 0.3 and not errors
        print(
            f"{name:<18}{elapsed:>7.1f}{str(tuple(round(v, 1) for v in m.bbox)):>16}"
            f"{str(m.watertight):>7}{m.solid_parts:>5}{m.non_manifold_edges:>7}{str(depth):>9}"
            f"  {'可用' if ok else '不可用'}" + (f"  ({len(errors)} ERROR)" if errors else "")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
