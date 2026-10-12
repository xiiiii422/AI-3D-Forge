"""整体缩放：离线验证「契约 → 确定性缩放 → 校验」这条链真的能让模型缩小。

判据（全部离线、秒级）：
  1. 契约识别出 0.8，且**源码参数名**全部被授权（否则缩放会被判成越权）；
  2. 确定性缩放真的把尺寸参数缩到 0.8；
  3. 数量/角度类参数**不变**（筋数不能从 16 变成 13）；
  4. `verify_change` 对这版判达标（这是它以前失败的地方）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aiforge import jobs as jobs_mod  # noqa: E402
from aiforge import scad as scad_src  # noqa: E402
from aiforge.changes import build_change_contract, verify_change  # noqa: E402
from aiforge.design import DesignSpec  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str) -> None:
    print(f"  [{'OK' if ok else 'FAIL'}] {label} —— {detail}")
    if not ok:
        failures.append(label)


projects = sorted(
    (ROOT / "build" / "work" / "modify-matrix" / "projects").rglob("未命名模型-*"),
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)
project = projects[0]
meta = json.loads((project / "meta.json").read_text(encoding="utf-8"))
# 用「加厚底座」那一版（r5）当输入：它就是线上缩放失败的那一版
source = (project / "revisions" / "r005" / "model.scad").read_text(encoding="utf-8")
spec = DesignSpec.from_dict(meta["spec"])
params = scad_src.extract_top_level_numbers(source)
labels = scad_src.extract_param_labels(source)
print(f"项目：{project.name} 第 5 版，{len(params)} 个顶层参数")
print(f"  outer_d={params.get('outer_d')} total_h={params.get('total_h')} "
      f"rib_count={params.get('rib_count')}")

instruction = "整体缩小到原来的80%。"
contract = build_change_contract(instruction, spec, params, labels)
print("\n=== 1. 契约 ===")
print(f"  scale_factor={contract.scale_factor}")
print(f"  changed={contract.changed}")
print(f"  unchanged={contract.unchanged}")
check("识别出 0.8", abs(float(contract.scale_factor or 0) - 0.8) < 1e-9, str(contract.scale_factor))
missing = [n for n in params if n not in contract.changed]
check("所有源码尺寸参数都被授权", not missing, f"未授权：{missing}")
check("没有必须逐字不变的项", contract.unchanged == [], str(contract.unchanged))

print("\n=== 2. 确定性缩放 ===")
scaled = jobs_mod.build_user_scaled_source(source, 0.8, "通用 FDM 打印机")
check("能构造出缩放版", scaled is not None and scaled != source, f"{len(scaled or '')} 字符")
if scaled:
    after = scad_src.extract_top_level_numbers(scaled)
    check("outer_d 缩到 0.8", abs(after["outer_d"] - params["outer_d"] * 0.8) < 1e-6,
          f"{params['outer_d']} → {after['outer_d']}")
    check("total_h 缩到 0.8", abs(after["total_h"] - params["total_h"] * 0.8) < 1e-6,
          f"{params['total_h']} → {after['total_h']}")
    if "rib_count" in params:
        check("筋数量不变（不是等比缩放的语义）",
              abs(after["rib_count"] - params["rib_count"]) < 1e-6,
              f"{params['rib_count']} → {after['rib_count']}")
    verdict = verify_change(source, scaled, contract, spec)
    print(f"  判据：ok={verdict.ok} performed={verdict.performed} "
          f"unauthorized={verdict.unauthorized[:4]} 代价={verdict.penalty():.2f}")
    check("契约核对判达标", verdict.ok, verdict.describe()[:160])
    check("没有越权改动", not verdict.unauthorized, str(verdict.unauthorized[:6]))

print("\n" + "=" * 66)
print(f"整体缩放：失败 {len(failures)} 项")
for item in failures:
    print(f"  ✗ {item}")
print("=" * 66)
raise SystemExit(1 if failures else 0)
