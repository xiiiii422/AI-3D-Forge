"""AI 3D Forge —— 项目持久化。

存储位置（需求第四十五节：用户数据不能因升级而丢失）：
    %USERPROFILE%\\Documents\\AI-3D-Forge\\Projects\\<项目名>-<短ID>\\
        model.scad          当前模型源码（用户可直接用 OpenSCAD 打开）
        model.stl           当前 STL
        meta.json           项目元数据（名称、参数、修订号、时间）
        history.jsonl       追加式历史记录，每次生成一行，永不覆盖
        revisions\\         历史 SCAD/STL（可在设置中关闭以节省空间）
        export\\            用户「另存为」的副本

写入全部原子化：先写临时文件再替换，避免断电或崩溃损坏用户项目。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .paths import paths

log = logging.getLogger(__name__)

NAME_SAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MAX_NAME_LEN = 40


def now_iso() -> str:
    """本地时区的 ISO8601 时间戳（用户在界面上看到的是本地时间）。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def safe_name(name: str, fallback: str = "未命名模型") -> str:
    """把用户/模型给出的名字变成合法的 Windows 文件名片段。"""
    cleaned = NAME_SAFE_RE.sub("", (name or "").strip())
    cleaned = cleaned.replace(" ", "").strip(". ")
    # 截断之后还要再剪一次尾巴：实测 `"a"*39 + "." + "b"*8` 切到 40 位正好停在**点号**上，
    # 而 Windows 的路径规范化会把尾部的 `.` / 空格去掉 —— 于是建出来的目录真名是 39 个 a，
    # 代码里拿到的名字却带着那个点。读写靠规范化仍能找到它，但任何拿目录名做
    # 相等比较的代码（`iterdir()` 给的是无点那个）会两边对不上。
    cleaned = cleaned[:MAX_NAME_LEN].strip(". ")
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def _project_id(data: dict[str, Any], directory: Path) -> str:
    """项目的 id：meta.json 里那个；没有就用目录名后 6 位。

    **列表与查找必须用同一个算法**。实测过的形状：meta.json 被手改过或早期
    版本写坏时没有 `id` —— 旧写法 `list()` 拿「目录名后 6 位」凑一个给用户看，
    而 `directory_for()` 只比对 meta 里的 `id` 字段（那时是 None），于是这个项目
    在侧栏里看得见、点进去就 404 —— 用户的感受是「软件把我的项目弄丢了」。
    """
    return str(data.get("id") or directory.name[-6:])


def atomic_write_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class HistoryEntry:
    revision: int
    created_at: str
    prompt: str | None = None
    instruction: str | None = None
    summary: str = ""
    name: str = ""
    stl: bool = False
    metrics: dict[str, Any] | None = None
    printability: dict[str, Any] | None = None
    params: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "created_at": self.created_at,
            "prompt": self.prompt,
            "instruction": self.instruction,
            "summary": self.summary,
            "name": self.name,
            "stl": self.stl,
            "metrics": self.metrics,
            "printability": self.printability,
            "params": self.params,
        }


@dataclass
class Project:
    id: str
    name: str
    dir: Path
    created_at: str
    updated_at: str
    revision: int = 0
    summary: str | None = None
    last_status: str = "unknown"
    params: dict[str, float] = field(default_factory=dict)
    param_meta: list[dict[str, Any]] = field(default_factory=list)
    history: list[HistoryEntry] = field(default_factory=list)
    metrics: dict[str, Any] | None = None
    printability: dict[str, Any] | None = None
    # 生成这个项目时用的打印机（换机后需要提示用户「这是按旧机器设计的」）
    printer: dict[str, Any] | None = None
    # 设计规格（DesignSpec）：用户意图的累积记录，跨会话不丢。
    # 没有它，用户上一轮说过的约束下一轮就会被忘掉。
    spec: dict[str, Any] | None = None

    # --- 路径 -----------------------------------------------------------
    @property
    def scad_path(self) -> Path:
        return self.dir / "model.scad"

    @property
    def stl_path(self) -> Path:
        return self.dir / "model.stl"

    @property
    def three_mf_path(self) -> Path:
        """切片软件用的 3MF（带毫米单位与打印机/材料元数据）。"""
        return self.dir / "model.3mf"

    @property
    def meta_path(self) -> Path:
        return self.dir / "meta.json"

    @property
    def history_path(self) -> Path:
        return self.dir / "history.jsonl"

    @property
    def thumbnail_path(self) -> Path:
        return self.dir / "thumbnail.svg"

    @property
    def export_dir(self) -> Path:
        return self.dir / "export"

    def revision_dir(self, revision: int) -> Path:
        return self.dir / "revisions" / f"r{revision:03d}"

    # --- 读取 -----------------------------------------------------------
    def read_scad(self) -> str:
        try:
            return self.scad_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    # --- 序列化 ---------------------------------------------------------
    def to_summary(self) -> dict[str, Any]:
        has_stl = self.stl_path.is_file()
        has_thumb = self.thumbnail_path.is_file()
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
            "thumbnail_url": f"/api/projects/{self.id}/thumbnail" if has_thumb else None,
            "stl_url": f"/api/projects/{self.id}/stl/raw" if has_stl else None,
            "summary": self.summary,
            "last_status": self.last_status,
        }

    def to_detail(self) -> dict[str, Any]:
        detail = self.to_summary()
        detail.update(
            {
                "scad": self.read_scad(),
                "params": self.param_meta
                or [
                    {"name": k, "label": k, "value": v, "unit": "mm", "min": None, "max": None}
                    for k, v in self.params.items()
                ],
                "history": [
                    {
                        "revision": entry.revision,
                        "prompt": entry.prompt,
                        "instruction": entry.instruction,
                        "created_at": entry.created_at,
                        "summary": entry.summary,
                    }
                    for entry in self.history
                ],
                "files": {
                    "scad_path": str(self.scad_path),
                    "stl_path": str(self.stl_path) if self.stl_path.is_file() else None,
                    "export_dir": str(self.export_dir),
                },
                "metrics": self.metrics,
                "printability": self.printability,
                "printer": self.printer,
                "spec": self.spec,
            }
        )
        return detail


# --------------------------------------------------------------------------
# 仓库
# --------------------------------------------------------------------------
class ProjectStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or paths().projects_dir
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OSError(
                f"无法创建项目目录 {self.root}：{exc.strerror or exc}。"
                "请检查磁盘空间，或在「设置」中更换项目位置。"
            ) from exc

    # --- 基本操作 -------------------------------------------------------
    def create(self, name: str = "未命名模型") -> Project:
        timestamp = now_iso()
        project_id = uuid.uuid4().hex[:12]
        folder = f"{safe_name(name)}-{project_id[:6]}"
        directory = self.root / folder
        counter = 1
        while directory.exists():
            directory = self.root / f"{folder}-{counter}"
            counter += 1
        project = Project(
            id=project_id,
            name=name.strip() or "未命名模型",
            dir=directory,
            created_at=timestamp,
            updated_at=timestamp,
        )
        directory.mkdir(parents=True, exist_ok=True)
        self.save(project)
        return project

    def directory_for(self, project_id: str) -> Path | None:
        for entry in self._iter_dirs():
            meta = entry / "meta.json"
            if meta.is_file():
                try:
                    data = json.loads(meta.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if _project_id(data, entry) == project_id:
                    return entry
        return None

    def get(self, project_id: str) -> Project | None:
        directory = self.directory_for(project_id)
        if directory is None:
            return None
        return self._load(directory)

    def list(self) -> list[Project]:
        projects: list[Project] = []
        for directory in self._iter_dirs():
            project = self._load(directory)
            if project is not None:
                projects.append(project)
        projects.sort(key=lambda p: p.updated_at, reverse=True)
        return projects

    def delete(self, project_id: str) -> bool:
        """移入「已删除」暂存区而非彻底删除 —— 误删可恢复。"""
        directory = self.directory_for(project_id)
        if directory is None:
            return False
        trash = self.root / ".deleted"
        trash.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = trash / f"{directory.name}__{stamp}"
        try:
            shutil.move(str(directory), str(target))
            return True
        except OSError as exc:
            log.warning("移入回收站失败：%s", exc)
            return False

    # --- 内部 -----------------------------------------------------------
    def _iter_dirs(self) -> Iterator[Path]:
        try:
            for entry in sorted(self.root.iterdir()):
                if entry.is_dir() and not entry.name.startswith("."):
                    yield entry
        except OSError:
            return

    def _load(self, directory: Path) -> Project | None:
        meta_path = directory / "meta.json"
        if not meta_path.is_file():
            return None
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("项目元数据损坏：%s", meta_path)
            return None

        project = Project(
            id=_project_id(data, directory),
            name=str(data.get("name") or directory.name),
            dir=directory,
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
            revision=int(data.get("revision") or 0),
            summary=data.get("summary"),
            last_status=str(data.get("last_status") or "unknown"),
            params=dict(data.get("params") or {}),
            param_meta=list(data.get("param_meta") or []),
            metrics=data.get("metrics"),
            printability=data.get("printability"),
            printer=data.get("printer"),
            spec=data.get("spec"),
        )
        project.history = self._load_history(directory)
        return project

    def _load_history(self, directory: Path) -> list[HistoryEntry]:
        path = directory / "history.jsonl"
        entries: list[HistoryEntry] = []
        if not path.is_file():
            return entries
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                entries.append(
                    HistoryEntry(
                        revision=int(raw.get("revision") or 0),
                        created_at=str(raw.get("created_at") or ""),
                        prompt=raw.get("prompt"),
                        instruction=raw.get("instruction"),
                        summary=str(raw.get("summary") or ""),
                        name=str(raw.get("name") or ""),
                        stl=bool(raw.get("stl")),
                        metrics=raw.get("metrics"),
                        printability=raw.get("printability"),
                        params=dict(raw.get("params") or {}),
                    )
                )
        except OSError:
            pass
        return entries

    # --- 保存 -----------------------------------------------------------
    def save(self, project: Project) -> None:
        payload = {
            "schema": 1,
            "id": project.id,
            "name": project.name,
            "created_at": project.created_at,
            "updated_at": project.updated_at,
            "revision": project.revision,
            "summary": project.summary,
            "last_status": project.last_status,
            "params": project.params,
            "param_meta": project.param_meta,
            "metrics": project.metrics,
            "printability": project.printability,
            "printer": project.printer,
            "spec": project.spec,
            "app": "AI 3D Forge",
        }
        atomic_write_json(project.meta_path, payload)

    def append_history(self, project: Project, entry: HistoryEntry) -> None:
        line = json.dumps(entry.to_dict(), ensure_ascii=False)
        try:
            project.history_path.parent.mkdir(parents=True, exist_ok=True)
            with project.history_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            log.warning("写入历史记录失败：%s", exc)

    def archive_revision(self, project: Project, revision: int, keep_stl: bool = True) -> None:
        """把当前版本归档到 revisions\\rNNN\\，保证历史可回溯。"""
        target = project.revision_dir(revision)
        try:
            target.mkdir(parents=True, exist_ok=True)
            if project.scad_path.is_file():
                shutil.copy2(project.scad_path, target / "model.scad")
            if keep_stl and project.stl_path.is_file():
                shutil.copy2(project.stl_path, target / "model.stl")
        except OSError as exc:
            log.info("归档历史版本失败（不影响主流程）：%s", exc)

    def rename(self, project: Project, name: str) -> Project:
        project.name = name.strip() or project.name
        project.updated_at = now_iso()
        self.save(project)
        return project

    # --- 导出 -----------------------------------------------------------
    def export_copy(self, project: Project, target_dir: Path | None = None) -> dict[str, str | None]:
        """把 SCAD/STL/3MF 复制到 export\\ 目录，便于用户交给切片软件或分享。"""
        destination = target_dir or project.export_dir
        destination.mkdir(parents=True, exist_ok=True)
        stem = safe_name(project.name, "model")
        scad_target = destination / f"{stem}.scad"
        stl_target = destination / f"{stem}.stl"
        mf_target = destination / f"{stem}.3mf"
        scad_out: str | None = None
        stl_out: str | None = None
        mf_out: str | None = None
        try:
            if project.scad_path.is_file():
                shutil.copy2(project.scad_path, scad_target)
                scad_out = str(scad_target)
            if project.stl_path.is_file():
                shutil.copy2(project.stl_path, stl_target)
                stl_out = str(stl_target)
            if project.three_mf_path.is_file():
                shutil.copy2(project.three_mf_path, mf_target)
                mf_out = str(mf_target)
        except OSError as exc:
            log.warning("导出副本失败：%s", exc)
        return {"scad": scad_out, "stl": stl_out, "threemf": mf_out, "dir": str(destination)}

    def download_name(self, project: Project, suffix: str) -> str:
        return f"{safe_name(project.name, 'model')}.{suffix}"


_store: ProjectStore | None = None


def store() -> ProjectStore:
    global _store
    if _store is None:
        _store = ProjectStore()
    return _store


def reset_for_tests() -> None:
    global _store
    _store = None
