"""量一量那个「y=145.83」：模型真有那么长，还是测量读错了？

直接对当前项目的 model.stl 用产品自己的解析器读三角面片，输出真实包围盒，
并找出超出 y>110 的面片 —— 真有突出的结构就会有一整片面片在那儿。
用法：python probe_stl_measure.py <项目目录>
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aiforge import mesh  # noqa: E402

project = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if project is None or not project.exists():
    projects = sorted(
        (ROOT / "build" / "work" / "modify-matrix" / "projects").rglob("未命名模型-*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    project = projects[0]
print(f"项目：{project}")

stl = project / "model.stl"
points, binary = mesh.read_triangles(stl)
xs = [p[0] for p in points]
ys = [p[1] for p in points]
zs = [p[2] for p in points]
print(f"格式：{'二进制' if binary else 'ASCII'}，顶点 {len(points)} 个")
print(f"顶点包围盒：x {min(xs):.2f}…{max(xs):.2f}（{max(xs) - min(xs):.2f}）"
      f"  y {min(ys):.2f}…{max(ys):.2f}（{max(ys) - min(ys):.2f}）"
      f"  z {min(zs):.2f}…{max(zs):.2f}（{max(zs) - min(zs):.2f}）")

metrics = mesh.analyze(stl)
print(f"产品测量：bbox={metrics.bbox} 体积={metrics.volume_mm3:.0f} mm³ "
      f"封闭={metrics.watertight} 件数={metrics.solid_parts} 三角面={metrics.triangles}")

triangles = [points[i : i + 3] for i in range(0, len(points), 3)]
far = [t for t in triangles if any(p[1] > 110 for p in t)]
print(f"y>110 的三角面：{len(far)} 个（共 {len(triangles)} 个）")
if far:
    fy = [p[1] for t in far for p in t]
    fx = [p[0] for t in far for p in t]
    fz = [p[2] for t in far for p in t]
    print(f"  它们的范围：x {min(fx):.2f}…{max(fx):.2f}，y {min(fy):.2f}…{max(fy):.2f}，"
          f"z {min(fz):.2f}…{max(fz):.2f}")
    for t in far[:4]:
        print("   ", [(round(p[0], 2), round(p[1], 2), round(p[2], 2)) for p in t])
