"""改名桥单元验证：规格名 height ↔ 源码参数 total_h（真实项目源码，不调 AI）。

判据（全部针对本次实测到的失败）：
  1. resolve_param_name("height", …, labels) → "total_h"；
  2. 契约 changed 里**同时**有 height 与 total_h，target_values 落在 total_h；
  3. 一次只改 total_h 100→50 的候选：判 ok、不再算「未授权改动」；
  4. follow 里出现 rib_z1 / rib_z0 / grip_h（同维度家族），且不在 unchanged 里；
  5. _apply_targets 的确定性兜底真能写出 total_h=50 并把 rib_z1 等比降到 44；
  6. 阴性对照：把改名桥关掉（不给 labels）时，同一份候选必须被判失败 ——
     证明第 3 条的好结果是桥带来的，不是判据放松换来的。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "build" / "work" / "probe-height"
sys.path.insert(0, str(ROOT / "src"))

from aiforge import jobs as jobs_mod  # noqa: E402
from aiforge import scad as scad_src  # noqa: E402
from aiforge.changes import (  # noqa: E402
    build_change_contract,
    follow_values,
    resolve_param_name,
    verify_change,
)
from aiforge.design import DesignSpec  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str) -> None:
    print(f"  [{'OK' if ok else 'FAIL'}] {label} —— {detail}")
    if not ok:
        failures.append(label)


meta_path = next((WORK / "projects").rglob("meta.json"))
meta = json.loads(meta_path.read_text(encoding="utf-8"))
source = (meta_path.parent / "model.scad").read_text(encoding="utf-8")
spec = DesignSpec.from_dict(meta["spec"])
params = scad_src.extract_top_level_numbers(source)
labels = scad_src.extract_param_labels(source)

print("=== 0. 标签解析 ===")
check("从 @param 注释里读到了 total_h 的说明", labels.get("total_h") == "总高", str(labels.get("total_h")))

print("\n=== 1. 改名桥 ===")
resolved = resolve_param_name("height", params, labels)
check("height → total_h", resolved == "total_h", str(resolved))
# 阴性对照用的参数名：既没有可用的注释标签，结尾缩写也不指向高度
# （`total_h` 这种写法靠结尾的 h 也能认出来，那是第二道网，见 §73）
opaque = {"body_extent": 100.0, "wall_t": 2.5}
check("毫无线索的名字不会被硬猜（阴性对照）",
      resolve_param_name("height", opaque) is None,
      f"无标签无缩写得到 {resolve_param_name('height', opaque)}")
check("结尾缩写也能认出高度（第二道网）",
      resolve_param_name("height", {"total_h": 100.0, "wall_t": 2.5}) == "total_h",
      f"{resolve_param_name('height', {'total_h': 100.0, 'wall_t': 2.5})}")

print("\n=== 2. 契约 ===")
contract = build_change_contract("把高度改成50mm。", spec, params, labels)
check("changed 同时含规格名与源码名", "height" in contract.changed and "total_h" in contract.changed,
      str(contract.changed))
check("目标值落在源码参数名上", contract.target_values.get("total_h") == 50.0,
      str(contract.target_values))
check("total_h 不再出现在「必须不变」清单里", "total_h" not in contract.unchanged,
      str(contract.unchanged))
for name in ("rib_z1", "rib_z0", "grip_h"):
    check(f"跟随项含 {name}", name in contract.follow, str(contract.follow))
check("跟随项不在「必须不变」清单里",
      not (set(contract.follow) & set(contract.unchanged)),
      str(sorted(set(contract.follow) & set(contract.unchanged))))
check("外径/壁厚仍在「必须不变」清单里",
      "outer_d" in contract.unchanged and "wall_t" in contract.unchanged,
      "、".join(n for n in ("outer_d", "wall_t") if n in contract.unchanged))

print("\n=== 3. AI 那次被误杀的候选 ===")
candidate = source.replace("total_h = 100;", "total_h = 50;")
print(f"  参数差异：total_h 100 → 50（其余 {len(params) - 1} 个参数原样）")
verdict = verify_change(source, candidate, contract, spec)
check("判为达标", verdict.ok, verdict.describe())
check("不再报「未授权改动」", not verdict.unauthorized, str(verdict.unauthorized))
check("不再报「未找到该参数」",
      not any("未找到" in r for r in verdict.reasons), str(verdict.reasons))
check("代价降到 0", verdict.penalty() == 0.0, f"{verdict.penalty():.2f}")

print("\n=== 3b. 阴性对照：拆掉改名桥，同一形状的候选必须判失败 ===")
# 把 total_h 换成一个「既没有注释标签、也没有缩写线索」的名字，其余完全同形；
# 若判据本身是可靠的，这时就该复现出线上那个失败：既说没改到、又说越权。
renamed = source.replace("total_h", "body_extent")
renamed_params = scad_src.extract_top_level_numbers(renamed)
narrow = build_change_contract("把高度改成50mm。", spec, renamed_params)
narrow_candidate = renamed.replace("body_extent = 100;", "body_extent = 50;")
narrow_verdict = verify_change(renamed, narrow_candidate, narrow, spec)
check("没有桥时确实判失败（说明桥是第 3 条达标的原因）",
      not narrow_verdict.ok and bool(narrow_verdict.unauthorized),
      f"ok={narrow_verdict.ok} unauthorized={narrow_verdict.unauthorized}")

print("\n=== 4. 确定性兜底 ===")
followed = follow_values(contract, params, labels)
check("沿轴的跟随项按比例算出新值", followed.get("rib_z1") == 44.0 and followed.get("rib_z0") == 6.0,
      str(followed))
check("特征自身尺寸不跟着缩（1.2mm 的筋不能被缩成 0.6mm）",
      "rib_h" not in followed and "grip_d" not in followed,
      str({k: v for k, v in followed.items()}))
surgical = jobs_mod._apply_targets(source, contract)
if surgical is None:
    check("兜底能写出改动", False, "返回 None")
else:
    got = scad_src.extract_top_level_numbers(surgical)
    check("兜底改对了 total_h", got.get("total_h") == 50.0, f"total_h={got.get('total_h')}")
    check("兜底把 rib_z1 等比降到 44", got.get("rib_z1") == 44.0, f"rib_z1={got.get('rib_z1')}")
    check("兜底没动无关参数", got.get("outer_d") == params.get("outer_d")
          and got.get("wall_t") == params.get("wall_t"),
          f"outer_d={got.get('outer_d')} wall_t={got.get('wall_t')}")
    check("兜底结果本身判达标", verify_change(source, surgical, contract, spec).ok,
          verify_change(source, surgical, contract, spec).describe())

print("\n" + "=" * 60)
print(f"改名桥：通过 {10 + 3 + 4 + 4 - len(failures)} 项，失败 {len(failures)} 项")
for item in failures:
    print(f"  ✗ {item}")
print("=" * 60)
raise SystemExit(1 if failures else 0)
