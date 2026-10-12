"""螺纹构造实验：找出「放进布尔运算也不会被 CGAL 丢掉」的写法。

背景（实测）：自建 polyhedron 螺旋牙单独渲染完全正常（封闭、无开放边），
但一放进 union/difference，OpenSCAD 就报 "The given mesh is not closed!"
并**静默丢掉整块几何** —— 于是模型变成一个光滑圆筒，用户拿到的东西拧不上。

本脚本批量生成若干变体、逐个编译、用项目自己的 mesh 模块量：
  包围盒 / 封闭 / 连通件 / 外半径起伏（牙深） / 编译耗时
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
WORK = Path(__file__).resolve().parent / "variants"

LIB_HEAD = """
function tooth_flat(p) =
    let (f = (p % 1 + 1) % 1)
    f < 0.125 ? 1 :
    f < 0.5   ? 1 - (f - 0.125) / 0.375 :
    f < 0.625 ? 0 :
                (f - 0.625) / 0.375;

function tooth_tri(p) =
    let (f = (p % 1 + 1) % 1)
    1 - abs(2 * f - 1);

module metric_thread(d, pitch_, length, r_in_drop = 1.5, flat = true,
                     fn = 64, slices_per_turn = 16, depth_ratio = 0.61) {
    r_major = d / 2;
    depth   = pitch_ * depth_ratio;
    r_minor = r_major - depth;
    r_in    = r_minor - r_in_drop;
    turns   = length / pitch_;
    na      = max(24, fn);
    nz      = max(4, ceil(turns * slices_per_turn));
    dz      = length / nz;

    function radius_at(i, j) =
        let (z = i * dz, theta = j * 360 / na, p = theta / 360 - z / pitch_)
        r_minor + depth * (flat ? tooth_flat(p) : tooth_tri(p));

    outer_pts = [for (i = [0 : nz]) for (j = [0 : na - 1])
                    let (theta = j * 360 / na, r = radius_at(i, j))
                    [r * cos(theta), r * sin(theta), i * dz]];
    inner_pts = [for (i = [0 : nz]) for (j = [0 : na - 1])
                    let (theta = j * 360 / na)
                    [r_in * cos(theta), r_in * sin(theta), i * dz]];

    o = function(i, j) i * na + (j % na);
    n_out = (nz + 1) * na;
    inn = function(i, j) n_out + i * na + (j % na);

    faces = concat(
        [for (i = [0 : nz - 1]) for (j = [0 : na - 1])
            [o(i, j), o(i, j + 1), o(i + 1, j + 1), o(i + 1, j)]],
        [for (i = [0 : nz - 1]) for (j = [0 : na - 1])
            [inn(i, j), inn(i + 1, j), inn(i + 1, j + 1), inn(i, j + 1)]],
        [for (j = [0 : na - 1]) [inn(0, j), inn(0, j + 1), o(0, j + 1), o(0, j)]],
        [for (j = [0 : na - 1]) [inn(nz, j), inn(nz, j + 1), o(nz, j + 1), o(nz, j)]]
    );
    polyhedron(points = concat(outer_pts, inner_pts), faces = faces, convexity = 6);
}

module adapter(flat, r_in_drop, slices_per_turn, fn) {
    difference() {
        union() {
            cylinder(d = 72 - 2 * 0.61 * 0.75, h = 10, $fn = fn);
            metric_thread(72, 0.75, 8.5, r_in_drop, flat, fn, slices_per_turn);
        }
        translate([0, 0, 1.6]) metric_thread(67.7, 0.75, 8.4, r_in_drop, flat, fn, slices_per_turn);
        translate([0, 0, -0.01]) cylinder(d = 65, h = 1.62, $fn = fn);
    }
}
"""

VARIANTS = {
    # 名称: (flat, r_in_drop, slices_per_turn, fn)
    "v1_flat_16_64": (True, 1.5, 16, 64),
    "v2_tri_16_64": (False, 1.5, 16, 64),
    "v3_flat_24_96": (True, 1.5, 24, 96),
    "v4_flat_deep": (True, 4.0, 16, 64),
    "v5_tri_32_96": (False, 3.0, 32, 96),
    "v6_flat_8_48": (True, 1.5, 8, 48),
}


def modulation(points, z_lo: float, z_hi: float, bins: int = 72) -> float | None:
    per_bin: dict[int, float] = {}
    for x, y, z in points:
        if not (z_lo <= z <= z_hi):
            continue
        index = int(((math.atan2(y, x) + math.pi) / (2 * math.pi)) * bins) % bins
        radius = math.hypot(x, y)
        per_bin[index] = max(per_bin.get(index, 0.0), radius)
    if len(per_bin) < bins // 2:
        return None
    values = list(per_bin.values())
    return round(max(values) - min(values), 3)


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    print(f"{'变体':<16}{'编译s':>7}{'包围盒':>16}{'封闭':>7}{'件数':>5}{'牙深mm':>9}  结论")
    for name, (flat, drop, spt, fn) in VARIANTS.items():
        scad = WORK / f"{name}.scad"
        stl = WORK / f"{name}.stl"
        scad.write_text(
            LIB_HEAD + f"\nadapter({'true' if flat else 'false'}, {drop}, {spt}, {fn});\n",
            encoding="utf-8",
        )
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
            print(f"{name:<16}{elapsed:>7.1f}{'—':>16}{'—':>7}{'—':>5}{'—':>9}  没有产出")
            continue
        metrics = mesh.analyze(stl)
        points, _ = mesh.read_triangles(stl)
        zs = [p[2] for p in points]
        mid = (min(zs) + max(zs)) / 2
        depth = modulation(points, mid - 0.3, mid + 0.3)
        ok = metrics.watertight and metrics.solid_parts == 1 and (depth or 0) > 0.3 and not errors
        verdict = "可用" if ok else ("几何被丢掉" if (depth or 0) < 0.05 else "有缺陷")
        print(
            f"{name:<16}{elapsed:>7.1f}{str(tuple(round(v, 1) for v in metrics.bbox)):>16}"
            f"{str(metrics.watertight):>7}{metrics.solid_parts:>5}{str(depth):>9}  {verdict}"
            + (f"  ({len(errors)} 个 ERROR)" if errors else "")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
