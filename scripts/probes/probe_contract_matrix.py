"""改动契约矩阵：拿真实项目源码，逐条检查「用户说什么 → 程序授权改什么」。

这是「说改 A，A 没变却改了别的」的直接判据：契约就是授权书，
授权错了，后面 AI 改得再对也是错的。每条都写明期望，
任何一条不符都算失败（并打印实际授权内容，便于定位）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aiforge import scad as scad_src  # noqa: E402
from aiforge.changes import build_change_contract  # noqa: E402
from aiforge.design import DesignSpec  # noqa: E402

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
print(f"项目：{project.name}（第 2 版，{len(params)} 个顶层参数）\n")

# (指令, 期望 target_values 里必须有的键→值, 绝对不许被授权的参数名关键词)
CASES: list[tuple[str, dict[str, float], tuple[str, ...]]] = [
    ("把高度改成50mm。", {"total_h": 50.0}, ()),
    ("把总高改到 80mm。", {"total_h": 80.0}, ()),
    # 加孔：结构改动，不能顺手把外径当成"增加10mm"
    ("在顶部正中间增加一个直径10mm的圆孔，要完全贯通到底部。", {}, ("outer_d",)),
    ("在底部增加4个直径5mm的圆孔。", {}, ("outer_d",)),
    # 改孔径：这一版源码里还没有孔的参数，不能授权任何整体尺寸
    # （等孔的参数存在时应当解析到它；这里判据是「绝不许落到外径上」）
    ("把这个孔的内径改成8mm。", {}, ("outer_d",)),
    # 改外径（正对照）
    ("把外径改成90mm。", {"outer_d": 90.0}, ()),
    ("把壁厚改成3mm。", {"wall_t": 3.0}, ()),
    # 明确冻结的项不能被授权
    ("高度不变，把外径改成70mm。", {"outer_d": 70.0}, ("total_h",)),
    # 底座加厚：必须落到「底部…厚度」这个参数上（以前什么都没匹配到 → 契约空转）
    ("把底座加厚到6mm。", {"bottom_t": 6.0}, ("wall_t", "outer_d")),
    # 相对改动
    ("把高度增加20mm。", {"total_h": 70.0}, ()),
    ("把外径减小10mm。", {"outer_d": 70.0}, ()),
    # 整体缩放
    ("整体缩小到原来的80%。", {}, ()),
]

failures: list[str] = []
for instruction, expect_targets, forbidden in CASES:
    contract = build_change_contract(instruction, spec, params, labels)
    problems: list[str] = []
    for name, want in expect_targets.items():
        got = contract.target_values.get(name)
        if got is None or abs(float(got) - want) > 1e-6:
            problems.append(f"目标值 {name} 期望 {want:g}，实际 {got}")
    for token in forbidden:
        if any(token in name for name in (*contract.changed, *contract.target_values)):
            problems.append(f"不该授权改 {token}（changed={contract.changed}）")
    if "缩小到" in instruction or "缩放" in instruction:
        if abs(float(contract.scale_factor or 0) - 0.8) > 1e-6:
            problems.append(f"缩放比例未识别：{contract.scale_factor}")
    if "不变" in instruction and not contract.frozen:
        problems.append("用户说了「高度不变」，但没有进入 frozen")
    status = "OK" if not problems else "FAIL"
    if problems:
        failures.append(instruction)
    print(f"[{status}] {instruction}")
    print(f"       changed={contract.changed}")
    print(f"       targets={contract.target_values} frozen={contract.frozen} "
          f"follow={contract.follow} scale={contract.scale_factor}")
    for item in problems:
        print(f"       ✗ {item}")

print("\n" + "=" * 70)
print(f"契约矩阵：通过 {len(CASES) - len(failures)} / {len(CASES)}")
for item in failures:
    print(f"  ✗ {item}")
print("=" * 70)
raise SystemExit(1 if failures else 0)
