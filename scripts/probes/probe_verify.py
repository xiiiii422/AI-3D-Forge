"""根因诊断：把「改高度 100→50」这条修改的**每一道校验**摊开看。

对同一个真实项目（build/work/probe-height 里的 Ø80×100 笔筒）依次做：
  1. 构建改动契约，打印 changed / unchanged / target_values；
  2. 调真实 AI 改一次 → verify_change 打印全部判据与 penalty；
  3. 按契约让 AI 再修一次 → 同样打印；
  4. 确定性兜底 _apply_targets → 打印它产出的源码与判据；
  5. _pick_least_bad 在「重做版 / 首版 / 原版」里选谁，各自的 penalty。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "build" / "work" / "probe-height"
os.environ["AIFORGE_DATA_DIR"] = str(WORK / "data")
os.environ["AIFORGE_PROJECTS_DIR"] = str(WORK / "projects")
sys.path.insert(0, str(ROOT / "src"))

from aiforge import jobs as jobs_mod  # noqa: E402
from aiforge import scad as scad_src  # noqa: E402
from aiforge.changes import build_change_contract, verify_change  # noqa: E402
from aiforge.config import store  # noqa: E402
from aiforge.design import DesignSpec  # noqa: E402
from aiforge.llm import DeepSeekClient  # noqa: E402

INSTRUCTION = "把高度改成50mm。"


def show_verdict(tag: str, verdict) -> None:
    print(f"\n--- {tag} ---")
    print(f"  ok={verdict.ok} performed={verdict.performed} spec_intact={verdict.spec_intact}")
    print(f"  unauthorized={verdict.unauthorized}")
    print(f"  target_misses={verdict.target_misses} implausible={verdict.implausible}")
    print(f"  reasons={verdict.reasons}")
    print(f"  penalty={verdict.penalty():.2f}")


def param_diff(before: str, after: str) -> str:
    a = scad_src.extract_top_level_numbers(before)
    b = scad_src.extract_top_level_numbers(after)
    diff = []
    for name, value in a.items():
        new = b.get(name)
        if new is None:
            diff.append(f"{name}: 被删除")
        elif abs(new - value) > 1e-9:
            diff.append(f"{name}: {value:g} → {new:g}")
    for name, value in b.items():
        if name not in a:
            diff.append(f"{name}: 新增 {value:g}")
    return "、".join(diff) or "（参数完全没变）"


async def main() -> int:
    meta_path = next((WORK / "projects").rglob("meta.json"))
    project_dir = meta_path.parent
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    current = (project_dir / "model.scad").read_text(encoding="utf-8")
    spec = DesignSpec.from_dict(meta["spec"])
    params = scad_src.extract_top_level_numbers(current)
    print(f"项目：{project_dir.name}，源码 {len(current.splitlines())} 行，"
          f"顶层参数 {len(params)} 个")

    contract = build_change_contract(INSTRUCTION, spec, params)
    print("\n=== 1. 改动契约 ===")
    print(f"  changed={contract.changed}")
    print(f"  unchanged={contract.unchanged}")
    print(f"  frozen={contract.frozen}")
    print(f"  target_values={contract.target_values}")
    print(f"  describe={contract.describe()}")

    api = store().config.api
    client = DeepSeekClient(store().api_key, api.base_url, api.model, api.timeout_s)

    print("\n=== 2. AI 第一次修改 ===")
    first = await client.modify_with_contract(current, contract, spec, None)
    source = scad_src.strip_code_fences(first.content)
    print(f"  参数差异：{param_diff(current, source)}")
    verdict = verify_change(current, source, contract, spec)
    show_verdict("首版判据", verdict)

    print("\n=== 3. AI 按契约重做 ===")
    repaired = await client.repair_change(current, source, verdict, contract)
    candidate = scad_src.strip_code_fences(repaired.content)
    print(f"  参数差异：{param_diff(current, candidate)}")
    recheck = verify_change(current, candidate, contract, spec)
    show_verdict("重做版判据", recheck)

    print("\n=== 4. 确定性兜底 _apply_targets ===")
    surgical = jobs_mod._apply_targets(current, contract)
    if surgical is None:
        print("  返回 None —— 说明目标参数没能写回源码（契约名与源码名对不上？）")
        print(f"  源码里的顶层数值参数名：{list(params)}")
    else:
        print(f"  参数差异：{param_diff(current, surgical)}")
        sverdict = verify_change(current, surgical, contract, spec)
        show_verdict("兜底版判据", sverdict)

    print("\n=== 5. 三版取舍 ===")
    pool = {"重做版": candidate, "首版": source, "原版": current}
    if surgical is not None:
        pool["兜底版"] = surgical
    for name, text in pool.items():
        score = verify_change(current, text, contract, spec).penalty()
        print(f"  {name}: penalty={score:.2f}")
    chosen = jobs_mod._pick_least_bad(current, [candidate, source, current], contract, spec)
    print(f"  _pick_least_bad 选中："
          f"{[k for k, v in pool.items() if v is chosen and k != '原版'] or ['原版']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
