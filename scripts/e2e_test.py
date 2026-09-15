"""AI 3D Forge —— 端到端验收测试（真实 DeepSeek + 真实 OpenSCAD）。

对应需求第四十六节的验收流程：
    描述需求 → AI 生成 SCAD → OpenSCAD 编译 STL → 读取测量数据
    → AI 打印性检查 → 自然语言修改（高度改成 150mm）→ 重新生成 → 下载 STL

用法（开发者）：
    $env:DEEPSEEK_API_KEY="sk-..."
    python scripts/e2e_test.py

测试使用独立的用户目录（build\\work\\e2e-*），不会读写你正在使用的真实配置与项目。
API Key 只从环境变量读取，绝不写入任何文件。
退出码 0 表示端到端流程真实跑通。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "build" / "work"

# 断言名字里带这些片段的，考的是「这一次 AI 写出来的模型好不好」，
# 会随模型的产出波动；其余断言考的是代码契约，必须永远通过。
# 只在最后汇总时用来把失败分类说清楚，**不影响任何断言的判定与退出码**。
_AI_QUALITY_CHECKS: tuple[str, ...] = (
    "模型几何可切片",
    "非流形边在可接受范围",
    "模型不是一整件",
    "模型封闭",
    "壁厚",
    "支撑体积",
    "打印性检查",
    "用户点名的总高做到了",
    "越界模型被确定性兜底装进机器",
)

# 在导入 aiforge 之前重定向用户目录。
# 注意：LOCAL_DIR 与 selfcheck 共用，这样环境检测阶段能看到已安装的 OpenSCAD
# （相当于「程序和 OpenSCAD 都已经在这台机器上」）；
# DATA/PROJECTS 用独立目录，保证不会碰到你正在使用的真实配置与项目。
os.environ["AIFORGE_DATA_DIR"] = str(WORK / "e2e-data")
os.environ["AIFORGE_LOCAL_DIR"] = str(WORK / "testlocal")
os.environ["AIFORGE_PROJECTS_DIR"] = str(WORK / "e2e-projects")
os.environ.setdefault("AIFORGE_CONSOLE_LOG", "1")

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from aiforge import logging_setup  # noqa: E402
from aiforge.version import APP_NAME, APP_VERSION  # noqa: E402

PROMPT = "设计一个桌面手机支架，高度100mm。"
MODIFY = "高度改成150mm。"

results: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}" + (f" —— {detail}" if detail else ""), flush=True)


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


class _Tee:
    """把标准输出同时写一份到 `build\\e2e-latest.log`（UTF-8）。

    为什么要内建这一步：`scripts\\verify_e2e_evidence.py` 的判据是**日志内容**
    （规格卡片、变更契约、AI 复核这些机制到底有没有被真实触发），而它默认读
    `build\\e2e-latest.log`。以前这个文件靠人手工
    `python scripts\\e2e_test.py | Tee-Object build\\e2e-latest.log` 落盘 ——
    一旦忘了 tee，核对脚本就会拿着**上一次**的旧日志去比对，
    报出一堆莫名其妙的「缺少证据」。实测踩到过：核对脚本读的是 6 小时前
    那次没跑完的日志（只有 1020 字节），4 条证据全部误报为缺失。

    写日志失败绝不能影响验收本身（磁盘满、权限、被占用），所以整体只吞异常。
    """

    def __init__(self, stream, handle) -> None:
        self._stream = stream
        self._handle = handle

    def write(self, text: str) -> int:
        try:
            self._handle.write(text)
            self._handle.flush()
        except Exception:  # noqa: BLE001 - 日志写不进去不该影响验收
            pass
        return self._stream.write(text)

    def flush(self) -> None:
        try:
            self._handle.flush()
        except Exception:  # noqa: BLE001
            pass
        self._stream.flush()

    def isatty(self) -> bool:
        return False


def start_e2e_log() -> Path | None:
    """让本次运行自己落盘一份日志，返回日志路径（失败则返回 None）。"""
    path = ROOT / "build" / "e2e-latest.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("w", encoding="utf-8")
    except OSError as exc:
        print(f"  [WARN] 端到端日志无法写入 {path}：{exc}", file=sys.stderr)
        return None
    sys.stdout = _Tee(sys.stdout, handle)
    return path


# --------------------------------------------------------------------------
def start_server() -> tuple[threading.Thread, int]:
    from aiforge import server as srv

    thread, port = srv.start_server(preferred_port=8899)
    return thread, port


def _move_dir(source: Path, destination: Path, attempts: int = 3) -> bool:
    """把目录搬到另一个位置（用于模拟「没有 OpenSCAD 的电脑」）。

    用 shutil.move 而不是 cmd rmdir/mklink：本环境下通过 cmd 重定向 stdio 会失败，
    而且直接搬运目录比创建联接更直白、副作用更小。
    """
    if not source.exists():
        return True
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    for attempt in range(attempts):
        try:
            shutil.move(str(source), str(destination))
            return True
        except (OSError, shutil.Error) as exc:
            print(f"  [WARN] 移动目录失败（第 {attempt + 1} 次）：{exc}")
            time.sleep(1.0)
    return False


def stream_job(client: httpx.Client, job_id: str, label: str) -> dict:
    """订阅作业 SSE 流并实时打印进度；返回最终结果。

    新流程会在生成前停下来请用户确认「我对需求的理解」（规格卡片）。
    测试里自动确认，并把规格带回来供断言使用。

    返回值统一为「顶层带 event、并已把 result 的字段展开到顶层」的扁平字典，
    这样调用方可以直接用 final["scad"] / final["stl"] / final["printability"]。
    契约 3.3：done 事件的 data 是完整 Job 快照（result 在 result 字段里）。
    """
    final: dict = {}
    last_percent = -1.0
    events = 0
    confirmed = False
    captured_spec: dict | None = None
    # 进度轨迹：用户反馈「进度条不能体现准确的进度」，所以这里把真实跑一遍时
    # SSE 里发出来的 (相对时间, 阶段, 百分比) 全记下来，跑完当场断言。
    # 断言的是**流里真实发出来的值**，不是代码里写的常量。
    trace: list[tuple[float, str, float]] = []
    trace_started = time.monotonic()
    # 刷新页面时前端只剩首帧 snapshot 可用，所以「等待确认」这一步的快照
    # 必须自带 spec —— 这一条是用户在服务端死等、界面却什么都没有的根因。
    # （走事件流时快照本身可能早于状态切换，因此也接受 spec 事件这一路径。）
    snapshot_had_spec_while_waiting = False

    def _confirm() -> None:
        nonlocal confirmed
        if confirmed:
            return
        confirmed = True
        try:
            response = client.post(f"/api/jobs/{job_id}/confirm-spec", json={}, timeout=30)
            if response.status_code == 409:
                print("      [规格已无需确认]", flush=True)
            else:
                response.raise_for_status()
                print("      [已自动确认规格]", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"      [确认规格失败] {exc}", flush=True)

    def _mark_spec_seen_in_snapshot(payload: dict) -> None:
        nonlocal snapshot_had_spec_while_waiting
        if payload.get("status") != "awaiting_confirmation":
            return
        nested = payload.get("spec")
        if isinstance(nested, dict) and (nested.get("spec") or {}).get("specs"):
            snapshot_had_spec_while_waiting = True

    with client.stream("GET", f"/api/jobs/{job_id}/stream", timeout=600.0) as response:
        event_name = ""
        for raw_line in response.iter_lines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            try:
                payload = json.loads(line[5:].strip())
            except ValueError:
                continue
            events += 1

            if event_name == "snapshot":
                _mark_spec_seen_in_snapshot(payload)
                if payload.get("status") == "awaiting_confirmation":
                    _confirm()
                for message in payload.get("messages", []):
                    print(f"      · {message['text']}", flush=True)
            elif event_name == "spec":
                captured_spec = payload.get("spec") or {}
                # 真后端在用户确认后会再推一次（awaiting_confirmation=false）。
                # 能走到这里说明「等待确认时界面确实拿到了规格」。
                snapshot_had_spec_while_waiting = True
                print(
                    f"      [规格] {captured_spec.get('summary') or captured_spec.get('kind')}",
                    flush=True,
                )
                for item in (captured_spec.get("specs") or [])[:8]:
                    print(f"        · {item.get('display')}", flush=True)
                for banned in (captured_spec.get("prohibitions") or [])[:3]:
                    print(f"        ✗ 不要：{banned}", flush=True)
            elif event_name == "message":
                kind = payload.get("kind", "info")
                prefix = {"warn": "⚠ ", "error": "✗ ", "success": "✓ "}.get(kind, "· ")
                print(f"      {prefix}{payload['text']}", flush=True)
                if payload.get("detail"):
                    print(f"        {payload['detail'][:200]}", flush=True)
            elif event_name == "stage":
                if payload.get("stage") == "awaiting_confirmation":
                    _confirm()
                percent = payload.get("percent")
                if percent is not None:
                    trace.append(
                        (
                            time.monotonic() - trace_started,
                            str(payload.get("stage") or ""),
                            float(percent),
                        )
                    )
                if percent is not None and abs(float(percent) - last_percent) >= 4:
                    last_percent = float(percent)
                    print(f"      [{float(percent):5.1f}%] {payload.get('text', '')}", flush=True)
            elif event_name in ("done", "error", "cancelled"):
                print(f"      <<{event_name}>>", flush=True)
                final = {"event": event_name, **payload}
                # 终帧也是前端读百分比的地方（done 快照里就是 100.0），
                # 不记进来的话轨迹会停在导出阶段的 99%。
                terminal_percent = payload.get("percent")
                if isinstance(terminal_percent, (int, float)):
                    trace.append(
                        (
                            time.monotonic() - trace_started,
                            str(payload.get("stage") or event_name),
                            float(terminal_percent),
                        )
                    )
                # 展开 result，兼容两种载荷形状（完整快照 / 纯 JobResult）
                result = payload.get("result")
                if isinstance(result, dict):
                    for key, value in result.items():
                        final.setdefault(key, value)
                break
    print(f"      （共收到 {events} 个事件）", flush=True)
    if captured_spec is not None:
        # 注意用赋值而不是 setdefault：done 快照里也有一个 spec 键，
        # 但它的值是本次生成所用的规格结构（可能与实时推送的那份不同），
        # setdefault 会因为「键已存在」而跳过，导致实时收到的规格被丢掉。
        final["spec"] = captured_spec
    final["snapshot_had_spec_while_waiting"] = snapshot_had_spec_while_waiting
    final["progress_trace"] = trace
    # 失败时把错误信息提到顶层，方便调用方统一读取
    if final.get("status") == "error" and isinstance(final.get("error"), dict):
        final.setdefault("message", final["error"].get("message"))
    return final


def _why(result: dict) -> str:
    """失败时说清「为什么」，成功时给一句人话。

    踩过的坑：这里原来是 `str(result.get("error") or "成功")` —— 而 error 事件的
    载荷里根本没有 `error` 字段（原因在 `message` 里），于是**作业明明报错，
    日志上却写着"成功"**。这种"看起来通过"的假证据比不打印还坏。
    """
    if result.get("event") == "done":
        return "成功"
    parts = [
        str(result.get(key)).strip()
        for key in ("message", "hint", "code")
        if result.get(key)
    ]
    nested = result.get("error")
    if isinstance(nested, dict):
        parts = [str(nested.get(key)).strip() for key in ("message", "hint", "code") if nested.get(key)]
    return "；".join(parts) or f"作业以 {result.get('event')} 结束（没有给出原因）"


def check_progress_trace(result: dict, label: str) -> None:
    """进度条必须真的在动、且只进不退（跑在**真实生成**的 SSE 流上）。

    用户反馈「进度条不太规范，不能体现准确的进度」。旧实现的毛病是：
    每个阶段只有一个写死的百分比，最长的生成阶段干脆没有百分比（不定态转圈），
    于是用户无法区分「在跑」和「卡死」。
    """
    trace = result.get("progress_trace") or []
    if result.get("event") != "done":
        # 失败/取消的运行不适用这条契约（本来就不会走到 100%）
        return
    percents = [percent for _t, _stage, percent in trace]
    step(f"{label}：进度事件成序列", len(trace) >= 5, f"收到 {len(trace)} 个百分比事件")
    step(
        f"{label}：进度只进不退",
        percents == sorted(percents),
        "看不出倒退" if percents == sorted(percents) else f"{percents}",
    )
    by_stage: dict[str, set[float]] = {}
    for _t, stage, percent in trace:
        by_stage.setdefault(stage or "?", set()).add(percent)
    moving = sorted(stage for stage, values in by_stage.items() if len(values) >= 2)
    step(
        f"{label}：长阶段内确实在推进（不是一个阶段一个定格数字）",
        bool(moving),
        f"阶段内出现多个百分比：{moving}",
    )
    step(
        f"{label}：只有做完才是 100%",
        bool(percents) and percents[-1] == 100.0 and all(p < 100.0 for p in percents[:-1]),
        f"共 {len(set(percents))} 个不同取值，末值 {percents[-1] if percents else None}",
    )
    step(
        f"{label}：进度分档有区分度",
        len(set(percents)) >= 6,
        f"{sorted(set(percents))}",
    )


def stream_install(client: httpx.Client, job_id: str) -> dict:
    final: dict = {}
    with client.stream("GET", f"/api/openscad/install/{job_id}/stream", timeout=900.0) as response:
        event_name = ""
        for raw_line in response.iter_lines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            try:
                payload = json.loads(line[5:].strip())
            except ValueError:
                continue
            if event_name == "progress":
                percent = payload.get("percent")
                suffix = f"（{percent:.0f}%）" if isinstance(percent, (int, float)) else ""
                print(f"      {suffix} {payload['message']}", flush=True)
            elif event_name == "log":
                print(f"      · {payload['message']}", flush=True)
            elif event_name in ("done", "error"):
                final = {"event": event_name, **payload}
                break
    return final


# --------------------------------------------------------------------------
def main() -> int:
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        print("缺少 DEEPSEEK_API_KEY 环境变量。", file=sys.stderr)
        print('用法：$env:DEEPSEEK_API_KEY="sk-..."; python scripts/e2e_test.py', file=sys.stderr)
        return 2

    # 清空测试目录。
    # 注意：LOCAL_DIR 与 selfcheck 共用（里面有已装好的 OpenSCAD），
    # 因此这里只清 data / projects，不碰 runtime —— 第二步会单独处理 runtime。
    for directory in ("e2e-data", "e2e-projects"):
        shutil.rmtree(WORK / directory, ignore_errors=True)
    (WORK / "e2e-data").mkdir(parents=True, exist_ok=True)

    logging_setup.setup(console=False)

    # 自己落盘一份日志：verify_e2e_evidence.py 的判据就是这份日志的内容，
    # 不要依赖「记得手工 Tee 一下」（忘了就会拿旧日志误报，见 _Tee 的注释）。
    log_path = start_e2e_log()

    print("=" * 72)
    print(f"{APP_NAME} {APP_VERSION} —— 端到端验收（真实 AI + 真实 OpenSCAD）")
    print("=" * 72)
    if log_path is not None:
        print(f"本次日志：{log_path}")
    print(f"需求描述：{PROMPT}")
    print(f"修改要求：{MODIFY}")

    # 清空测试用目录，并从「未安装 OpenSCAD」的干净状态开始（随后再链接已有副本）
    for directory in ("e2e-data", "e2e-projects"):
        shutil.rmtree(WORK / directory, ignore_errors=True)

    section("启动软件")
    thread, port = start_server()
    base = f"http://127.0.0.1:{port}"
    print(f"  本机服务：{base}")
    # trust_env=False 是必须的，不是可选项：
    # httpx 默认会读系统代理（Windows 上来自注册表 Internet Settings）。
    # 这台机器上 ProxyEnable=1、代理是 127.0.0.1:7890（mihomo），
    # 于是连「访问本机服务」的请求也被送去代理，代理回一个 502 空响应，
    # 表现为 /api/app/health 解析 JSON 失败 —— 看起来像程序起不来，其实完全无关。
    # 回环地址永远不该走代理。
    client = httpx.Client(base_url=base, timeout=120.0, trust_env=False)

    runtime_root = Path(os.environ["AIFORGE_LOCAL_DIR"]) / "runtime"
    runtime_backup = Path(os.environ["AIFORGE_LOCAL_DIR"]) / "runtime.bak"
    moved = False

    try:
        health = client.get("/api/app/health").json()
        step("程序启动", health.get("ok") is True, f"版本 {health.get('version')}")

        # --- 环境检测 ---------------------------------------------------
        section("第一步：环境自动检测（需求第三十三节）")
        report = client.post("/api/env/detect").json()
        print(f"  环境就绪：{report['ok_count']}/{report['total']} 项（overall={report['overall']}）")
        for item in report["items"]:
            icon = {"ok": "✓", "warn": "⚠", "fail": "✗"}.get(item["status"], "?")
            print(f"    {icon} {item['title']}：{item['detail']}")
        step("环境检测返回 10 项以上", report["total"] >= 10, f"{report['total']} 项")
        openscad_item = next((i for i in report["items"] if i["id"] == "openscad"), None)
        step("OpenSCAD 自动检测通过", bool(openscad_item and openscad_item["status"] == "ok"),
             openscad_item["detail"] if openscad_item else "缺少该项")

        candidates = client.get("/api/openscad/candidates").json()["candidates"]
        step("能列出所有 OpenSCAD 副本", len(candidates) >= 1, f"{len(candidates)} 个：{[c['path'] for c in candidates][:2]}")

        # --- OpenSCAD 自动安装（模拟未安装的电脑）-------------------------
        section("第二步：OpenSCAD 自动安装（需求第三十四节）")
        # 这一步会把共享的 OpenSCAD 运行目录临时移走，**期间不能并行跑 selfcheck**
        # （它会因为「找不到 OpenSCAD」大量失败，看起来像代码坏了）。
        # 留一个标记文件，让并行的自检能认出「这是被端到端测试借走了」而不是真缺。
        (runtime_root.parent / "E2E-HOLDS-OPENSCAD").write_text(
            "端到端测试正在临时移走 OpenSCAD 运行目录，请等它跑完再跑 selfcheck。\n",
            encoding="utf-8",
        )
        moved = _move_dir(runtime_root, runtime_backup)
        if moved:
            print(f"  已临时移走 {runtime_root}，模拟一台没有 OpenSCAD 的电脑")
        else:
            print(f"  [WARN] 无法临时移走 {runtime_root}，将直接在现有环境上测试自动安装")

        from aiforge import config as cfg_mod

        cfg_mod.store(refresh=True).update("openscad", {"path": None, "version": None})

        before = client.get("/api/openscad/detect").json()
        step("未安装时能被识别为缺失", before["found"] is False, "已正确识别为未安装")

        install_started = client.post("/api/openscad/install", json={"confirm": True, "method": "portable"})
        step("自动安装接口接受请求", install_started.status_code == 202, str(install_started.status_code))
        install_final = stream_install(client, install_started.json()["job_id"])
        installed_info = (install_final.get("info") or {}) if isinstance(install_final.get("info"), dict) else {}
        step(
            "自动安装完成并配置",
            install_final.get("event") == "done" and bool(installed_info.get("found")),
            f"{install_final.get('message')} → {installed_info.get('path')}",
        )

        after = client.get("/api/openscad/detect").json()
        step(
            "安装后再次检测到 OpenSCAD",
            after["found"] and after["cli_ok"],
            f"{after['version']} @ {after['path']}",
        )

        cli_test = client.post("/api/openscad/test").json()
        step("CLI 冒烟测试通过", cli_test["ok"] is True, cli_test["message"])

        # --- DeepSeek 配置 ----------------------------------------------
        section("第三步：DeepSeek API 配置（需求第四十五节）")
        key = os.environ["DEEPSEEK_API_KEY"].strip()
        saved = client.put("/api/settings", json={"api_key": key, "model": "deepseek-chat"}).json()
        step("API Key 已保存", saved["api_key_set"] is True, f"掩码 {saved['api_key_masked']}（存储：{saved['api_key_store']}）")
        step(
            "接口不回传完整 Key",
            key not in json.dumps(saved, ensure_ascii=False),
            "只返回掩码值",
        )
        tested = client.post("/api/settings/api-key/test", json={})
        if tested.status_code == 400:
            step("API Key 真实可用", False, f"HTTP 400：{tested.json().get('detail')}")
        else:
            step("API Key 真实可用", tested.json().get("ok") is True, str(tested.json().get("message")))

        # --- 打印机配置与知识库 -----------------------------------------
        section("第三步补：打印机配置与知识库（机型真正影响生成）")
        printers = client.get("/api/printers").json()
        step("内置机型库可读", len(printers["printers"]) >= 10, f"{len(printers['printers'])} 台机型")
        names = [p["name"] for p in printers["printers"][:4]]
        print(f"      例如：{' / '.join(names)}")

        # 选一台小尺寸机器（Prusa MINI+ 180³）作为后面的基准
        tiny = next((p for p in printers["printers"] if p["id"] == "prusa-mini-04"), None)
        step("能指定目标机型", tiny is not None, tiny["name"] if tiny else "未找到 prusa-mini-04")
        if tiny:
            chosen = client.post(
                "/api/printers/select", json={"printer_id": "prusa-mini-04", "material": "PLA"}
            ).json()
            c = chosen["printer"]["constraints"]
            step(
                "切换机型后约束随之改变",
                chosen["printer"]["id"] == "prusa-mini-04" and c["max_build_x"] == 180,
                f"成型 {c['max_build_x']:.0f}×{c['max_build_y']:.0f}×{c['max_build_z']:.0f}mm，"
                f"最小壁厚 {c['min_wall_mm']}mm",
            )
            print("      该机型的设计要求：")
            for line in chosen["printer"]["checklist"]:
                print(f"        · {line}")

        # 换一台大机器，同一个需求应当允许更大的尺寸
        client.post("/api/printers/select", json={"printer_id": "large-500-04", "material": "PLA"})
        large = client.get("/api/printers/selected").json()
        step(
            "切换到大机型后限制放宽",
            large["printer"]["constraints"]["max_build_x"] == 500,
            f"{large['printer']['name']}：成型 {large['printer']['constraints']['max_build_x']:.0f}mm，"
            f"最小壁厚 {large['printer']['constraints']['min_wall_mm']}mm",
        )

        # --- 成型范围硬校验（确定性，不依赖 AI 这次是否听话）----------------
        # 用一个「参数化的大模型」走完整的生成链路（create 模式）。
        # 模型本身由程序给出的尺寸决定（240×40×20mm），远超 Prusa MINI 的 180³，
        # 因此无论 AI 怎么写代码，程序都必须把它处理到装得下为止。
        # 这样验证的是「程序一定拦得住」，而不是「AI 这次有没有遵守提示词」。
        client.post("/api/printers/select", json={"printer_id": "prusa-mini-04", "material": "PLA"})
        oversized = client.post(
            "/api/jobs",
            json={
                "prompt": (
                    "设计一个用于验证成型范围的实心长方体试件。"
                    "必须使用参数 length = 240（单位 mm）作为长度，宽 40mm，厚 20mm，"
                    "不要添加任何其它结构，不要分件，就一个长方体。"
                ),
                "mode": "create",
                "name": "成型范围硬校验",
            },
        )
        step("越界用例任务已创建", oversized.status_code == 202, oversized.text[:110])
        fit_result = stream_job(client, oversized.json()["job_id"], "越界用例")

        if fit_result.get("event") == "done":
            bbox = (fit_result.get("stl") or {}).get("bbox_mm") or {}
            within = (fit_result.get("stl") or {}).get("within_build_volume")
            step(
                "程序自动把越界模型处理到装得下这台机器",
                within is True
                and float(bbox.get("x", 1e9)) <= 180.0
                and float(bbox.get("z", 1e9)) <= 180.0,
                f"最终尺寸 {bbox.get('x')}×{bbox.get('y')}×{bbox.get('z')}mm"
                f"（上限 180×180×180mm），within_build_volume={within}",
            )
            step(
                "结果里带回了打印机上下文",
                bool(fit_result.get("printer")) and fit_result["printer"]["id"] == "prusa-mini-04",
                f"打印机={((fit_result.get('printer') or {}).get('name'))}，"
                f"材料={((fit_result.get('printer') or {}).get('material'))}",
            )
            step(
                "耗材重量按材料密度估算",
                bool((fit_result.get("stl") or {}).get("material"))
                and ((fit_result.get("stl") or {}).get("mass_g") or 0) > 0,
                f"材料={(fit_result.get('stl') or {}).get('material')}"
                f"（密度 {(fit_result.get('stl') or {}).get('material_density')}）"
                f"→ {(fit_result.get('stl') or {}).get('mass_g')}g",
            )
        else:
            step("越界用例完成", False, str(fit_result.get("message"))[:150])

        # 同一个大模型在大机型上必须保持原始尺寸（不该被无谓缩放）
        client.post("/api/printers/select", json={"printer_id": "large-500-04", "material": "PLA"})
        big_again = client.post(
            "/api/jobs",
            json={"prompt": "设计一个实心长方体试件：长 240mm、宽 40mm、厚 20mm，就一个长方体。",
                  "mode": "create", "name": "大机型对照"},
        )
        big_fit = stream_job(client, big_again.json()["job_id"], "大机型对照") if big_again.status_code == 202 else {}
        if big_fit.get("event") == "done":
            bbox = (big_fit.get("stl") or {}).get("bbox_mm") or {}
            step(
                "同一需求在大机型上不被缩放",
                float(bbox.get("x", 0)) > 100,
                f"尺寸 {bbox.get('x')}×{bbox.get('y')}×{bbox.get('z')}mm（大机型上限 500mm）",
            )
        else:
            step("大机型对照完成", False, str(big_fit.get("message"))[:120])

        # 知识库自述
        knowledge = client.get("/api/printers/knowledge/summary").json()
        step(
            "知识库能解释每项参数的影响",
            len(knowledge["features"]) >= 8,
            f"{knowledge['builtin_count']} 台机型，{len(knowledge['features'])} 项特性说明",
        )
        print("      知识库覆盖的特性：")
        for feature in knowledge["features"][:5]:
            print(f"        · {feature['label']}：{feature['affects']}")

        # 换回通用机型，后续步骤沿用
        client.post("/api/printers/select", json={"printer_id": "generic-220-04", "material": "PLA"})
        step("可切回通用机型", client.get("/api/printers/selected").json()["printer"]["id"] == "generic-220-04")

        # --- 生成模型 ---------------------------------------------------
        section("第四步：用一句话生成 3D 模型（需求第四十四条核心流程）")
        started = client.post("/api/jobs", json={"prompt": PROMPT, "mode": "create"})
        step("生成任务已创建", started.status_code == 202, started.text[:120])
        ref = started.json()
        project_id = ref["project_id"]
        job_result = stream_job(client, ref["job_id"], "生成")

        step("AI 生成 SCAD 并编译成功", job_result.get("event") == "done", _why(job_result))
        check_progress_trace(job_result, "生成")

        if job_result.get("event") != "done":
            print(json.dumps(job_result, ensure_ascii=False, indent=2)[:2000])
            return 1

        scad = job_result.get("scad") or ""
        step("得到参数化 SCAD 源码", "// @param" in scad or "=" in scad, f"{len(scad)} 字符")

        # 规格确认关卡：必须先问、必须带上规格、必须能确认后才继续
        first_spec = job_result.get("spec") or {}
        step(
            "生成前先确认「我对需求的理解」",
            bool(first_spec.get("specs")),
            f"{first_spec.get('kind') or first_spec.get('summary')}；"
            f"{len(first_spec.get('specs') or [])} 项规格",
        )
        step(
            "等待确认时快照自带规格（刷新页面卡片不会丢）",
            bool(job_result.get("snapshot_had_spec_while_waiting")),
            "首帧 snapshot 里带 spec 且含规格项",
        )
        user_values = {
            s["name"]: s.get("value")
            for s in (first_spec.get("specs") or [])
            if s.get("user_specified")
        }
        step(
            "用户亲口说的尺寸被标记为「你指定」",
            any(abs(float(v or 0) - 100.0) < 0.01 for v in user_values.values()),
            f"你指定：{user_values}",
        )
        print("      —— 生成的 SCAD 源码（前 22 行）——")
        for line in scad.splitlines()[:22]:
            print(f"      | {line}")

        stl = job_result.get("stl") or {}
        step(
            "生成 STL 文件",
            bool(stl) and stl.get("size_bytes", 0) > 0,
            f"{stl.get('triangles')} 个三角面，{stl.get('size_bytes')} 字节",
        )
        bbox = stl.get("bbox_mm") or {}
        print(f"      模型尺寸：{bbox.get('x')} × {bbox.get('y')} × {bbox.get('z')} mm")
        print(f"      体积：{stl.get('volume_mm3')} mm³，耗材约 {stl.get('mass_g')} g")

        # 几何完整性用**绝对判据**，不用比例：
        #   开放边（boundary edge）= 网格有破洞，切片会出问题 → 必须为 0
        #   非流形边（>2 个面共享一条边）→ 允许极少数，但必须被打印性检查报出来
        # 用 watertight_ratio 做阈值不稳：边总数少时，1 条非流形边就能把比例拉到 0.95 以下。
        mesh_metrics = stl.get("metrics") or {}
        boundary = int(mesh_metrics.get("boundary_edges") or 0)
        non_manifold = int(mesh_metrics.get("non_manifold_edges") or 0)
        step(
            "模型几何可切片（无开放边）",
            boundary == 0,
            f"开放边 {boundary}，非流形边 {non_manifold}，封闭率 {stl.get('watertight_ratio')}",
        )
        step(
            "非流形边在可接受范围（≤ 5 条）",
            non_manifold <= 5,
            f"非流形边 {non_manifold} 条（{stl.get('triangles')} 个三角面）",
        )
        if non_manifold or boundary:
            issues_text = json.dumps(job_result.get("printability") or {}, ensure_ascii=False)
            step(
                "几何不完美时打印性检查如实报出",
                "非流形" in issues_text or "封闭" in issues_text or "开放边" in issues_text,
                "AI 检查结论中已提到该几何问题",
            )

        printability = job_result.get("printability") or {}
        step(
            "AI 打印性检查完成",
            bool(printability),
            f"评分 {printability.get('score')}／结论 {printability.get('verdict')}：{printability.get('summary')}",
        )
        step(
            "打印性检查对应用的是选中机型",
            (printability.get("printer") or {}).get("id") == "generic-220-04",
            f"基准机型 = {((printability.get('printer') or {}).get('name'))}",
        )
        for issue in (printability.get("issues") or [])[:5]:
            print(f"        - [{issue['severity']}] {issue['title']}：{issue['detail']}")
            print(f"          建议：{issue['suggestion']}")

        # --- Three.js 预览所需的数据 -------------------------------------
        section("第五步：3D 预览数据（需求第三十九节 STL 预览）")
        raw = client.get(f"/api/projects/{project_id}/stl/raw")
        step("STL 可被预览组件读取", raw.status_code == 200 and len(raw.content) > 84,
             f"{len(raw.content)} 字节，Content-Type={raw.headers.get('content-type')}")

        thumb = client.get(f"/api/projects/{project_id}/thumbnail")
        step("缩略图可用", thumb.status_code == 200 and thumb.text.startswith("<svg"),
             f"{thumb.headers.get('content-type')}")

        metrics = client.get(f"/api/projects/{project_id}/metrics").json()
        step("模型测量数据可用", metrics.get("triangles", 0) > 0,
             f"{metrics.get('triangles')} 面，{metrics.get('volume_cm3')} cm³")

        # --- 自然语言修改 -----------------------------------------------
        section("第六步：自然语言修改「高度改成150mm」")
        before_z = (stl.get("bbox_mm") or {}).get("z", 0)
        modify = client.post("/api/jobs", json={"instruction": MODIFY, "project_id": project_id, "mode": "modify"})
        step("修改任务已创建", modify.status_code == 202, modify.text[:120])
        modify_result = stream_job(client, modify.json()["job_id"], "修改")
        step("修改后重新生成成功", modify_result.get("event") == "done", _why(modify_result))
        check_progress_trace(modify_result, "修改")

        if modify_result.get("event") == "done":
            new_stl = modify_result.get("stl") or {}
            after_z = (new_stl.get("bbox_mm") or {}).get("z", 0)
            new_scad = modify_result.get("scad") or ""
            # 三层断言，从「意图」一路查到「用户拿到的东西」：
            #
            # 1) 源码层：用户点名的 150 必须真的写进顶层参数赋值。
            #    只查数字不查参数名 —— 参数名由 AI 起，契约核对（下一步）才管「只改该改的」。
            step(
                "点名尺寸写进了源码（顶层参数赋值为 150）",
                bool(re.search(r"(?m)^\s*[A-Za-z_$][\w$]*\s*=\s*150(?:\.0+)?\s*;", new_scad)),
                f"新源码 {len(new_scad)} 字符",
            )
            delta = float(after_z) - float(before_z)
            # 2) 几何层：改动必须真的传到了几何（参数被静默忽略时 Δ≈0）。
            step(
                "改动传到了几何（总高确实变大）",
                delta > 5.0,
                f"修改前 Z={before_z}mm → 修改后 Z={after_z}mm（Δ={delta:+.2f}mm）",
            )
            # 3) 需求层：直接核对**用户点名的那个数字**，用产品自己的口径
            #    （`jobs._spec_dimension_problems`：实测 < 声明的 90% 就算没做到）。
            #
            #    为什么不再用「Δ≈50±8」：Δ 只是代理量。设计里一旦出现倾斜件，
            #    总高就不是参数的线性函数 —— 实测 AI 会把手机支架的靠背做成
            #    18° 倾斜（源码里 `back_len = (height - base_h) / cos(rad)`，
            #    但两次 rotate 之后长度方向接近水平），于是「参数 +50」只让
            #    总高 +41。那种情况下 Δ 判据会在**设计本身就没做到**时报错，
            #    报的却不是「用户要的 150mm 没做到」这件事，指向性很差。
            #    直接查绝对高度既更贴近需求，也不会漏掉「参数被忽略」。
            named = 150.0
            step(
                "用户点名的总高做到了（实测 ≥ 声明的 90%）",
                float(after_z) >= named * 0.9,
                f"点名 {named:g}mm，实测总高 {after_z}mm（{float(after_z) / named:.0%}）",
            )

            # --- 本次改造的核心断言：只改了该改的 ---------------------------
            # 契约核对是「只改该改的」的**权威判据**：它比的是源码顶层的参数赋值，
            # 不是 AI 自报的参数表。自报参数表常常多出/少掉条目（AI 会顺手加参数），
            # 拿它做判据会得出「一切正常」的假象。
            verdict = modify_result.get("change_verdict") or {}
            step(
                "改动通过契约校验（只改授权内容）",
                bool(verdict.get("ok")) is True,
                f"校验结论：{modify_result.get('change_summary')}；"
                f"未授权改动={verdict.get('unauthorized')}",
            )

            # 源码级对比：除用户点名的那一项，其它顶层参数必须一字未改
            def top_numbers(src: str) -> dict:
                found: dict = {}
                for line in (src or "").splitlines():
                    if line[:1].isspace():
                        continue
                    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(-?\d+(?:\.\d+)?)\s*;", line)
                    if m and not m.group(1).startswith("$"):
                        found[m.group(1)] = float(m.group(2))
                return found

            before_src = top_numbers(job_result.get("scad"))
            after_src = top_numbers(modify_result.get("scad"))
            moved = [
                n for n in before_src if n in after_src and abs(before_src[n] - after_src[n]) > 1e-9
            ]
            step(
                "源码里除点名项外，其它顶层参数一个都没变",
                len(moved) <= 1,
                f"发生变化的顶层参数：{moved or '（无）'}；共 {len(before_src)} 个参与对比",
            )

            # 规格必须随项目持久化并保留用户指定的数值（这是「说过的不再忘」）。
            # 注意：修改类作业不推 spec 事件（只有从零设计才需要确认关卡），
            # 所以这里核对的是**项目里真正存下来的那份规格** —— 重开软件后
            # 续改靠的就是它。
            detail = client.get(f"/api/projects/{project_id}").json()
            persisted = detail.get("spec") or {}
            persisted_values = {
                s["name"]: s.get("value")
                for s in (persisted.get("specs") or [])
                if s.get("user_specified")
            }
            step(
                "设计规格已保留并随项目持久化",
                bool(persisted.get("fingerprint")) and bool(persisted_values),
                f"指纹 {persisted.get('fingerprint')}；"
                f"你指定的项 {list(persisted_values)[:6]}",
            )
            # 指纹还要写进源码本身，这样下一轮修改能认出「还是同一个设计」
            step(
                "生成的源码里带着规格指纹",
                "// @spec" in (modify_result.get("scad") or ""),
                "modify 结果里的 SCAD 顶部应有 @spec 注释",
            )

            step("项目保留了历史版本", len(detail.get("history") or []) >= 2,
                 f"{len(detail.get('history'))} 条历史，当前 r{detail.get('revision')}")
            step("项目文件已保存到用户目录", Path(detail["files"]["scad_path"]).is_file(),
                 detail["files"]["scad_path"])
            step(
                "项目里存下了设计规格（重开软件后还记得你的要求）",
                bool((detail.get("spec") or {}).get("fingerprint")),
                str((detail.get("spec") or {}).get("kind")),
            )

        # --- 参数微调（不消耗 AI 额度）-----------------------------------
        section("第七步：参数微调（不调用 AI，秒级重编译）")
        params = (modify_result.get("params") if modify_result.get("event") == "done" else None) or []
        # 由 AI 自己决定参数名，测试取第一个「尺寸类」参数来验证，不硬编码具体名字
        height_param = next(
            (p for p in params if any(k in p["name"].lower() for k in ("height", "total", "size"))),
            params[0] if params else None,
        )
        if height_param:
            param_name = height_param["name"]
            tweak_value = round(float(height_param["value"]) * 1.6, 1)
            metrics_resp = client.get(f"/api/projects/{project_id}/metrics")
            pre_metrics = metrics_resp.json() if metrics_resp.status_code == 200 else {}
            pre_z = (pre_metrics.get("bbox") or {}).get("z")
            # 「拿到一个能用的基准尺寸」本身也要当成一步来报。
            # 原先这里直接把 pre_z 丢进 float()：接口一旦没给 bbox，测试会以
            # TypeError 崩掉整轮验收，而不是报告"这一步失败了" —— 崩掉会掩盖掉
            # 后面所有步骤的结果，看起来像"测试脚本坏了"，其实是被测接口变了。
            step(
                "调参前能读到模型实测尺寸",
                isinstance(pre_z, (int, float)),
                f"HTTP {metrics_resp.status_code}；bbox={pre_metrics.get('bbox')}",
            )
            tweak = client.post(f"/api/projects/{project_id}/params", json={"params": {param_name: tweak_value}})
            step("参数调整任务已创建", tweak.status_code == 202, tweak.text[:100])
            tweak_result = stream_job(client, tweak.json()["job_id"], "调参")
            if tweak_result.get("event") == "done":
                new_params = {p["name"]: p["value"] for p in (tweak_result.get("params") or [])}
                # 断言要容忍我们**自己**在提示词里要求的对齐写法：源码长这样
                #   `height   = 240;`
                # 早期这里写成字面量 `f"{name} = {value}" in scad`，一旦 AI 把等号
                # 对齐（我们正是这么要求的）就永远匹配不上 —— 模型明明改对了，
                # 报告却说"参数没生效"。这类"拿字面量当断言"的脆弱检查见 §55。
                in_source = bool(
                    re.search(
                        rf"(?m)^\s*{re.escape(param_name)}\s*=\s*{tweak_value:g}(?:\.0+)?\s*;",
                        tweak_result.get("scad") or "",
                    )
                )
                value_ok = abs(float(new_params.get(param_name, 0)) - tweak_value) < 1e-6
                stl_info = tweak_result.get("stl") or {}
                z = (stl_info.get("bbox_mm") or {}).get("z")
                measured_ok = isinstance(z, (int, float))
                step(
                    "调参结果里带着模型实测尺寸",
                    measured_ok,
                    f"stl 字段：{sorted(stl_info)[:10]}；bbox_mm={stl_info.get('bbox_mm')}",
                )
                # 高度类参数会反映到 Z 尺寸，但总高通常还包含底座等其它结构，
                # 因此断言「整体明显变高」而不是「恰好等于参数值」。
                raised = bool(
                    measured_ok
                    and isinstance(pre_z, (int, float))
                    and float(z) > float(pre_z) * 1.1
                )
                step(
                    f"参数生效（{param_name}: {height_param['value']} → {tweak_value}，未调用 AI）",
                    in_source and value_ok and raised,
                    f"写进源码={in_source}、回读参数={new_params.get(param_name)}"
                    f"（期望 {tweak_value}，{'一致' if value_ok else '不一致'}）、"
                    f"Z 尺寸 {pre_z}mm → {z}mm（含底座等结构故不等高）",
                )
            else:
                step("参数微调任务完成", False, str(tweak_result.get("message")))
        else:
            step("参数微调", False, f"模型没有暴露任何参数（实际：{params}）")

        # --- 下载 -------------------------------------------------------
        section("第八步：下载 SCAD / STL 与项目保存")
        stl_download = client.get(f"/api/projects/{project_id}/stl")
        disposition = stl_download.headers.get("content-disposition", "")
        # 除了 RFC 5987 的 filename*，ASCII 兜底也必须**有主名**：
        # 中文项目名去掉非 ASCII 之后只剩扩展名，会退化成 `filename=".stl"` ——
        # 只认 filename= 的客户端会存下一个隐藏文件，用户以为下载失败。
        ascii_stem = ""
        if 'filename="' in disposition:
            ascii_name = disposition.split('filename="')[1].split('"')[0]
            ascii_stem = ascii_name.rpartition(".")[0]
        step(
            "STL 可下载且文件名正确",
            stl_download.status_code == 200
            and len(stl_download.content) > 84
            and "filename*" in disposition
            and not ascii_stem.startswith("."),
            f"ASCII 兜底主名={ascii_stem!r}；{disposition[:90]}",
        )
        scad_download = client.get(f"/api/projects/{project_id}/scad")
        step(
            "SCAD 可下载",
            scad_download.status_code == 200 and "cube" in scad_download.text or "//" in scad_download.text,
            f"{len(scad_download.text)} 字符",
        )

        projects = client.get("/api/projects").json()
        step("项目列表可读", len(projects) >= 1, f"{len(projects)} 个项目，最近的：{projects[0]['name']}")

        # --- 诊断与错误恢复 ---------------------------------------------
        section("第九步：系统诊断与错误恢复")
        diag = client.get("/api/diagnostics").json()
        groups = {g["group"]: g["items"] for g in diag["groups"]}
        print(f"  诊断总分：{diag['overall']}")
        for name, items in groups.items():
            icons = "".join({"ok": "✓", "warn": "⚠", "fail": "✗"}.get(i["status"], "?") for i in items)
            print(f"    {icons}  {name}")
        step(
            "诊断中心覆盖全部关键分组",
            {"AI 服务", "3D 引擎", "模型生成", "STL 预览", "文件系统", "网络"} <= set(groups),
            "、".join(groups.keys()),
        )

        bad = client.post("/api/compile", json={"scad": "cube([1,1,1)"})
        bad_result = stream_job(client, bad.json()["job_id"], "错误用例") if bad.status_code == 202 else {}
        step(
            "语法错误被友好处理（不出现英文堆栈）",
            bad_result.get("event") == "error" and "Error" not in str(bad_result.get("message", "")),
            str(bad_result.get("message", ""))[:150],
        )

        # 先记录环境变量来源的 Key（本测试用它免落盘），再临时摘掉它以验证「清除」与
        # 「未配置 Key 时的快速失败」——真实用户机器上不会有这个变量。
        env_key = os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            missing_key = client.put("/api/settings", json={"api_key": ""}).json()
            step("API Key 可清除", missing_key["api_key_set"] is False,
                 f"清除后 api_key_set={missing_key['api_key_set']}，存储位置={missing_key['api_key_store']}")
            no_key_job = client.post("/api/jobs", json={"prompt": "测试"})
            step(
                "未配置 Key 时快速失败并给出修复入口",
                no_key_job.status_code == 409 and no_key_job.json().get("code") == "NO_API_KEY",
                no_key_job.json().get("hint", ""),
            )
            # 环境变量作为兜底：没有保存过 Key 时应当能读到它
            os.environ["DEEPSEEK_API_KEY"] = env_key or ""
            fallback = client.get("/api/settings").json()
            step(
                "环境变量可作为兜底来源（并如实告知用户）",
                fallback["api_key_store"] == "env" and bool(fallback.get("api_key_source_note")),
                str(fallback.get("api_key_source_note"))[:90],
            )
        finally:
            if env_key:
                os.environ["DEEPSEEK_API_KEY"] = env_key

        restored = client.put("/api/settings", json={"api_key": key}).json()
        step("API Key 可重新写入", restored["api_key_set"] is True, restored["api_key_masked"])

        # --- 数据保留 ---------------------------------------------------
        section("第十步：数据保留（需求第四十四、四十五条）")
        settings = client.get("/api/settings").json()
        # 本测试用 AIFORGE_DATA_DIR 把用户目录重定向到 build\work（测试隔离手段）。
        # 这里临时摘掉该变量，确认「真实用户环境」下的默认位置符合 Windows 规范。
        from aiforge.paths import AppPaths

        saved_overrides = {}
        for name in ("AIFORGE_DATA_DIR", "AIFORGE_LOCAL_DIR", "AIFORGE_PROJECTS_DIR"):
            if name in os.environ:
                saved_overrides[name] = os.environ.pop(name)
        try:
            default_paths = AppPaths.resolve()
        finally:
            os.environ.update(saved_overrides)

        step(
            "默认用户数据目录符合 Windows 规范（%APPDATA%）",
            "AppData" in str(default_paths.data_dir) and "AI-3D-Forge" in str(default_paths.data_dir),
            str(default_paths.data_dir),
        )
        step(
            "默认项目目录在「文档」下（卸载/升级都不会丢）",
            "Projects" in str(default_paths.projects_dir) and "AI-3D-Forge" in str(default_paths.projects_dir),
            str(default_paths.projects_dir),
        )
        step("测试实例的数据目录已隔离", "build" in settings["data_dir"], settings["data_dir"])
        step("API Key 已恢复可用", settings["api_key_set"] is True, settings["api_key_masked"])

    finally:
        client.close()
        from aiforge import runtime_state

        runtime_state.stop_server()
        thread.stop()
        thread.join(timeout=8)

        # 收尾：删掉本次自动安装产生的副本，恢复测试前那份 OpenSCAD
        if moved:
            final_runtime = Path(os.environ["AIFORGE_LOCAL_DIR"]) / "runtime"
            shutil.rmtree(final_runtime, ignore_errors=True)
            try:
                shutil.move(str(runtime_backup), str(final_runtime))
                print(f"  已恢复原有 OpenSCAD：{final_runtime}")
            except OSError as exc:
                print(f"  [WARN] 恢复原有 OpenSCAD 失败：{exc}（下次自检会重新安装）")
        # 无论成功与否都要清掉「借用中」标记，否则自检会一直以为在并行跑
        (Path(os.environ["AIFORGE_LOCAL_DIR"]) / "E2E-HOLDS-OPENSCAD").unlink(missing_ok=True)

    print("\n" + "=" * 72)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [r for r in results if not r[1]]
    print(f"端到端验收：通过 {passed} 项，失败 {len(failed)} 项")
    if failed:
        print("-" * 72)
        for name, _, detail in failed:
            print(f"  ✗ {name}：{detail}")
        # 把「这一版模型没做好」和「代码坏了」分开说。
        #
        # 为什么必须分开：端到端里混着两类断言 ——
        #   * 代码契约类：接口形状、状态机、机制有没有被触发、错误处理。
        #     它们与 AI 这一版写了什么形状**完全无关**，红了就是回归。
        #   * AI 产出质量类：几何封不封闭、是不是一整件、点名尺寸做到没有…
        #     同一个模型、同一份代码，跑两次结果可能不同（实测 64/64 与 62/64
        #     交替出现过，失败项全是几何质量）。
        # 不分开的话，一次运气不好的生成看起来和代码回归一模一样，
        # 排查要花掉几十分钟 —— 实测就是这么被坑过一次。
        # **注意：exit code 不因此改变**（失败就是失败），这只是把话说清楚。
        ai_like = [r for r in failed if any(p in r[0] for p in _AI_QUALITY_CHECKS)]
        print("-" * 72)
        print(
            f"  失败构成：AI 产出质量类 {len(ai_like)} 项（随模型产出波动）、"
            f"代码契约类 {len(failed) - len(ai_like)} 项（必须为 0）"
        )
        if ai_like and len(ai_like) == len(failed):
            print("  → 本次失败全部落在「这一版模型没做好」上，代码契约零失败。")
            print("     先重跑一次确认不是运气问题；**连续两次都红就是真回归**，必须查。")
        else:
            print("  → 存在代码契约类失败：这是回归，不允许靠重跑掩盖。")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
