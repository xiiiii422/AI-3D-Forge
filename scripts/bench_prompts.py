"""可复现的生成质量基准（对标 CADTestBench / CAD Arena 的做法）。

为什么必须有这一支（对标结论见 docs/COMPETITIVE-GAPS.md 的 G3 与路线 S1.4）：
  * 我们改提示词、换模型、调判据之后，**没有任何东西能回答"质量是涨了还是跌了"**。
    selfcheck 测的是机制有没有接对，端到端验收测的是"跑通没有"，两者都不给分数。
  * CADTestBench（arXiv 2605.07807）与 CAD Arena 的做法是：固定一组分难度的提示词，
    跑完报三个指标 —— PR（需求全达率的样本占比）、RS（需求达成条数占比）、
    IR（没能判定的占比），每次改动都回归同一套题。
  * 顺带能量竞品公认最弱的一项：**一致性**。评测原文说"同一提示词两次出两个模型"；
    我们走参数化路线本就该稳定，那就用数字证明它稳定（AIFORGE_BENCH_REPEAT=2）。

判据全部来自对导出 STL 的实测（requirement_tests 由 aiforge/reqtests.py 产出），
不看模型自己的陈述。

用法：
    build\\venv\\Scripts\\python.exe scripts\\bench_prompts.py
    $env:AIFORGE_BENCH_TIERS="1";        只跑某个难度档（1..4）
    $env:AIFORGE_BENCH_LIMIT="3";        只跑前 N 题（额度有限时先跑子集）
    $env:AIFORGE_BENCH_REPEAT="2";       每题跑两遍，额外报一致性
输出：
    build\\work\\bench\\bench-report.json / bench-report.md
退出码：
    0 跑完（题目失败是数据，不是脚本错误）；2 环境不满足（没有 Key / 没有 OpenSCAD）。
"""

from __future__ import annotations

import console_utf8  # noqa: F401,E402 —— 必须最先导入：GBK 控制台下 print 中文/符号会弄崩脚本（见 src/aiforge/stdio.py）
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "build" / "work" / "bench"
# 只重定向用户数据（配置/项目），不重定向 local：
# local 里放着本机已装好的 OpenSCAD，重定向过去测的就成了夹具洁癖而不是产品能力。
# 必须**强制赋值**：第一轮实测从上一条脚本（打包冒烟）继承到了 AIFORGE_PROJECTS_DIR，
# 题目项目全写进了别人的目录 —— 报告数字看着正常，数据其实是脏的。
os.environ["AIFORGE_DATA_DIR"] = str(WORK / "data")
os.environ["AIFORGE_PROJECTS_DIR"] = str(WORK / "projects")
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

# 20 题、4 档难度（题量与分档对齐 CAD Arena / CADTestBench 的公开做法）。
# 每题都带**可实测的点名要求**，否则需求核对表无从判定。
PROMPTS: list[dict[str, Any]] = [
    # ---- T1 单实体基础件：先看有没有"能测的都对" ----
    {"tier": 1, "name": "方块", "prompt": "设计一个长方体块：长 60mm、宽 40mm、高 20mm，实心。"},
    {"tier": 1, "name": "圆片", "prompt": "设计一个圆形垫片：外径 50mm，厚度 5mm。"},
    {"tier": 1, "name": "六棱柱", "prompt": "设计一个正六棱柱把手：对边宽 30mm，高度 45mm。"},
    {"tier": 1, "name": "圆锥座", "prompt": "设计一个圆锥台摆件：底面直径 60mm，顶面直径 20mm，高 40mm。"},
    {"tier": 1, "name": "楔形块", "prompt": "设计一个楔形门挡：长 90mm、宽 40mm、高 30mm，斜面从最高点降到 5mm。"},
    # ---- T2 带一个功能特征：孔/槽/齿 ----
    {"tier": 2, "name": "带孔板", "prompt": "设计一块安装板：100mm×60mm×6mm，正中开一个直径 12mm 的通孔。"},
    {"tier": 2, "name": "笔筒", "prompt": "设计一个圆形笔筒：外径 80mm、总高 100mm、壁厚 2.5mm，底部封闭、顶部敞开。"},
    {"tier": 2, "name": "手机支架", "prompt": "设计一个桌面手机支架：高度 90mm，背面留一个充电线穿孔。"},
    {"tier": 2, "name": "挂钩", "prompt": "设计一个墙壁挂钩：底板 40mm×40mm×8mm，钩体伸出 30mm，能挂 5mm 粗的绳子。"},
    {"tier": 2, "name": "线缆夹", "prompt": "设计一个理线夹：整体长 40mm、宽 20mm、高 15mm，中间留一个直径 6mm 的线孔。"},
    # ---- T3 多特征 + 配合尺寸：尺寸核对与不越权改动 ----
    {"tier": 3, "name": "花盆", "prompt": "设计一个多肉花盆：口径 90mm、高 70mm、壁厚 3mm，底部留一个直径 8mm 的排水孔。"},
    {"tier": 3, "name": "合页块", "prompt": "设计一个简易合页座：两个 50mm×30mm×6mm 的耳片，中间用直径 8mm 的轴孔连通。"},
    {"tier": 3, "name": "齿轮", "prompt": "设计一个 20 齿直齿轮：分度圆直径 60mm，齿厚 3mm，板厚 8mm，中心轴孔 8mm。"},
    {"tier": 3, "name": "收纳盒", "prompt": "设计一个无盖收纳盒：外尺寸 120mm×80mm×40mm，壁厚 2.4mm，底面平整。"},
    {"tier": 3, "name": "镜头盖挂座", "prompt": "设计一个镜头盖挂座：圆环内径 58mm、外径 66mm、厚 10mm，带一个 20mm 高的挂耳。"},
    # ---- T4 专业件：螺纹/薄壁/免支撑，这些正是竞品最容易翻车的地方 ----
    {"tier": 4, "name": "转接环", "prompt": "设计一个 67mm 转 77mm 的滤镜转接环：带真螺纹，中间必须镂空。"},
    {"tier": 4, "name": "螺母", "prompt": "设计一个 M8 六角螺母：对边宽 14mm、厚 6.5mm，内螺纹要能拧到 M8 螺栓上。"},
    {"tier": 4, "name": "管夹", "prompt": "设计一个卡住直径 20mm 水管的管夹：内径 20mm，壁厚 4mm，带两颗 M3 螺钉的耳座。"},
    {"tier": 4, "name": "散热片", "prompt": "设计一个 40mm×40mm 的散热片：鳍片厚 2mm、高 20mm、间距 4mm，底板厚 3mm。"},
    {"tier": 4, "name": "耳机挂架", "prompt": "设计一个显示器耳机挂架：挂钩卡进显示器上沿（厚 8mm），托住直径 25mm 的耳罩臂。"},
]


def run_job(client: httpx.Client, prompt: str, timeout_s: int = 480) -> dict[str, Any]:
    """跑一次"一句话生成"，自动确认规格卡片，返回终态作业。"""
    created = client.post("/api/jobs", json={"prompt": prompt, "mode": "create"})
    if created.status_code != 202:
        return {"status": "rejected", "error": created.text[:300]}
    job_id = created.json()["job_id"]
    deadline = time.time() + timeout_s
    confirmed = False
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = client.get(f"/api/jobs/{job_id}").json()
        status = last.get("status")
        if status == "awaiting_confirmation" and not confirmed:
            client.post(f"/api/jobs/{job_id}/confirm-spec", json={})
            confirmed = True
        if status in ("done", "error", "cancelled"):
            break
        time.sleep(2)
    else:
        last["status"] = last.get("status") or "timeout"
    return last


def score_of(job: dict[str, Any]) -> dict[str, Any]:
    """从一次作业里抽出基准要记录的数字。"""
    result = job.get("result") or {}
    suite = result.get("requirement_tests") or {}
    stl = result.get("stl") or {}
    metrics = stl.get("metrics") or {}
    bbox = stl.get("bbox_mm") or metrics.get("bbox") or {}
    return {
        "status": job.get("status"),
        "pass_rate": suite.get("pass_rate"),
        "requirement_score": suite.get("requirement_score"),
        "invalid_ratio": suite.get("invalid_ratio"),
        "counts": suite.get("counts") or {},
        "groups": suite.get("groups"),
        "failed": [
            {
                "requirement": t.get("requirement"),
                "expected": t.get("expected"),
                "actual": t.get("actual"),
                "deviation_pct": t.get("deviation_pct"),
            }
            for t in (suite.get("tests") or [])
            if t.get("status") in ("fail", "invalid")
        ][:8],
        "triangles": stl.get("triangles"),
        "bbox": [bbox.get("x"), bbox.get("y"), bbox.get("z")],
        "watertight": stl.get("watertight"),
        "solid_parts": (metrics.get("solid_parts")),
        "project_id": result.get("project_id"),
        "error": (job.get("error") or {}) if isinstance(job.get("error"), dict) else job.get("error"),
    }


def mean(values: list[float | None]) -> float | None:
    got = [float(v) for v in values if v is not None]
    return round(sum(got) / len(got), 4) if got else None


def render_markdown(rows: list[dict[str, Any]], started: float) -> str:
    ok_rows = [r for r in rows if r["runs"]]
    scored = [r["runs"][0] for r in ok_rows]  # 一致性另算，指标取第一遍
    lines = [
        "# 生成质量基准报告",
        "",
        f"- 题数：{len(rows)}（4 个难度档），实际完成 {len(ok_rows)}",
        f"- 用时：{(time.time() - started) / 60:.1f} 分钟",
        f"- PR（整份需求全达成的题占比）：**"
        f"{mean([r['pass_rate'] for r in scored]) if ok_rows else '—'}**",
        f"- RS（需求达成条数占比均值）：**{mean([r['requirement_score'] for r in scored]) if ok_rows else '—'}**",
        f"- IR（没能判定的题占比）：**{mean([r['invalid_ratio'] for r in scored]) if ok_rows else '—'}**",
        f"- 封闭性达成率：**{mean([1.0 if r['watertight'] else 0.0 for r in scored]) if ok_rows else '—'}**",
        f"- 单一实体率：**{mean([1.0 if (r['solid_parts'] or 0) <= 1 else 0.0 for r in scored]) if ok_rows else '—'}**",
        "",
        "口径对齐 CADTests（arXiv 2605.07807）：invalid 计为失败，不当成通过。",
        "",
        "| 档 | 题目 | 状态 | RS | 未达成/没测出来 | 实测尺寸(mm) | 面数 | 水密 | 件数 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in ok_rows:
        r = row["runs"][0]
        bad = "；".join(
            f"{f['requirement']}（要 {f['expected']}/测 {f['actual']}）" for f in r["failed"][:3]
        ) or "—"
        bbox = "×".join(str(v) for v in r["bbox"] if v is not None) or "—"
        lines.append(
            f"| T{row['tier']} | {row['name']} | {r['status']} | {r['requirement_score']} | {bad} "
            f"| {bbox} | {r['triangles']} | {'是' if r['watertight'] else '否'} | {r['solid_parts']} |"
        )
    consistency = [r for r in rows if len(r["runs"]) > 1]
    if consistency:
        same = sum(
            1
            for r in consistency
            if all(run["bbox"] == r["runs"][0]["bbox"] for run in r["runs"])
        )
        lines += [
            "",
            "## 一致性（同一提示词重复生成）",
            "",
            f"- 重复题数 {len(consistency)}，实测尺寸完全一致 {same} 题 → **尺寸一致率 "
            f"{(same / len(consistency) * 100):.0f}%**",
            "",
            "> 竞品公开评测里最常见的一条抱怨是「同一提示词两次出两个模型」；",
            "> 这条指标就是给自己留的尺子：哪天它掉下 100%，说明直出/确定性链路被动摇了。",
        ]
    lines += ["", "明细数据见同目录 `bench-report.json`。", ""]
    return "\n".join(lines)


def main() -> int:
    tiers = {
        int(x)
        for x in str(os.environ.get("AIFORGE_BENCH_TIERS", "") or "").replace(" ", ",").split(",")
        if x.strip().isdigit()
    }
    limit = int(os.environ.get("AIFORGE_BENCH_LIMIT", "0") or 0)
    repeats = max(1, min(3, int(os.environ.get("AIFORGE_BENCH_REPEAT", "1") or 1)))
    prompts = [p for p in PROMPTS if not tiers or p["tier"] in tiers]
    if limit:
        prompts = prompts[:limit]

    from aiforge import server as srv
    from aiforge.config import store

    cfg = store()
    if not cfg.api_key:
        print("没有配置 API Key：基准需要真实调用生成链路，不能用假数据代替。")
        return 2

    WORK.mkdir(parents=True, exist_ok=True)
    thread, port = srv.start_server(preferred_port=8911)
    base = f"http://127.0.0.1:{port}"
    client = httpx.Client(base_url=base, timeout=180.0, trust_env=False)
    started = time.time()
    rows: list[dict[str, Any]] = []
    print(f"基准服务：{base}；{len(prompts)} 题 × {repeats} 遍", flush=True)
    try:
        for index, item in enumerate(prompts, 1):
            runs = []
            for rep in range(repeats):
                job = run_job(client, item["prompt"])
                runs.append(score_of(job))
                if job.get("status") != "done":
                    break
            rows.append({"index": index, "tier": item["tier"], "name": item["name"],
                         "prompt": item["prompt"], "runs": runs})
            first = runs[0]
            print(
                f"  [{index:>2}/{len(prompts)}] T{item['tier']} {item['name']:<8} "
                f"{first['status']:<8} RS={first['requirement_score']} "
                f"未达成={len(first['failed'])} 尺寸={first['bbox']} 面数={first['triangles']}",
                flush=True,
            )
    finally:
        client.close()

    report = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
        "elapsed_min": round((time.time() - started) / 60, 1),
        "model": {
            "base_url": cfg.config.api.base_url,
            "model": cfg.config.api.model,
            "economy_base_url": getattr(cfg.config.api, "economy_base_url", ""),
            "economy_model": getattr(cfg.config.api, "economy_model", ""),
        },
        "settings": {"tiers": sorted(tiers) or "all", "limit": limit, "repeats": repeats},
        "rows": rows,
        "aggregate": {
            "pr": mean([r["runs"][0]["pass_rate"] for r in rows if r["runs"]]),
            "rs": mean([r["runs"][0]["requirement_score"] for r in rows if r["runs"]]),
            "ir": mean([r["runs"][0]["invalid_ratio"] for r in rows if r["runs"]]),
            "watertight": mean(
                [1.0 if r["runs"][0]["watertight"] else 0.0 for r in rows if r["runs"]]
            ),
            "single_body": mean(
                [
                    1.0 if (r["runs"][0]["solid_parts"] or 0) <= 1 else 0.0
                    for r in rows
                    if r["runs"]
                ]
            ),
        },
    }
    (WORK / "bench-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (WORK / "bench-report.md").write_text(render_markdown(rows, started), encoding="utf-8")
    print("\n汇总：", report["aggregate"])
    print(f"报告：{WORK / 'bench-report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
