"""AI 3D Forge —— 全局路径与运行环境解析。

设计原则（对应需求第三十七、四十五节）：
  * 程序自身可以安装在任意位置（默认 D 盘，少占 C 盘）。
  * 用户数据（配置、API Key、项目、STL）一律放在用户目录，绝不放在 Program Files。
      - 配置 / 运行组件：%APPDATA%\\AI-3D-Forge\\   （localappdata 放体积大的运行组件）
      - 项目 / STL 导出：%USERPROFILE%\\Documents\\AI-3D-Forge\\Projects\\
  * 升级软件时必须保留以上目录 —— 安装器只覆盖程序目录。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "AI 3D Forge"
APP_SLUG = "AI-3D-Forge"
APP_ID = "ai-3d-forge"


# --------------------------------------------------------------------------
# 基础运行环境判断
# --------------------------------------------------------------------------
def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包后的发布版中。"""
    return bool(getattr(sys, "frozen", False))


def is_windows() -> bool:
    return os.name == "nt"


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


def bundle_root() -> Path:
    """只读资源（前端 dist、内置模板）所在目录。"""
    if is_frozen():
        # PyInstaller onedir: 资源随 exe 同级（sys._MEIPASS 指向 _internal）
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    # 源码运行：src/aiforge/paths.py -> 项目根
    return Path(__file__).resolve().parents[2]


def install_root() -> Path:
    """程序安装目录（卸载时会被删除，不要往里写用户数据）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# 可写目录
# --------------------------------------------------------------------------
def roaming_dir() -> Path:
    """%APPDATA%\\AI-3D-Forge —— 配置与轻量数据。"""
    override = _env_path("AIFORGE_DATA_DIR")
    if override:
        return override
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_SLUG
    # 非 Windows 兜底（开发/测试用）
    return Path.home() / f".{APP_ID}"


def local_dir() -> Path:
    """%LOCALAPPDATA%\\AI-3D-Forge —— 运行组件（OpenSCAD 绿色版）、日志、缓存。

    放 LOCALAPPDATA 而非 APPDATA：体积大、可重建、不需要跟随漫游配置文件。
    """
    override = _env_path("AIFORGE_LOCAL_DIR")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_SLUG
    return roaming_dir() / "local"


def documents_dir() -> Path:
    """用户的「文档」真实目录（可能被 OneDrive 重定向，必须查注册表）。"""
    override = _env_path("AIFORGE_PROJECTS_DIR")
    if override:
        return override

    if is_windows():
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            )
            try:
                value, _ = winreg.QueryValueEx(key, "Personal")
                expanded = os.path.expandvars(str(value))
                if expanded:
                    return Path(expanded)
            finally:
                winreg.CloseKey(key)
        except OSError:
            pass

    home = Path.home()
    for candidate in (home / "Documents", home / "文档"):
        if candidate.exists():
            return candidate
    return home / "Documents"


@dataclass(frozen=True)
class AppPaths:
    """一次性解析出全部路径，并保证可写目录已创建。"""

    config_file: Path
    secret_file: Path
    log_dir: Path
    runtime_dir: Path
    cache_dir: Path
    data_dir: Path
    projects_dir: Path
    exports_dir: Path
    downloads_dir: Path

    @classmethod
    def resolve(cls) -> "AppPaths":
        roaming = roaming_dir()
        local = local_dir()
        docs = documents_dir()
        projects = docs / APP_SLUG / "Projects"
        return cls(
            config_file=roaming / "config.json",
            secret_file=roaming / "credentials.json",
            log_dir=local / "logs",
            runtime_dir=local / "runtime",
            cache_dir=local / "cache",
            data_dir=roaming,
            projects_dir=projects,
            exports_dir=projects,
            downloads_dir=local / "cache" / "downloads",
        )

    def ensure(self) -> "AppPaths":
        """创建所有需要的目录；失败时抛出带中文说明的 OSError。"""
        for directory in (
            self.data_dir,
            self.log_dir,
            self.runtime_dir,
            self.cache_dir,
            self.projects_dir,
            self.downloads_dir,
        ):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise OSError(
                    f"无法创建目录 {directory}：{exc.strerror or exc}。"
                    "请检查该位置是否存在、是否有写入权限，或磁盘是否已满。"
                ) from exc
        return self

    def is_writable(self, directory: Path) -> tuple[bool, str]:
        """实际写入测试，用于环境检测的「项目目录权限」一项。"""
        probe = directory / ".aiforge_write_test"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True, "可读写"
        except OSError as exc:
            return False, f"{exc.strerror or exc}"
        finally:
            if probe.exists():
                try:
                    probe.unlink()
                except OSError:
                    pass


_paths_cache: AppPaths | None = None


def paths(refresh: bool = False) -> AppPaths:
    global _paths_cache
    if _paths_cache is None or refresh:
        _paths_cache = AppPaths.resolve()
    return _paths_cache


def frontend_dir() -> Path:
    """前端构建产物目录。"""
    if is_frozen():
        return bundle_root() / "webui"
    return bundle_root() / "webui" / "dist"


def free_space_gb(path: Path) -> float | None:
    """磁盘剩余空间（GB）；用于「少占 C 盘」与磁盘已满的提前告警。"""
    try:
        import shutil

        target = path
        while not target.exists() and target.parent != target:
            target = target.parent
        return round(shutil.disk_usage(target).free / (1024**3), 2)
    except OSError:
        return None
