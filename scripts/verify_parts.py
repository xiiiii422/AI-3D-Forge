"""把固定零件库的每个零件编译一遍并实测：封闭 / 单一实体 / 尺寸正确 / 螺纹在正确的一侧 / 中心镂空。

为什么必须这样验：库里放的是"手写几何"，一旦某个零件在 OpenSCAD 里镶嵌失败，
用户拿到的就是残缺模型 —— 而这类问题**不会报错**（OpenSCAD 退出码仍是 0）。
这里用真实编译 + 真实网格分析把每个零件钉住。

特别地，滤镜转接环曾有一个只在「升口径（小转大，如 67→77）」才暴露的致命 bug：
外表面按 male_d 走、内表面按 female_d 走，当 male_d < female_d 时内半径 > 外半径，
polyhedron 内外翻面 —— 外螺纹跑进孔里、内螺纹跑到外表面，编译照样"水密单一实体"、
退出码 0，但拿到的是谁都拧不上的废件。所以这里对升/降口径都分别探测
「外表面有没有外螺纹」「内表面有没有内螺纹」「中心是否真的镂空」。
"""

from __future__ import annotations

import console_utf8  # noqa: F401,E402 —— 必须最先导入：GBK 控制台下 print 中文/符号会弄崩脚本（见 src/aiforge/stdio.py）
import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # scripts/<file> → 仓库根
sys.path.insert(0, str(ROOT / "src"))

from aiforge import mesh, openscad  # noqa: E402

LIB = ROOT / "src" / "aiforge" / "parts" / "aiforge_parts.scad"

# 每个用例：(标签, 调用, 期望 bbox, 容差, 螺纹探测带, 中心镂空下限)
#   螺纹探测带 = [(which, frac_lo, frac_hi), ...]，which ∈ {"outer","inner"}：
#       outer = 该高度带上「半径较大那一半」的径向跨度（测外螺纹牙深）
#       inner = 该高度带上「半径较小那一半」的径向跨度（测内螺纹牙深）
#   中心镂空下限 = min(半径)/max(半径) 的下限；None = 不检查（如带顶盖的旋盖）。
CASES = [
    # 降口径（外螺纹更粗，沿用既有已验证布局）
    ("filter_adapter 默认(72→67)", "filter_adapter();", (72.0, 72.0, 8.4), 0.6,
     [("outer", 0.45, 0.55), ("inner", 0.45, 0.55)], 0.30),
    ("filter_adapter 77→72", "filter_adapter(male_d = 77, female_d = 72, height = 9);", (77.0, 77.0, 9.0), 0.6,
     [("outer", 0.45, 0.55), ("inner", 0.45, 0.55)], 0.30),
    # 升口径（小转大）：外螺纹在顶部外表面、内螺纹在底部内表面（已翻成大端贴平台）
    ("filter_adapter 67→77 升口径", "filter_adapter(male_d = 67, female_d = 77);", (81.7, 81.7, 8.4), 0.7,
     [("outer", 0.80, 0.90), ("inner", 0.10, 0.20)], 0.30),
    ("filter_adapter 52→82 升口径", "filter_adapter(male_d = 52, female_d = 82, height = 10);", (86.7, 86.7, 10.0), 0.7,
     [("outer", 0.80, 0.90), ("inner", 0.10, 0.20)], 0.30),
    ("thread_sleeve 72/67", "thread_sleeve();", (72.0, 72.0, 10.0), 0.6,
     [("outer", 0.45, 0.55), ("inner", 0.45, 0.55)], 0.30),
    ("threaded_cap 67", "threaded_cap();", (None, None, 12.0), 1.2,
     [("inner", 0.30, 0.45)], None),
]


def thread_depth(vertices, z_lo: float, z_hi: float, which: str = "outer") -> float | None:
    """量某个高度带上「外表面」或「内表面」的半径跨度 = 牙深。

    两个坑都踩过：
      1. 按角度分箱取最大值再比箱间差 → 量出 0.07mm（真值 0.46mm）：
         螺旋牙在固定高度上的角向周期只有约 1.2°（0.75mm 螺距、半径 36mm），
         比网格角度步长小得多，逐箱取最大值会把牙顶牙底平均掉。
      2. 不区分内外直接取极差 → 把内孔半径也算进来，量出 2.6mm。
    正确做法：高度带取一个螺距（相位走满一圈），再按 which 只取半径较大（外表面）
    或较小（内表面）的那一半，跨度即牙深；光滑圆筒的跨度只剩镶嵌噪声（≈0）。
    """
    radii = [math.hypot(p[0], p[1]) for p in vertices if z_lo <= p[2] <= z_hi]
    if len(radii) < 32:
        return None
    mid = (max(radii) + min(radii)) / 2
    sel = [r for r in radii if (r > mid if which == "outer" else r < mid)]
    if len(sel) < 16:
        return None
    return round(max(sel) - min(sel), 3)


def main() -> int:
    exe = openscad.resolve_executable()
    if exe is None:
        print("没有可用的 OpenSCAD，跳过（请先安装或让软件自动安装）")
        return 2
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="parts_") as tmp:
        temp = Path(tmp)
        for label, call, expected, tol, probes, min_hollow in CASES:
            scad = temp / "case.scad"
            stl = temp / "case.stl"
            scad.write_text(f'use <{LIB.name}>\n{call}\n', encoding="utf-8")
            started = time.perf_counter()
            proc = subprocess.run(
                [str(exe), "-o", str(stl), str(scad)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={**__import__("os").environ, "OPENSCADPATH": str(LIB.parent)},
            )
            elapsed = time.perf_counter() - started
            errors = [ln for ln in (proc.stdout + proc.stderr).splitlines() if "ERROR" in ln]
            if not stl.is_file():
                tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-6:])
                failures.append(f"{label}: 没有产出 STL（退出码 {proc.returncode}）")
                print(f"  [FAIL] {label}: 没有产出 STL（退出码 {proc.returncode}）\n{tail}")
                continue
            m = mesh.analyze(stl)
            vertices, _ = mesh.read_triangles(stl)
            zs = [p[2] for p in vertices]
            z_lo, z_hi = min(zs), max(zs)
            height = z_hi - z_lo
            radii = [math.hypot(p[0], p[1]) for p in vertices]
            hollow_ratio = min(radii) / max(radii) if max(radii) > 0 else 0.0

            bbox_ok = all(
                want is None or abs(got - want) <= tol
                for got, want in zip(m.bbox, expected)
            )
            ok = (
                m.watertight
                and m.boundary_edges == 0
                and m.non_manifold_edges == 0
                and m.solid_parts == 1
                and bbox_ok
                and not errors
            )
            depth_report = []
            for which, f0, f1 in probes:
                depth = thread_depth(vertices, z_lo + f0 * height, z_lo + f1 * height, which)
                depth_report.append(f"{which}={depth}")
                if (depth or 0) <= 0.15:
                    ok = False
            if min_hollow is not None and hollow_ratio < min_hollow:
                ok = False
            status = "OK  " if ok else "FAIL"
            print(
                f"  [{status}] {label}: {elapsed:.1f}s  bbox={tuple(round(v, 2) for v in m.bbox)}"
                f"  封闭={m.watertight} 件数={m.solid_parts} 牙深[{', '.join(depth_report)}]"
                f" 镂空比={hollow_ratio:.2f}"
                + (f"  ERROR={errors[:1]}" if errors else "")
            )
            if not ok:
                failures.append(
                    f"{label}: watertight={m.watertight} parts={m.solid_parts} "
                    f"bbox={m.bbox} depths={depth_report} hollow={hollow_ratio:.2f} errors={errors[:1]}"
                )
    if failures:
        print("\n固定零件库验证失败：")
        for item in failures:
            print("  ✗", item)
        return 1
    # 报的是「用例数」，不是「零件数」：同一个零件会跑多个参数组合（升/降口径就是两套判据），
    # 混说成「N 个零件」会让交接的人以为库里多了几个零件。
    part_ids = {label.split()[0] for label, *_ in CASES}
    print(
        f"\n固定零件库 {len(CASES)} 个用例（{len(part_ids)} 个零件）全部通过"
        "（封闭、单一实体、尺寸正确、螺纹在正确一侧、中心镂空）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
