"""单点诊断：为什么「把高度改成50mm」没有落到几何上？

只做两件事：生成 Ø80×100 笔筒、把高度改成 50mm，然后把作业的
**全部阶段消息**、改动合同、最终 SCAD 打印出来，定位改动在哪一步被丢掉。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "build" / "work" / "probe-height"
os.environ["AIFORGE_DATA_DIR"] = str(WORK / "data")
os.environ["AIFORGE_PROJECTS_DIR"] = str(WORK / "projects")
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

BASE_PROMPT = (
    "设计一个圆形的桌面笔筒：外径 80mm、总高 100mm、壁厚 2.5mm，"
    "底部是封闭的实心底，顶部敞开。"
)


def run_job(client: httpx.Client, payload: dict, verbose: bool = False) -> dict:
    created = client.post("/api/jobs", json=payload)
    if created.status_code != 202:
        return {"event": "rejected", "message": created.text[:300]}
    job_id = created.json()["job_id"]
    project_id = created.json()["project_id"]
    deadline = time.time() + 420
    confirmed = False
    seen: set[str] = set()
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/api/jobs/{job_id}").json()
        if verbose:
            for msg in last.get("messages") or []:
                key = json.dumps(msg, ensure_ascii=False, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    print(f"    [{msg.get('level')}] {msg.get('text')}", flush=True)
        status = last.get("status")
        if status == "awaiting_confirmation" and not confirmed:
            client.post(f"/api/jobs/{job_id}/confirm-spec", json={})
            confirmed = True
        if status in ("done", "error", "cancelled"):
            break
        time.sleep(2)
    last["project_id"] = project_id
    return last


def dump(label: str, client: httpx.Client, project_id: str) -> None:
    metrics = client.get(f"/api/projects/{project_id}/metrics").json()
    print(f"  {label}: bbox={metrics.get('bbox')} 体积={metrics.get('volume_mm3'):.0f} "
          f"封闭={metrics.get('watertight')} 件数={metrics.get('solid_parts')}")
    scad = client.get(f"/api/projects/{project_id}/scad")
    if scad.status_code == 200:
        lines = scad.text.splitlines()
        print(f"  SCAD {len(lines)} 行：")
        for line in lines[:26]:
            print(f"      {line}")
    spec = (client.get(f"/api/projects/{project_id}").json() or {}).get("spec") or {}
    print(f"  规格：kind={spec.get('kind')} 指纹={spec.get('fingerprint')}")
    for item in spec.get("items") or []:
        print(f"      - {item.get('label')}={item.get('value')} "
              f"({item.get('name')}) 用户指定={item.get('user_specified')}")


def main() -> int:
    from aiforge import server as srv
    from aiforge.config import store

    if not store().api_key:
        print("没有 API Key")
        return 2
    thread, port = srv.start_server(preferred_port=8917)
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=180.0, trust_env=False)
    print(f"服务：http://127.0.0.1:{port}")
    try:
        print("\n=== 1. 生成 ===")
        first = run_job(client, {"prompt": BASE_PROMPT, "mode": "create"})
        print(f"  状态={first.get('status')} 错误={str(first.get('error'))[:200]}")
        if first.get("status") != "done":
            return 1
        pid = first["project_id"]
        dump("基线", client, pid)

        print("\n=== 2. 改高度 → 50mm（打印每一步消息）===")
        result = run_job(client, {"instruction": "把高度改成50mm。", "project_id": pid,
                                 "mode": "modify"}, verbose=True)
        print(f"  状态={result.get('status')} revision={result.get('revision')} "
              f"错误={str(result.get('error'))[:200]}")
        dump("改后", client, pid)

        print("\n=== 3. 该项目的修订目录 ===")
        proj_dir = WORK / "projects"
        for proj in sorted(proj_dir.iterdir()):
            revs = proj / "revisions"
            if revs.exists():
                for r in sorted(revs.iterdir()):
                    scad = r / "model.scad"
                    size = scad.stat().st_size if scad.exists() else -1
                    print(f"    {proj.name}/{r.name}: model.scad={size} 字节")
        return 0
    finally:
        client.close()
        thread.stop()
        thread.join(timeout=8)


if __name__ == "__main__":
    raise SystemExit(main())
