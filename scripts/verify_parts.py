"""把固定零件库的每个零件编译一遍并实测：封闭 / 单一实体 / 尺寸正确。

为什么必须这样验：库里放的是"手写几何"，一旦某个零件在 OpenSCAD 里镶嵌失败，
用户拿到的就是残缺模型 —— 而这类问题**不会报错**（OpenSCAD 退出码仍是 0）。
这里用真实编译 + 真实网格分析把每个零件钉住。
"""

from __future__ import annotations

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

CASES = [
    # (标签, 调用代码, 期望 bbox 容差)
    ("filter_adapter 默认(72→67)", "filter_adapter();", (72.0, 72.0, 8.4), 0.6),
    ("filter_adapter 77→72", "filter_adapter(male_d = 77, female_d = 72, height = 9);", (77.0, 77.0, 9.0), 0.6),
    ("thread_sleeve 72/67", "thread_sleeve();", (72.0, 72.0, 10.0), 0.6),
    ("threaded_cap 67", "threaded_cap();", (None, None, 12.0), 1.2),
]


def thread_depth(vertices, z_lo: float, z_hi: float) -> float | None:
    """量「外表面」在一条高度带里的半径跨度 = 牙深。

    两个坑都踩过：
      1. 按角度分箱取最大值再比箱间差 → 量出 0.07mm（真值 0.46mm）：
         螺旋牙在固定高度上的角向周期只有约 1.2°（0.75mm 螺距、半径 36mm），
         比网格角度步长小得多，逐箱取最大值会把牙顶牙底平均掉。
      2. 直接对带内所有顶点取极差 → 量出 2.6mm：把**内孔**的半径也算进来了。
    正确做法：高度带取一个螺距（相位走满一圈），并且只看半径较大的那一半
    （外表面），跨度即牙深；光滑圆筒的跨度只剩镶嵌噪声（≈0）。
    """
    radii = [math.hypot(p[0], p[1]) for p in vertices if z_lo <= p[2] <= z_hi]
    if len(radii) < 32:
        return None
    outer = [r for r in radii if r > (max(radii) + min(radii)) / 2]
    if len(outer) < 16:
        return None
    return round(max(outer) - min(outer), 3)


def main() -> int:
    exe = openscad.resolve_executable()
    if exe is None:
        print("没有可用的 OpenSCAD，跳过（请先安装或让软件自动安装）")
        return 2
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="parts_") as tmp:
        temp = Path(tmp)
        for label, call, expected, tol in CASES:
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
            mid = (min(zs) + max(zs)) / 2
            # 高度带取一个螺距，保证相位走满一圈
            depth = thread_depth(vertices, mid - 0.375, mid + 0.375)
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
                and (depth or 0) > 0.15
                and not errors
            )
            status = "OK  " if ok else "FAIL"
            print(
                f"  [{status}] {label}: {elapsed:.1f}s  bbox={tuple(round(v, 2) for v in m.bbox)}"
                f"  封闭={m.watertight}  件数={m.solid_parts}  牙深={depth}"
                + (f"  ERROR={errors[:1]}" if errors else "")
            )
            if not ok:
                failures.append(
                    f"{label}: watertight={m.watertight} parts={m.solid_parts} "
                    f"bbox={m.bbox} depth={depth} errors={errors[:1]}"
                )
    if failures:
        print("\n固定零件库验证失败：")
        for item in failures:
            print("  ✗", item)
        return 1
    print(f"\n固定零件库 {len(CASES)} 个零件全部通过（封闭、单一实体、尺寸正确）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
