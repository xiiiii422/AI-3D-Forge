import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT / "src"))
from aiforge import mesh
projects = sorted((ROOT / "build/work/modify-matrix/projects").rglob("未命名模型-*"), key=lambda p: p.stat().st_mtime, reverse=True)
proj = projects[0]
print(f"项目：{proj.name}")
for stl in sorted(proj.rglob("model.stl")):
    rev = stl.parent.name
    m = mesh.analyze(stl)
    # 只报产品判据真正会拿来用的那组数：孔轴搜出来在哪、六个探针各量到多少
    print(f"  {rev}: 总高 {m.bbox[2]:.1f}mm  中部孔径={m.hole_diameter_mm}  "
          f"剖面(自底向上)={[None if d is None else round(d, 2) for d in m.hole_profile]}")
