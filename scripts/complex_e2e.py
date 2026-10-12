"""复杂实例端到端测试：真实 LLM 生成 → 编译 → 实测几何特征。

用途：量化「模型太简单、不贴合描述」这个问题，并在改动后对比是否改善。

对每个用例记录：
  * 生成的 SCAD 行数、独立几何模块数、特征操作次数（复杂度代理指标）
  * 形体设计指标：截面渐变（收分/外扩）、圆角收边、阵列特征、公差参数
  * 实测 STL 的关键特征：填充率、封闭性、**零件数**、**腔体孤岛**
  * 规格里要求的结构要点，在代码里有没有对应实现痕迹

硬性验收（对应用户实际踩到的两个坑）：
  * 零件数必须为 1 —— 「一个成品」不能是「几个各自独立的零件」
  * 容器类零件腔内不能有孤岛 —— 加强筋不能穿进腔体内部
  * 源码声明了的间隙/夹口必须在网格上量得到 —— 减法特征假绿的正面检验

用法：
  $env:DEEPSEEK_API_KEY='...'
  & build\\venv\\Scripts\\python.exe scripts\\complex_e2e.py [--keep] [--only 1,4]
"""

from __future__ import annotations

import console_utf8  # noqa: F401,E402 —— 必须最先导入：GBK 控制台下 print 中文/符号会弄崩脚本（见 src/aiforge/stdio.py）
import asyncio
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

WORK = ROOT / "build" / "work" / "complex"
os.environ["AIFORGE_DATA_DIR"] = str(WORK / "data")
os.environ["AIFORGE_LOCAL_DIR"] = str(ROOT / "build" / "work" / "testlocal")
os.environ["AIFORGE_PROJECTS_DIR"] = str(WORK / "projects")

from aiforge import compiler, llm, mesh, render, reqtests, scad as scad_src  # noqa: E402
# 直接引用应用里的判据函数，不在测试脚本里另写一套（另写一套就会漂）。
from aiforge.jobs import (  # noqa: E402
    _declared_gaps_from_source,
    _downgrade_printability,
    _geometry_integrity_problems,
    _hard_failures,
    _integrity_rank,
    _mesh_quality_not_worse,
)

# 真实复杂需求：每个都要求「多个功能特征协同」，而不是一个简单形状。
# 选例标准照用户给的对标物（D:\模型\Desk_Bin_15mm_Tabletop.3mf）：
# 市面上真实存在、一件打印成型、有明确使用场景的桌面小物 —— 而不是练习作。
CASES: list[dict[str, object]] = [
    {
        "name": "夹桌式桌面垃圾桶",
        "prompt": (
            "设计一个可以夹在 15mm 厚桌板边缘的桌面垃圾桶，一次打印成型不需要支撑。"
            "要求：桶身向上外扩呈扇形，桶底是圆角矩形，外壁有竖向加强筋，"
            "夹持口能卡住桌沿不晃动，整体容量尽量大。"
        ),
        "must_have": ["夹", "外扩 或 扇形 或 锥", "加强筋 或 肋", "圆角"],
        "container": True,
    },
    {
        "name": "线缆收纳底座",
        "prompt": (
            "设计一个桌面线缆收纳底座：底部配重腔可以放硬币，顶部有 3 条走线槽，"
            "侧面有防滑齿纹，整体 80×80×30mm，一次打印成型无需支撑。"
        ),
        "must_have": ["槽", "齿纹 或 纹 或 棱", "腔", "配重"],
        "container": True,
    },
    {
        "name": "壁挂钥匙挂钩",
        "prompt": (
            "设计一个壁挂式钥匙挂钩，带 3 个钩子，背面有两个 M4 螺丝沉头孔可以拧在墙上，"
            "顶部有一个放信件的凹槽，钩子要能承受钥匙重量不折断。"
        ),
        "must_have": ["钩", "沉头 或 螺丝", "凹槽", "3 个 或 三"],
        "container": False,
    },
    {
        "name": "夹桌式水杯架",
        "prompt": (
            "设计一个夹在桌沿的水杯架，夹持厚度 20mm。要求：下方是 C 形夹口，"
            "上方是一个带缺口圆环的杯托（杯径 70mm），夹口与杯托之间用一块竖直立板连成一体，"
            "立板两侧各加一条加强筋，整体一次打印成型、不用支撑。"
        ),
        "must_have": ["夹", "杯 或 环", "加强筋 或 肋", "缺口 或 开口"],
        "container": False,
    },
    {
        "name": "桌面多格笔筒",
        "prompt": (
            "设计一个桌面笔筒：外形是上大下小的圆角方形壳体，内部被隔板分成 4 个格子，"
            "其中一格是向前倾斜 15 度的斜插筒（放剪刀），前面板上有 5 个圆孔插笔，"
            "底部加厚配重，一次打印成型无需支撑。"
        ),
        "must_have": ["隔板 或 格", "斜", "孔", "圆角 或 倒角"],
        "container": True,
    },
    {
        "name": "壁挂机顶盒托架",
        "prompt": (
            "设计一个壁挂式机顶盒托架：底部是一块带 4 个 M4 沉孔的背板，"
            "前方伸出一个 L 形托板承托设备，托板两侧有向上翘起的挡边，"
            "底面做散热镂空阵列，侧面留一个线缆过孔，整体一次打印成型。"
        ),
        "must_have": ["沉孔 或 螺丝", "挡边 或 翘", "镂空 或 阵列 或 开孔", "线缆 或 过孔"],
        "container": False,
    },
]


def complexity(scad: str) -> dict[str, object]:
    """用确定性指标给生成结果的复杂度与设计深度打分。"""
    body = scad_src.strip_comments(scad)
    modules = re.findall(r"^\s*module\s+(\w+)", body, re.M)
    params = scad_src.extract_top_level_numbers(scad)
    solids = [m for m in ("cube", "cylinder", "sphere", "polyhedron") if m in body]
    # 「特征操作」是复杂模型的关键：布尔、变换、循环、挤出
    ops = {
        "difference": len(re.findall(r"\bdifference\s*\(", body)),
        "union": len(re.findall(r"\bunion\s*\(", body)),
        "intersection": len(re.findall(r"\bintersection\s*\(", body)),
        "hull": len(re.findall(r"\bhull\s*\(", body)),
        "minkowski": len(re.findall(r"\bminkowski\s*\(", body)),
        "linear_extrude": len(re.findall(r"\blinear_extrude\s*\(", body)),
        "rotate_extrude": len(re.findall(r"\brotate_extrude\s*\(", body)),
        "for": len(re.findall(r"\bfor\s*\(", body)),
        "translate": len(re.findall(r"\btranslate\s*\(", body)),
        "rotate": len(re.findall(r"\brotate\s*\(", body)),
        "offset": len(re.findall(r"\boffset\s*\(", body)),
        "polygon": len(re.findall(r"\bpolygon\s*\(", body)),
    }
    # 形体设计深度：这几项决定成品是「练习作」还是「有设计的产品」
    design = {
        # 截面渐变：两层轮廓 + hull，或 linear_extrude(scale=)，或 resize
        "taper": len(re.findall(r"\bhull\s*\(", body)) + len(re.findall(r"scale\s*=", body)),
        # 圆角/倒角收边
        "fillet": len(re.findall(r"\boffset\s*\(", body))
        + len(re.findall(r"\bminkowski\s*\(", body))
        + len(re.findall(r"圆角|倒角|fillet|chamfer", scad)),
        # 阵列/重复特征（齿纹、镂空、成组孔）
        "array": len(re.findall(r"\bfor\s*\(", body)),
        # 装配公差：留间隙说明作者真的考虑过装配
        "tolerance": len(re.findall(r"clearance|gap|tolerance|余量|间隙|公差", scad, re.I)),
    }
    return {
        "lines": len([line for line in scad.splitlines() if line.strip()]),
        "modules": modules,
        "params": len(params),
        "solid_kinds": solids,
        "ops": {k: v for k, v in ops.items() if v},
        "op_total": sum(ops.values()),
        "design": design,
        "design_total": sum(design.values()),
    }


def analyze(stl_path, source: str):
    """与生产同口径的网格分析：源码里声明了间隙就顺带实测空隙。"""
    declared = _declared_gaps_from_source(source)
    return (
        compiler.analyze_stl(stl_path, None, declared or None),
        declared,
    )


async def run_case(client: llm.DeepSeekClient, case: dict, index: int) -> dict:
    name = str(case["name"])
    prompt = str(case["prompt"])
    print("=" * 74)
    print(f"[{index}] {name}")
    print("=" * 74)

    spec = await client.parse_design_spec(prompt, None)
    print(f"规格：{spec.kind}；结构要点 {len(spec.features)} 条")
    result = await client.generate_from_spec(prompt, spec, None)
    scad = scad_src.strip_code_fences(result.content)
    stats = complexity(scad)
    print(
        f"生成：{stats['lines']} 行 / {len(stats['modules'])} 个 module / "
        f"{stats['params']} 个参数 / 特征操作 {stats['op_total']} 次 {stats['ops']}"
    )
    print(f"      设计深度：{stats['design']}")

    out_dir = WORK / f"case{index}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "model.scad").write_text(scad, encoding="utf-8")

    compiled = await asyncio.to_thread(compiler.compile_to_stl, scad, out_dir, "model")
    if not compiled.ok:
        print(f"编译失败：{compiled.message[:120]}")
        return {"name": name, "ok": False, "reason": compiled.message[:200], "stats": stats}

    metrics, declared_gaps = await asyncio.to_thread(analyze, compiled.stl_path, scad)
    bbox = metrics.bbox
    first_pass_parts = metrics.solid_parts
    first_pass_islands = sum(n for _, n in metrics.cavity_islands)
    print(
        f"首次实测：{bbox[0]:.1f}×{bbox[1]:.1f}×{bbox[2]:.1f}mm  "
        f"{metrics.triangles} 面  体积 {metrics.volume_mm3/1000:.1f}cm³  "
        f"封闭={metrics.watertight}  零件数={metrics.solid_parts}"
    )
    if declared_gaps:
        gaps = metrics.interior_gaps
        print(
            f"      声明间隙 {[f'{n}={v:g}mm' for _l, v, n in declared_gaps]}  "
            f"实测被夹空档 "
            f"{[round(x, 2) for x in gaps.per_axis] if gaps else '未测到（模型过大/过小）'}"
        )

    # ---- 复现应用里的「实测 → 定点修复 → 采纳更好的一版」闭环 ----
    # 逻辑与 GenerationRunner._repair_geometry_integrity 保持一致：
    # 只有「散件更少 → 点名尺寸更准 → 穿腔更少、且仍然封闭」才采纳，否则保留原版。
    problems = _geometry_integrity_problems(metrics, spec)
    repaired_note = ""
    best_source, best_compiled, best_metrics = scad, compiled, metrics
    best_rank = _integrity_rank(metrics, spec)
    if problems:
        print(f"   结构问题：{problems[0].splitlines()[0][:80]}…")
        for attempt in (1, 2):
            fixed = await client.repair_geometry(
                best_source,
                "\n".join(_geometry_integrity_problems(best_metrics, spec)),
                attempt,
            )
            candidate = scad_src.strip_code_fences(fixed.content)
            retry = await asyncio.to_thread(
                compiler.compile_to_stl, candidate, out_dir, "model_fixed"
            )
            if not retry.ok:
                repaired_note = "修复版编译失败，保留原版"
                break
            retry_metrics, _ = await asyncio.to_thread(analyze, retry.stl_path, candidate)
            # 采纳条件与生产一致：网格质量不比原版差 + 完整性严格更好（以前只看
            # watertight 布尔值，两边都不水密时它恒真，能把 28 条非流形边换成 287 条）
            if _mesh_quality_not_worse(best_metrics, retry_metrics) and (
                _integrity_rank(retry_metrics, spec) < best_rank
            ):
                best_source, best_compiled, best_metrics = candidate, retry, retry_metrics
                best_rank = _integrity_rank(retry_metrics, spec)
            if best_rank == (0, 0, 0, 0, 0, 0, 0):
                break

        if best_source is not scad:
            scad, compiled, metrics = best_source, best_compiled, best_metrics
            repaired_note = (
                f"第 {attempt} 次修复后采纳：零件 {first_pass_parts}→{metrics.solid_parts}、"
                f"穿腔 {first_pass_islands}→{sum(n for _, n in metrics.cavity_islands)}"
            )
            stats = complexity(scad)
            (out_dir / "model.scad").write_text(scad, encoding="utf-8")
            bbox = metrics.bbox
            print(f"   {repaired_note}")
            print(
                f"修复后实测：{bbox[0]:.1f}×{bbox[1]:.1f}×{bbox[2]:.1f}mm  "
                f"{metrics.triangles} 面  封闭={metrics.watertight}  "
                f"零件数={metrics.solid_parts}"
            )
        else:
            repaired_note = "修复未改善，保留原版"
            print(f"   {repaired_note}")

    body = scad_src.strip_comments(scad)
    hits = []
    for want in case["must_have"]:
        alts = [a.strip() for a in str(want).split(" 或 ")]
        hits.append((want, any(a in body or a in scad for a in alts)))
    for want, found in hits:
        print(f"   {'✓' if found else '✗'} 代码里体现了「{want}」")

    # ---- 外观复核：让多模态模型真的看一眼（程序指标之外的第二双眼睛）----
    visual = ""
    try:
        shots = await asyncio.to_thread(render.render_views, compiled.stl_path, out_dir)
        visual = await client.review_snapshot(
            [s.path.read_bytes() for s in shots], prompt, mesh.summarize_for_ai(metrics, "model.stl")
        )
        flag = "有问题" if llm.parse_snapshot_verdict(visual) else "无明显问题"
        print(f"   外观复核（{flag}）：{visual.replace(chr(10), ' ')[:180]}")
    except Exception as exc:  # noqa: BLE001 - 复核失败不该让用例失败
        print(f"   外观复核跳过：{type(exc).__name__}: {exc}")

    # 体积/包围盒比：空心容器应当明显小于实心包络
    envelope = bbox[0] * bbox[1] * bbox[2]
    fill = metrics.volume_mm3 / envelope if envelope else 0
    print(f"填充率：{fill*100:.1f}%（实心块≈100%，空心容器通常 <35%）")

    # ---- 硬性验收：用户实际踩到的两个坑 ----
    problems = []
    if not metrics.watertight:
        problems.append(f"非封闭实体（开放边 {metrics.boundary_edges}）")
    if metrics.solid_parts != 1:
        problems.append(f"零件数 {metrics.solid_parts}（必须是一整件）")
    if case.get("container") and metrics.cavity_islands:
        worst = max(n for _, n in metrics.cavity_islands)
        problems.append(f"腔内孤岛 {worst} 处（特征穿进了腔体）")

    # ---- 声明的间隙/夹口是否真的切出来了（减法特征假绿的正面检验）----
    # 拿过去的交付版本跑这一段会得到：零件 1、封闭、无穿腔，而夹口沿 Y 实测 0mm；
    # 只看上面三条验收是看不出来的。
    suite = reqtests.evaluate(metrics=metrics, requirement=prompt)
    gap_tests = [t for t in suite.tests if t.kind == "feature-gap"]
    hard = _hard_failures(suite)
    # 拿一份「AI 全说没问题」的判定跑降级：这一步验的就是判定诚实化本身
    sim: dict[str, object] = {"verdict": "pass", "score": 85, "issues": []}
    downgraded = _downgrade_printability(sim, suite, visual, [])
    if gap_tests:
        for t in gap_tests:
            print(f"   间隙实测 {t.status}：{t.requirement} → {t.actual[:90]}")
    else:
        print("   间隙实测：源码里没有可核对的间隙/夹口参数（本判据不适用）")
    print(
        f"   判定诚实化：假设 AI 给 pass/85 → 按实测证据 {sim['verdict']}/{sim['score']}"
        + (f"（{downgraded}）" if downgraded else "（无需降级）")
    )
    if hard:
        problems.append(
            f"实测硬失败 {len(hard)} 项：" + "；".join(str(t.requirement) for t in hard[:3])
        )
    print(
        "   验收：" + ("✓ 通过（一整件、封闭、无穿腔、声明间隙量得到）" if not problems else "✗ " + "；".join(problems))
    )

    return {
        "name": name,
        "ok": True,
        "stats": stats,
        "bbox": bbox,
        "triangles": metrics.triangles,
        "volume_cm3": metrics.volume_mm3 / 1000,
        "fill_ratio": fill,
        "watertight": metrics.watertight,
        "solid_parts": metrics.solid_parts,
        "cavity_islands": metrics.cavity_islands,
        "first_pass_parts": first_pass_parts,
        "first_pass_islands": first_pass_islands,
        "repaired": repaired_note,
        "visual_review": visual,
        "problems": problems,
        "gap_tests": [(t.status, str(t.requirement)) for t in gap_tests],
        "hard_failures": [str(t.requirement) for t in hard],
        "honest_verdict": f"{sim['verdict']}/{sim['score']}",
        "spec_features": spec.features,
        "must_have_hits": hits,
    }


async def main() -> int:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        print("缺少 DEEPSEEK_API_KEY")
        return 2
    # 迭代时只跑指定用例：--only 1,3
    only: set[int] = set()
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--only" and i + 1 < len(argv):
            only = {int(x) for x in argv[i + 1].split(",") if x.strip().isdigit()}
    client = llm.DeepSeekClient(key)
    results = []
    for i, case in enumerate(CASES, 1):
        if only and i not in only:
            continue
        try:
            results.append(await run_case(client, case, i))
        except Exception as exc:  # noqa: BLE001
            print(f"用例失败：{type(exc).__name__}: {exc}")
            results.append({"name": case["name"], "ok": False, "reason": str(exc)})

    print()
    print("=" * 74)
    print("汇总")
    print("=" * 74)
    failed = 0
    first_pass_bad = 0
    for r in results:
        if not r.get("ok"):
            print(f"  ✗ {r['name']}：{r.get('reason')}")
            failed += 1
            first_pass_bad += 1
            continue
        stats = r["stats"]
        missed = [w for w, f in r["must_have_hits"] if not f]
        print(
            f"  {r['name']}：{stats['lines']} 行 / 特征操作 {stats['op_total']} / "
            f"设计深度 {stats['design_total']} / 填充率 {r['fill_ratio']*100:.0f}% / "
            f"零件 {r['solid_parts']} / 缺特征 {missed or '无'}"
        )
        if r["problems"]:
            print(f"      验收不通过：{'；'.join(r['problems'])}")
            failed += 1
        if r.get("hard_failures"):
            print(
                f"      间隙/特征实测：{r['gap_tests']}；判定诚实化后为 "
                f"{r['honest_verdict']}（原本 AI 会给 pass/85）"
            )
        if r["first_pass_parts"] != 1 or r["first_pass_islands"]:
            first_pass_bad += 1
            print(
                f"      首次生成有问题（零件 {r['first_pass_parts']}、"
                f"穿腔 {r['first_pass_islands']}）→ {r['repaired'] or '未修复'}"
            )
    print()
    print(
        f"首次生成即合格：{len(results) - first_pass_bad} / {len(results)}；"
        f"经自动修复后交付合格：{len(results) - failed} / {len(results)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
