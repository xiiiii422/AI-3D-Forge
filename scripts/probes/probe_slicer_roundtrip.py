"""真机往返：把真实项目的 3MF 送进本机真实安装的切片软件，并核对可观测证据。

只在开发机上跑（它会真的启动 Bambu Studio / OrcaSlicer），不进产品包。
判定标准全部来自进程表与命令行，不看接口"有没有报错"。

为什么从 `build/work/` 搬进版本库（2026-09-21）：`docs/COMPETITIVE-GAPS.md` §8.2 一直拿
它当铁证引用，而 `build/` 整个目录是忽略的构建产物 —— 新克隆的人根本没有这个文件，
等于文档指向一个不存在的证据。它能证明的东西（detached 真成立、命令行里真的带着我们的
3MF、快照没被自己删掉）**没有全部固化成自检**：自检只能验“杀父进程后子进程还在”，
验不了真切片器的往返。跑法：先起一个隔离后端（`AIFORGE_PORT=8771`），再
`build/venv/Scripts/python.exe scripts/probes/probe_slicer_roundtrip.py`。

（本段里刻意全用正斜杠写路径：文档串里“反斜杠 + w”不是合法转义，会招来一条
SyntaxWarning —— 而全量自检的 stderr 必须干净，因为构建脚本就按“stderr 有东西”报失败。）
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8771"
EXES = ("bambu-studio.exe", "orca-slicer.exe")


def call(method: str, path: str, payload=None):
    req = urllib.request.Request(BASE + path, method=method)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def running() -> list[dict]:
    """进程表里活着的切片软件：pid / 父进程 / 完整命令行。"""
    filt = " OR ".join(f"Name='{n}'" for n in EXES)
    ps = f"Get-CimInstance Win32_Process -Filter \"{filt}\" | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    text = out.stdout.strip()
    if not text:
        return []
    data = json.loads(text)
    return data if isinstance(data, list) else [data]


def find(rows: list[dict], pid: int) -> dict | None:
    for row in rows:
        if int(row.get("ProcessId") or 0) == int(pid):
            return row
    return None


def pick_project() -> str:
    """拿一个真有模型的项目（而不是把项目号写死，换台机器就不存在了）。"""
    code, projects = call("GET", "/api/projects")
    if code != 200 or not isinstance(projects, list):
        raise SystemExit(f"拿不到项目列表：{code}")
    for item in projects:
        if item.get("stl_url"):
            return str(item["id"])
    raise SystemExit("这台机器上还没有生成过模型，没法做往返验证")


def exe_in_command_line(cmd: str, exe: str) -> bool:
    """命令行里的可执行文件是不是接口说的那个（Bambu 会把自己的路径加引号）。"""
    head = cmd.strip().lower()
    target = exe.strip().lower()
    for candidate in (target, f'"{target}"'):
        if head.startswith(candidate):
            return True
    return False


def main() -> int:
    bad: list[str] = []
    project = pick_project()
    print(f"用项目 {project} 做往返")
    print("== 起点：进程表里已有的切片软件 ==")
    before = running()
    for row in before:
        print(f"  pid={row['ProcessId']} {row['Name']}")

    code, slicers = call("GET", "/api/slicers?rescan=1")
    print(f"\n== GET /api/slicers?rescan=1 -> {code} ==")
    for s in slicers["slicers"]:
        print(f"  {s['id']:14} {s['name']:14} version={s['version']} 来源={s['source_label']} running={s['running']}")

    # --- 第一轮：显式点名 Bambu Studio -------------------------------------
    code, res = call("POST", f"/api/projects/{project}/send-to-slicer", {"slicer": "bambu_studio"})
    print(f"\n== 送到 Bambu Studio -> {code} ==")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if code != 200:
        bad.append(f"送 Bambu Studio 返回 {code}")
    time.sleep(6)  # 给它一点时间真正建窗口/读文件
    rows = running()
    row = find(rows, res.get("pid", -1))
    if row is None:
        bad.append(f"进程表里找不到接口报的 pid={res.get('pid')}（状态 {res.get('status')}）")
    else:
        cmd = row.get("CommandLine") or ""
        print(f"  进程证据：pid={row['ProcessId']} 父进程={row['ParentProcessId']} 命令行={cmd}")
        if "slicer" not in cmd and ".3mf" not in cmd:
            bad.append(f"它的命令行里没有我们的模型文件：{cmd}")
        if not exe_in_command_line(cmd, str(res.get("exe", ""))):
            bad.append(f"命令行里的可执行文件不是接口说的 {res.get('exe')}：{cmd}")
    if res.get("status") == "started" and not str(res.get("detail", "")).count(str(res.get("pid"))):
        bad.append("started 的 detail 里没写进程号，用户无从核对")
    print(f"  送出的文件：{res.get('file')} 类型={res.get('kind')} 脱离作业={res.get('detached')}")
    if not str(res.get("file", "")).lower().endswith(".3mf"):
        bad.append(f"没走 3MF（实际 {res.get('file')}）")
    if not res.get("detached"):
        bad.append("detached 不是 True：用户关掉本软件会连带关掉切片软件")
    # 交出去的那一份必须还在（曾经的缺陷：快照排序用错了时间，把刚送出的删了）
    staged = Path(str(res.get("file", "")))
    if not staged.is_file():
        bad.append(f"送出的快照已经不在了（切片软件马上会报文件不存在）：{staged}")
    else:
        print(f"  快照核对：{staged.name} 存在，{staged.stat().st_size} 字节，mtime={time.strftime('%H:%M:%S', time.localtime(staged.stat().st_mtime))}")

    # --- 第二轮：不点名，只改设置里的默认目标 ------------------------------
    code, settings = call("GET", "/api/settings")
    print(f"\n== 当前设置里的 default_slicer = {settings.get('default_slicer')!r} (HTTP {code}) ==")
    code, body = call("PUT", "/api/settings", {"default_slicer": "orcaslicer"})
    print(f"== PUT 默认目标 = orcaslicer -> {code} ==")
    if code != 200:
        bad.append(f"存默认目标被拒：{code} {str(body)[:160]}")
    code, check = call("GET", "/api/settings")
    got = check.get("default_slicer")
    if got != "orcaslicer":
        bad.append(f"设置没存住：回读 {got!r}")
    print(f"  回读：{got!r}")

    # 存一个当前检测不到的 id 必须被拒（否则卸载后会留下幽灵默认值）
    code, body = call("PUT", "/api/settings", {"default_slicer": "not_installed_slicer"})
    if code != 409:
        bad.append(f"幽灵默认值没被拦住：{code}")
    else:
        print(f"  不存在的 id 被拒：{code} {body.get('code')} {body.get('message')}")

    code, res2 = call("POST", f"/api/projects/{project}/send-to-slicer", {})
    print(f"\n== 不点名再送一次 -> {code} ==")
    print(json.dumps(res2, ensure_ascii=False, indent=2))
    if res2.get("slicer") != "orcaslicer":
        bad.append(f"不点名时没用默认目标：拿到 {res2.get('slicer')}")
    time.sleep(6)
    row2 = find(running(), res2.get("pid", -1))
    if row2 is None:
        bad.append(f"OrcaSlicer 的 pid={res2.get('pid')} 不在进程表里（状态 {res2.get('status')}）")
    else:
        print(f"  进程证据：pid={row2['ProcessId']} 父进程={row2['ParentProcessId']} 命令行={row2.get('CommandLine')}")
    staged2 = Path(str(res2.get("file", "")))
    if not staged2.is_file():
        bad.append(f"第二次送出的快照不在了：{staged2}")
    # 同一个版本号反复送：不能因为“只留最近 5 份”把同一份删掉
    if Path(str(res.get("file"))).is_file() != bool(res.get("file")):
        bad.append("第一轮的快照在第二轮之后消失了")

    # --- 交回默认（不留副作用）--------------------------------------------
    call("PUT", "/api/settings", {"default_slicer": ""})

    print("\n== 结束时进程表里的切片软件 ==")
    for row in running():
        print(f"  pid={row['ProcessId']} {row['Name']}")

    print("\n" + ("往返验证失败：" + "；".join(bad) if bad else "SLICER_ROUNDTRIP_OK"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
