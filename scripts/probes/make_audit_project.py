# -*- coding: utf-8 -*-
r"""给真后端界面审计造一个**真的有模型**的项目（确定性编译，不依赖 AI、不花额度）。

为什么需要它（`docs/HANDOVER.md` §7 第 4 条记的就是这套跑法）：
`webui/scripts/audit-interface.mjs` 里「会变化的数字必须用 tabular-nums」这一项，
只有在工作台**加载了真实模型**时才存在候选；空工作台永远报「0 个候选」，
看起来全清其实一步都没查。所以审计前必须有一个 `revision > 0` 且带 STL 的项目。

数据目录全部指到 `build/work/audit-*` 沙箱：绝不拿用户真实项目做审计，
也不会把测试项目留在「文档」目录里。

用法（PowerShell，三条环境变量要在同一个会话里设给后端与本脚本）::

    $env:AIFORGE_DATA_DIR='build\work\audit-data'
    $env:AIFORGE_LOCAL_DIR='build\work\audit-local'
    $env:AIFORGE_PROJECTS_DIR='build\work\audit-projects'
    build\venv\Scripts\python.exe -X utf8 scripts\probes\make_audit_project.py
    build\venv\Scripts\python.exe -X utf8 main.py --server --port 8770
    node webui\scripts\layout-audit.mjs http://127.0.0.1:8770
    node webui\scripts\audit-interface.mjs http://127.0.0.1:8770
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

SCAD = """// 界面审计件：20×20×30 的方筒，底部实心、顶部开口，侧面一个 Ø8 通孔
$fn = 64;
outer = 20;
height = 30;
wall = 3;
bottom = 4;
difference() {
    cube([outer, outer, height]);
    translate([wall, wall, bottom])
        cube([outer - 2 * wall, outer - 2 * wall, height - bottom + 0.1]);
    translate([outer, outer / 2, height / 2])
        rotate([0, 90, 0])
            cylinder(r = 4, h = outer + 2, center = true);
}
"""


def main() -> int:
    # 沙箱：没设就用 build/work 下的默认位置，仍然不碰真实用户目录
    os.environ.setdefault("AIFORGE_DATA_DIR", str(ROOT / "build" / "work" / "audit-data"))
    os.environ.setdefault("AIFORGE_LOCAL_DIR", str(ROOT / "build" / "work" / "audit-local"))
    os.environ.setdefault("AIFORGE_PROJECTS_DIR", str(ROOT / "build" / "work" / "audit-projects"))

    from aiforge import projects as projects_mod
    from aiforge import threemf
    from aiforge.compiler import compile_to_stl
    from aiforge.mesh import analyze
    from aiforge.paths import paths
    from aiforge.printers import registry

    paths(refresh=True).ensure()
    projects_mod.reset_for_tests()
    store = projects_mod.store()

    project = store.create("界面审计件")
    result = compile_to_stl(SCAD, project.dir, basename="model", fn=64)
    print(f"项目目录：{store.root}")
    print(f"编译 ok={result.ok} 用时={result.duration_s:.1f}s 丢弃几何={result.dropped_geometry}")
    if not result.ok:
        print(f"  message：{result.message}")
        print(f"  fatal：{result.fatal}")
        return 1

    metrics = analyze(project.stl_path)
    project.revision = 1
    project.summary = "界面审计用：真实编译的开口方筒"
    project.last_status = "pass"
    project.params = {"height": 30.0, "outer": 20.0}
    project.metrics = metrics.to_dict()
    project.printability = {"score": 88, "verdict": "pass", "issues": []}
    store.save(project)
    store.append_history(
        project,
        projects_mod.HistoryEntry(
            revision=1, created_at=projects_mod.now_iso(), summary="界面审计件", stl=True
        ),
    )

    # 顺带产一份真 3MF：审计与「送到切片软件」这条链路用的就是它
    info = threemf.write_3mf_from_stl(
        project.stl_path,
        project.three_mf_path,
        title=project.name,
        metadata={
            "Title": project.name,
            "Material": "PLA",
            "Printer": registry().selected().name,
        },
    )
    back = threemf.read_3mf_summary(project.three_mf_path)
    print(
        f"project id={project.id}  revision={project.revision}"
        f"  stl={project.stl_path.stat().st_size} 字节"
    )
    print(
        f"网格：封闭={metrics.watertight} 实体数={metrics.solid_parts}"
        f" 体积={metrics.volume_cm3:.2f}cm3 面数={metrics.triangles}"
    )
    print(
        f"3MF：面数={info['triangles']} 顶点={info['vertices']}"
        f" 读回 unit={back['unit']} build={back['has_build']} metadata={back['metadata']}"
    )
    print("下一步：起后端 `main.py --server --port 8770`，再跑两条审计脚本。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
