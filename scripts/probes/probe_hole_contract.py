"""为什么「加一个直径10mm的圆孔」会把外径也改了？

对**真实归档的那一版源码**重放契约构建，看这次改动被授权了哪些东西。
判据：外径（outer_d）绝不能出现在 changed / target_values 里 ——
用户说的是「直径10mm的圆孔」，那个 10mm 属于孔，不属于外径。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aiforge import scad as scad_src  # noqa: E402
from aiforge.changes import build_change_contract  # noqa: E402
from aiforge.design import DesignSpec, normalize_name  # noqa: E402

projects = sorted(
    (ROOT / "build" / "work" / "modify-matrix" / "projects").rglob("未命名模型-*"),
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)
project = projects[0]
meta = json.loads((project / "meta.json").read_text(encoding="utf-8"))
source = (project / "revisions" / "r002" / "model.scad").read_text(encoding="utf-8")
spec = DesignSpec.from_dict(meta["spec"])
params = scad_src.extract_top_level_numbers(source)
labels = scad_src.extract_param_labels(source)
print(f"项目：{project.name}，第 2 版源码 {len(source.splitlines())} 行")
print(f"顶层参数：{params}")
print("规格里用户点名的项：")
for item in spec.specs:
    if item.user_specified:
        print(f"  {item.name} / {item.label} = {item.value} → 归一化 "
              f"{normalize_name(item.name)} / {normalize_name(item.label or '')}")

instruction = "在顶部正中间增加一个直径10mm的圆孔，要完全贯通到底部。"
contract = build_change_contract(instruction, spec, params, labels)
print(f"\n指令：{instruction}")
print(f"  changed      = {contract.changed}")
print(f"  target_values= {contract.target_values}")
print(f"  follow       = {contract.follow}")
print(f"  外径被授权改了吗："
      f"{'是（BUG）' if any('outer' in n or 'diameter' in n for n in contract.changed) else '否'}")
