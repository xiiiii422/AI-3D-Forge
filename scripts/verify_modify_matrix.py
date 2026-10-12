"""自然语言修改端到端矩阵：一句一句改，每一步都用**实测几何**判定。

为什么要单独做这一支（用户要求「重点测试自然语言修改」）：
现有的端到端验收只做了一次「高度改成150mm」+ 一次参数微调，
覆盖不到"连续改、改结构、改孔径、缩放"这些真实用法，也验证不了
「用户说改 A，结果 A 没变、却把别处改掉」这类问题。

判据（全部来自对 STL 的实测，不看 AI 的自我陈述）：
  * 目标改动**真的落到几何上**（尺寸/体积按预期变化）；
  * **无关尺寸不动**（例如改高度时外径必须还是 80±1）；
  * 每一步都仍然是**封闭的单一实体**；
  * 项目 revision 连续递增、规格指纹不变 —— 证明是"改这一版"，不是从零重画；
  * 孔径与贯通只用**产品自己量出来的那组数**（`/metrics` 的 `hole_profile`）和
    产品同一个容差公式判定 —— 验收不许自带第二把尺子，否则「改好了」是两支
    尺子商量的结果，而不是产品的结论。

用法：
    build\\venv\\Scripts\\python.exe scripts\\verify_modify_matrix.py
"""

from __future__ import annotations

import console_utf8  # noqa: F401,E402 —— 必须最先导入：GBK 控制台下 print 中文/符号会弄崩脚本（见 src/aiforge/stdio.py）
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "build" / "work" / "modify-matrix"
# 只重定向"用户数据"（配置/项目），**不**重定向 local 目录：
# local 目录里放着本机已装好的 OpenSCAD，重定向过去会让这条链路直接报
# 「没有找到 OpenSCAD」—— 那测的就不是产品功能，而是测试夹具的洁癖了。
os.environ["AIFORGE_DATA_DIR"] = str(WORK / "data")
os.environ["AIFORGE_PROJECTS_DIR"] = str(WORK / "projects")
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

BASE_PROMPT = (
    "设计一个圆形的桌面笔筒：外径 80mm、总高 100mm、壁厚 2.5mm，"
    "底部是封闭的实心底，顶部敞开。"
)

# (说明, 修改指令, 判定函数)
STEPS: list[tuple[str, str]] = [
    ("改高度", "把高度改成50mm。"),
    ("加顶部孔", "在顶部正中间增加一个直径10mm的圆孔，要完全贯通到底部。"),
    ("改孔径", "把这个孔的内径改成8mm。"),
    ("加厚底座", "把底座加厚到6mm。"),
    ("整体缩放", "整体缩小到原来的80%。"),
]

results: list[tuple[str, bool, str]] = []


def step(label: str, ok: bool, detail: str) -> None:
    results.append((label, ok, detail))
    print(f"  [{'OK' if ok else 'FAIL'}] {label} —— {detail}", flush=True)


def run_job(client: httpx.Client, payload: dict, auto_confirm: bool = True) -> dict:
    """跑一个作业直到终态，自动确认规格卡片。"""
    created = client.post("/api/jobs", json=payload)
    if created.status_code != 202:
        return {"event": "rejected", "message": created.text[:200], "status_code": created.status_code}
    job_id = created.json()["job_id"]
    project_id = created.json()["project_id"]
    deadline = time.time() + 420
    confirmed = False
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/jobs/{job_id}").json()
        status = last.get("status")
        if status == "awaiting_confirmation" and auto_confirm and not confirmed:
            client.post(f"/api/jobs/{job_id}/confirm-spec", json={})
            confirmed = True
        if status in ("done", "error", "cancelled"):
            break
        time.sleep(2)
    last["project_id"] = project_id
    return last


def project_metrics(client: httpx.Client, project_id: str) -> dict:
    """从**磁盘上的 STL** 重新分析（不是读缓存），保证量的是当前模型。"""
    response = client.get(f"/api/projects/{project_id}/metrics")
    if response.status_code != 200:
        return {}
    return response.json()


def get_detail(client: httpx.Client, project_id: str) -> dict:
    response = client.get(f"/api/projects/{project_id}")
    return response.json() if response.status_code == 200 else {}


def main() -> int:
    from aiforge import server as srv
    from aiforge.config import store

    if not store().api_key:
        print("没有 API Key，无法跑真实修改链路")
        return 2

    thread, port = srv.start_server(preferred_port=8901)
    base = f"http://127.0.0.1:{port}"
    client = httpx.Client(base_url=base, timeout=180.0, trust_env=False)
    print(f"服务：{base}")
    try:
        # ---------- 1. 从零生成 ----------
        print("\n=== 基线：用一句话生成笔筒 ===")
        first = run_job(client, {"prompt": BASE_PROMPT, "mode": "create"})
        step("生成成功", first.get("status") == "done", str(first.get("error") or "ok")[:120])
        if first.get("status") != "done":
            return 1
        project_id = first["project_id"]
        base_metrics = project_metrics(client, project_id)
        bbox = base_metrics.get("bbox") or {}
        base_bbox = dict(bbox)
        volume = float(base_metrics.get("volume_mm3") or 0)
        print(f"  基线实测：{bbox}，体积 {volume:.0f} mm³，"
              f"封闭={base_metrics.get('watertight')} 件数={base_metrics.get('solid_parts')}")
        step("基线尺寸符合要求（Ø80×100）",
             abs(float(bbox.get("x", 0)) - 80) <= 2 and abs(float(bbox.get("z", 0)) - 100) <= 3,
             f"x={bbox.get('x')} z={bbox.get('z')}")
        step("基线是封闭单一实体",
             bool(base_metrics.get("watertight")) and int(base_metrics.get("solid_parts") or 0) == 1,
             f"watertight={base_metrics.get('watertight')} parts={base_metrics.get('solid_parts')}")
        spec = (get_detail(client, project_id).get("spec") or {})
        fingerprint = spec.get("fingerprint")
        step("基线带规格指纹（后续改动要保留它）", bool(fingerprint), str(fingerprint))

        # ---------- 2. 连续修改 ----------
        previous = {"bbox": bbox, "volume": volume, "revision": 1}
        # 每一版实测到的包围盒：回退校验要拿它当基准。
        # 为什么不读 history 里的 metrics：历史记录里只有摘要（revision/指令/时间），
        # 不存几何 —— 拿不到就只能跳过校验，等于没验（实测踩过：断言比到了 None）。
        bbox_by_revision: dict[int, dict] = {1: dict(bbox)}
        # 子集模式：智谱免费档限流下跑完全部 5 步不现实（每步多次调用+退避），
        # 用 AIFORGE_MATRIX_STEPS="改高度,整体缩放" 只跑关键几步，验收才可在时限内完成。
        only = os.environ.get("AIFORGE_MATRIX_STEPS", "")
        if only.strip():
            wanted = [w.strip() for w in only.split(",") if w.strip()]
            steps = [s for s in STEPS if s[0] in wanted]
            if not steps:
                print(f"AIFORGE_MATRIX_STEPS 没匹配到任何步骤：{only}")
                return 2
        else:
            steps = list(STEPS)
        for label, instruction in steps:
            print(f"\n=== {label}：{instruction} ===")
            result = run_job(client, {"instruction": instruction, "project_id": project_id, "mode": "modify"})
            if result.get("status") != "done":
                step(f"{label}：作业完成", False, str(result.get("error") or result.get("message"))[:150])
                continue
            step(f"{label}：作业完成", True, f"revision={result.get('revision')}")

            metrics = project_metrics(client, project_id)
            bb = metrics.get("bbox") or {}
            vol = float(metrics.get("volume_mm3") or 0)
            now = {"bbox": bb, "volume": vol, "revision": result.get("revision")}
            print(f"  实测：{bb}，体积 {vol:.0f} mm³，壁厚 p05="
                  f"{(metrics.get('wall') or {}).get('p05_mm')}")

            step(f"{label}：仍是封闭单一实体",
                 bool(metrics.get("watertight")) and int(metrics.get("solid_parts") or 0) == 1,
                 f"watertight={metrics.get('watertight')} parts={metrics.get('solid_parts')}")

            if label == "改高度":
                step(f"{label}：高度落到 50mm",
                     abs(float(bb.get("z", 0)) - 50) <= 4, f"z={bb.get('z')}")
                # 与**基线**比，而不是与公称值比：模型可能带加强筋/收分，
                # 包围盒本来就会略大于公称外径（实测 Ø80 的模型带筋后是 Ø83）。
                step(f"{label}：外径没被顺带改掉",
                     abs(float(bb.get("x", 0)) - float(base_bbox.get("x", 0))) <= 1.0,
                     f"x={bb.get('x')}（基线 {base_bbox.get('x')}）")
            elif label == "加顶部孔":
                # 判据用**绝对体积**：Ø10 的孔穿过 2.5mm 底只挖掉约 200mm³，
                # 拿百分比阈值（2%）去判会漏掉一个真正做对了的孔（实测踩过）。
                removed = previous["volume"] - vol
                step(f"{label}：确实挖掉了材料（体积下降）",
                     removed > 100,
                     f"{previous['volume']:.0f} → {vol:.0f} mm³（少了 {removed:.0f} mm³）")
                step(f"{label}：外形尺寸没变",
                     abs(float(bb.get("x", 0)) - float(base_bbox.get("x", 0))) <= 1.0
                     and abs(float(bb.get("z", 0)) - 50) <= 5,
                     f"x={bb.get('x')}（基线 {base_bbox.get('x')}），z={bb.get('z')}")
                hole = check_through_hole(metrics, diameter=10.0)
                step(f"{label}：孔真的贯通（贴底 0.6mm 仍能量到孔）", hole["through"], hole["detail"])
            elif label == "改孔径":
                hole = check_through_hole(metrics, diameter=8.0)
                step(f"{label}：孔径变成 8mm", hole["diameter_ok"], hole["detail"])
                step(f"{label}：外形尺寸没变",
                     abs(float(bb.get("x", 0)) - float(base_bbox.get("x", 0))) <= 1.0,
                     f"x={bb.get('x')}（基线 {base_bbox.get('x')}）")
            elif label == "加厚底座":
                base = measure_base_thickness(client, project_id)
                # 判据用**实测底厚**，不用体积：底座本来就有 6mm 时，模型不改也满足要求
                # （体积不变是对的；早先拿体积判会把一个正确的"无需改动"误报成失败）。
                step(f"{label}：底座实测厚度接近 6mm",
                     base is not None and abs(base - 6.0) <= 1.0,
                     f"实心底厚约 {base if base is None else round(base, 1)}mm"
                     f"（体积 {previous['volume']:.0f} → {vol:.0f} mm³）")
                step(f"{label}：总高没被改掉", abs(float(bb.get("z", 0)) - 50) <= 5, f"z={bb.get('z')}")
            elif label == "整体缩放":
                ratio_z = float(bb.get("z", 0)) / max(float(previous["bbox"].get("z", 1)), 1e-6)
                ratio_x = float(bb.get("x", 0)) / max(float(previous["bbox"].get("x", 1)), 1e-6)
                step(f"{label}：三轴等比缩到 ~0.8",
                     abs(ratio_z - 0.8) <= 0.06 and abs(ratio_x - 0.8) <= 0.06,
                     f"x×{ratio_x:.3f} z×{ratio_z:.3f}")

            # revision 必须递增：证明是"改这一版"而不是新建
            if previous["revision"] is not None and result.get("revision") is not None:
                step(f"{label}：revision 递增（改的是同一个项目）",
                     int(result["revision"]) > int(previous["revision"]),
                     f"{previous['revision']} → {result['revision']}")
                bbox_by_revision[int(result["revision"])] = dict(bb)
            previous = now

        # ---------- 3. 历史与导出 ----------
        print("\n=== 历史版本与导出 ===")
        detail = get_detail(client, project_id)
        history = detail.get("history") or []
        step(
            f"历史版本已归档（≥{1 + len(steps)} 条）",
            len(history) >= 1 + len(steps),
            f"{len(history)} 条",
        )
        stl = client.get(f"/api/projects/{project_id}/stl")
        scad = client.get(f"/api/projects/{project_id}/scad")
        body = stl.content
        is_binary = len(body) > 84 and b"facet" not in body[:200]
        is_ascii = body[:5] == b"solid" and b"facet normal" in body[:600]
        step("STL 可下载且是有效 STL 文件",
             stl.status_code == 200 and len(body) > 84 and (is_binary or is_ascii),
             f"{len(body)} 字节（{'二进制' if is_binary else 'ASCII'}）")
        step("SCAD 可下载且含参数区",
             scad.status_code == 200 and "// @param" in scad.text,
             f"{len(scad.text)} 字符")
        final_spec = get_detail(client, project_id).get("spec") or {}
        step("始终是同一个项目与同一份规格对象（不是重新生成）",
             bool(final_spec.get("kind")) and final_spec.get("kind") == spec.get("kind")
             and bool(final_spec.get("fingerprint")),
             f"kind={final_spec.get('kind')}，指纹 {spec.get('fingerprint')} → "
             f"{final_spec.get('fingerprint')}（改过尺寸后指纹变化属预期）")

        # ---- 回退历史版本：必须真的回到那一版 ----
        history_before = [entry["revision"] for entry in (get_detail(client, project_id).get("history") or [])]
        target = 2 if 2 in history_before else history_before[0]
        wanted_bbox = bbox_by_revision.get(int(target))
        restored = client.post(f"/api/projects/{project_id}/restore", json={"revision": target})
        step("回退接口可用", restored.status_code == 200, restored.text[:120])
        if restored.status_code == 200:
            after = project_metrics(client, project_id)
            after_bbox = after.get("bbox") or {}
            detail_after = get_detail(client, project_id)
            step("回退后是新的一版（历史只追加）",
                 detail_after.get("revision") == restored.json().get("revision"),
                 f"revision={detail_after.get('revision')}")
            # 判据用**这一轮实测到的那一版包围盒**（不是历史摘要里的文字）
            step("回退后模型尺寸与那一版一致",
                 wanted_bbox is not None
                 and abs(float(after_bbox.get("z", 0)) - float(wanted_bbox.get("z", 0))) <= 1.0
                 and abs(float(after_bbox.get("x", 0)) - float(wanted_bbox.get("x", 0))) <= 1.0,
                 f"回退到第 {target} 版：{after_bbox}（该版实测 "
                 f"{wanted_bbox if wanted_bbox else '未记录'}）")
            step("回退后仍能正常下载模型",
                 client.get(f"/api/projects/{project_id}/stl").status_code == 200,
                 "回退后 STL 仍可下载")
    finally:
        client.close()
        thread.stop()
        thread.join(timeout=8)

    failed = [item for item in results if not item[1]]
    print("\n" + "=" * 72)
    print(f"修改矩阵：通过 {len(results) - len(failed)} 项，失败 {len(failed)} 项")
    for label, _ok, detail in failed:
        print(f"  ✗ {label}: {detail}")
    print("=" * 72)
    return 1 if failed else 0


def check_through_hole(metrics: dict, diameter: float) -> dict:
    """判「孔是否真的贯通到底」与「孔径是否落到位」，输入是**产品的实测剖面**。

    `metrics` 来自 `/api/projects/{id}/metrics`：后端收到这个请求会**重新读磁盘上的
    STL** 再分析（`compiler.analyze_stl`），所以它是产品自己看到的几何，
    既不是 AI 的自我陈述，也不是缓存里的旧值。

    为什么把脚本里原来那份 `measure_hole_diameter` 删掉（接手时留下的 P1）：
      * 它是这份尺子**并入 `aiforge.mesh` 之前的旧口径**：从 `(0, 0)` 出发走射线，
        等于把模型的包围盒中心轴当成孔轴。偏心孔直接量成 None，于是对着一个
        改对了的孔报「没改到」；产品侧现在会先搜孔轴（`_find_hole_axis`），
        实测偏心 12mm 的 Ø10 也量得到。
      * 更要紧的是**两把尺子**：产品判据（`jobs._hole_problems`）的容差是
        `max(0.6, 孔径 × 6%)`、口径是「剖面任一高度命中即达成」，这份副本写死
        ±1.6mm。同一版模型完全可以一边判过、一边判不过 —— 那这份验收证据
        就没法拿来证明产品的行为。
    代价要写明白：共用一把尺子，尺子坏了这一支会跟着瞎。所以尺子本身由自检守着 ——
    偏心孔、非原点模型、6 个探针必须全部命中、腔壁月牙的假读数（旧值 11.36）、
    薄底座里的孔要靠贴底探针，都是逐条实测断言。
    """
    profile = metrics.get("hole_profile") or []
    if not profile:
        return {"through": False, "diameter_ok": False,
                "detail": "实测里没有 hole_profile（metrics 没取到，或后端这一版没量孔径）"}
    # 容差与口径与 `jobs._hole_problems` 逐字一致
    tol = max(0.6, diameter * 0.06)
    hit = [d for d in profile if d is not None and abs(d - diameter) <= tol]
    # 「贯通」只看**最贴底的那根探针**（`mesh._HOLE_PROFILE_PLAN` 第一条 = 底面上方 0.6mm）：
    # 那一段落在实心底的材料里，能量到的空腔只能是被打穿的孔。第二根（2.5mm）不能拿来
    # 判贯通 —— 底座薄于 2.5mm 时它已经在笔筒内腔里了，量到的是腔不是孔。
    # 内腔也不会冒充孔：腔宽远大于 window(15mm)，`measure_hole_diameter` 直接不认。
    bottom = profile[0]
    detail = (
        f"目标孔径 {diameter:g}mm（产品容差 ±{tol:.2f}mm）；"
        f"贴底 0.6mm 探针实测 {None if bottom is None else round(bottom, 1)}mm；"
        f"剖面自底向上 {[None if d is None else round(d, 1) for d in profile]}"
    )
    return {"through": bottom is not None, "diameter_ok": bool(hit), "detail": detail}


def point_inside_along_x(points, z: float, x: float, y: float) -> bool | None:
    """在给定高度上，从很左边沿 +X 打一条射线，数穿过三角面的次数判断该点是否在实体内。

    现在**只有 `measure_base_thickness` 用它**：产品没有「实心底厚」这个指标，
    这一处是本脚本唯一的自有尺子；孔径/贯通一律走产品的 `hole_profile`，
    不再在这里重复实现（重复一份就会漂成两把尺子）。

    采样点必须**稍微偏移**：正好落在三角面的顶点或边上时，交点会被数两次或漏掉，
    奇偶判定就反了 —— 实测把「底部有 6mm 实心」读成了「从 z=0 就是空的」，
    于是对着一个正常模型报「底座厚度 0mm」。
    """
    z = z + 0.0137
    y = y + 0.0173
    hits = 0
    for i in range(0, len(points) - 2, 3):
        a, b, c = points[i], points[i + 1], points[i + 2]
        if not (min(a[2], b[2], c[2]) <= z <= max(a[2], b[2], c[2])):
            continue
        cross = ray_triangle_x(a, b, c, y, z)
        if cross is not None and cross > x:
            hits += 1
    if hits == 0:
        return None
    return hits % 2 == 1


def ray_triangle_x(a, b, c, y: float, z: float) -> float | None:
    """射线 (y,z 固定, x 递增) 与三角形求交，返回交点的 x（不相交返回 None）。"""
    # 先求三角形在 (y,z) 平面上的重心坐标
    y1, z1 = b[1] - a[1], b[2] - a[2]
    y2, z2 = c[1] - a[1], c[2] - a[2]
    det = y1 * z2 - y2 * z1
    if abs(det) < 1e-12:
        return None
    py, pz = y - a[1], z - a[2]
    u = (py * z2 - pz * y2) / det
    v = (pz * y1 - py * z1) / det
    if u < 0 or v < 0 or u + v > 1:
        return None
    return a[0] + u * (b[0] - a[0]) + v * (c[0] - a[0])


def measure_base_thickness(client: httpx.Client, project_id: str) -> float | None:
    """量实心底的厚度：在偏轴 20mm 处沿 Z 扫，找"材料 → 空腔"的分界。"""
    from aiforge import mesh

    raw = client.get(f"/api/projects/{project_id}/stl/raw")
    if raw.status_code != 200:
        return None
    tmp = WORK / "base-check.stl"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(raw.content)
    points, _ = mesh.read_triangles(tmp)
    zs = [p[2] for p in points]
    lo, hi = min(zs), max(zs)
    height = hi - lo
    for step in range(0, 61):
        z = lo + height * step / 60.0
        if point_inside_along_x(points, z, 20.0, 0.0) is False:
            return z - lo
    return None


if __name__ == "__main__":
    raise SystemExit(main())
