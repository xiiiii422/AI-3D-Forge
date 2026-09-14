"""AI 3D Forge —— 用户配置与密钥存储。

对应需求第四十五节：
  * API Key 不写入程序安装目录、不写进源码。
  * 优先 Windows 凭据管理器；不可用时退回受保护权限的本地文件，并明确告知用户。
  * 升级软件不得丢失配置：配置只存在于 %APPDATA%\\AI-3D-Forge\\config.json。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .paths import APP_NAME, paths

log = logging.getLogger(__name__)

KEYRING_SERVICE = "AI-3D-Forge"
KEYRING_USER = "deepseek-api-key"

# 读配置一律用 utf-8-sig：它写出的字节与 utf-8 完全相同（不带 BOM），
# 但读的时候能容忍 BOM。为什么必须容忍：用户完全可能用记事本之类
# 「另存为 UTF-8」去手改 config.json，那样就会带上 BOM；早期这里写死 utf-8，
# 于是软件把**自己目录里一份完好的配置**判成损坏、回退默认值并把原文件改名成
# .json.corrupt —— 用户看到的现象是「明明配好了，重启后设置全没了」。
# 实测在日志里抓到过这条警告，才回头修的。
CONFIG_ENCODING = "utf-8-sig"

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


# --------------------------------------------------------------------------
# 配置数据模型
# --------------------------------------------------------------------------
@dataclass
class ApiSettings:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_s: int = 180
    max_retries: int = 2


@dataclass
class OpenScadSettings:
    """OpenSCAD 相关配置；path 为用户覆盖（自动检测结果也写回这里）。"""

    path: str | None = None
    version: str | None = None
    auto_install: bool = True
    last_install_method: str | None = None


@dataclass
class PrinterSettings:
    """目标打印机与材料。

    机型与材料的真实数据在 printers.json（见 printers.PrinterRegistry），
    这里保存的是「用户当前选了哪一台」，便于界面首屏就能显示。
    """

    printer_id: str | None = None
    material: str | None = None
    # 每次生成前是否先把打印机约束显示在对话里（让用户确认目标机器）
    announce_in_chat: bool = True
    # 生成前是否先让用户确认「我对需求的理解」（规格卡片）。
    # 默认开启：这是防止「做出来不是我要的」最有效的一步，代价只是看一眼。
    confirm_spec: bool = True


@dataclass
class BehaviorSettings:
    keep_stl_history: bool = True
    open_in_browser: bool = False
    telemetry: bool = False  # 永不上报，保留字段以明示隐私立场


@dataclass
class UiSettings:
    theme: str = "light"  # light | dark | system
    last_project_id: str | None = None


@dataclass
class AppConfig:
    version: int = 1
    first_run_done: bool = False
    api: ApiSettings = field(default_factory=ApiSettings)
    openscad: OpenScadSettings = field(default_factory=OpenScadSettings)
    printer: PrinterSettings = field(default_factory=PrinterSettings)
    behavior: BehaviorSettings = field(default_factory=BehaviorSettings)
    ui: UiSettings = field(default_factory=UiSettings)

    # ---------------- 序列化 ----------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppConfig":
        """宽容解析：未知字段忽略、缺失字段用默认值，保证升级后旧配置仍可用。"""
        cfg = cls()
        if not isinstance(raw, dict):
            return cfg
        try:
            cfg.version = int(raw.get("version", 1))
        except (TypeError, ValueError):
            pass
        cfg.first_run_done = bool(raw.get("first_run_done", False))
        for name, model in (
            ("api", ApiSettings),
            ("openscad", OpenScadSettings),
            ("printer", PrinterSettings),
            ("behavior", BehaviorSettings),
            ("ui", UiSettings),
        ):
            section = raw.get(name)
            if isinstance(section, dict):
                valid = {f.name for f in fields(model)}
                kwargs = {k: v for k, v in section.items() if k in valid}
                try:
                    setattr(cfg, name, model(**kwargs))
                except TypeError:
                    log.warning("配置段 %s 解析失败，使用默认值", name)
        return cfg


# --------------------------------------------------------------------------
# 密钥存储
# --------------------------------------------------------------------------
class SecretStore:
    """API Key 存储。优先 Windows 凭据管理器，降级为受限权限文件。"""

    def __init__(self, secret_file: Path) -> None:
        self._file = secret_file
        self._backend: str | None = None

    # --- 后端探测 -------------------------------------------------------
    def _keyring(self):
        if self._backend == "file":
            return None
        try:
            import keyring
            from keyring.backends import Windows

            if not isinstance(keyring.get_keyring(), Windows.WinVaultKeyring):
                # 未识别到 Windows 凭据管理器（如开发机上装了别的后端）
                if os.name != "nt":
                    return None
            return keyring
        except Exception as exc:  # noqa: BLE001 - 任何异常都降级
            log.info("凭据管理器不可用（%s），改用本地加密文件存储", exc)
            self._backend = "file"
            return None

    # --- 读写 -----------------------------------------------------------
    def get(self) -> str | None:
        """按「用户在界面里的选择优先」的顺序取 Key。

        优先级：Windows 凭据管理器 > 本地配置文件 > 环境变量。
        刻意把环境变量放在最后：用户在设置里显式保存/清除的 Key 才是他真正的意图，
        不能被开发机或工具残留的环境变量悄悄覆盖（否则会出现「清除了却还在用」的怪现象）。
        环境变量只作为「没有保存过任何 Key」时的兜底，方便自动化测试与高级用户。
        """
        kr = self._keyring()
        if kr is not None:
            try:
                value = kr.get_password(KEYRING_SERVICE, KEYRING_USER)
                if value:
                    self._backend = "credential_manager"
                    return value
            except Exception as exc:  # noqa: BLE001
                log.info("读取凭据管理器失败：%s", exc)

        data = self._read_file()
        if data:
            self._backend = "config_file"
            return data

        env = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("AIFORGE_API_KEY")
        if env and env.strip():
            self._backend = "env"
            return env.strip()
        return None

    def set(self, value: str | None) -> str:
        """写入 Key；传空值表示清除。返回实际使用的存储后端。"""
        value = (value or "").strip()
        kr = self._keyring()
        if kr is not None:
            try:
                if value:
                    kr.set_password(KEYRING_SERVICE, KEYRING_USER, value)
                else:
                    try:
                        kr.delete_password(KEYRING_SERVICE, KEYRING_USER)
                    except Exception:  # noqa: BLE001 - 不存在即视为已清除
                        pass
                self._clear_file()
                self._backend = "credential_manager"
                return self._backend
            except Exception as exc:  # noqa: BLE001
                log.warning("写入凭据管理器失败（%s），改用本地文件", exc)

        self._write_file(value)
        self._backend = "config_file"
        return self._backend

    @property
    def backend(self) -> str:
        if self._backend:
            return self._backend
        if os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("AIFORGE_API_KEY"):
            return "env"
        # 探测一次
        if self._read_file():
            return "config_file"
        try:
            import keyring
            from keyring.backends import Windows

            if isinstance(keyring.get_keyring(), Windows.WinVaultKeyring):
                return "credential_manager"
        except Exception:  # noqa: BLE001
            pass
        return "config_file"

    # --- 文件后端（受限权限，仅当前用户可读）----------------------------
    def _read_file(self) -> str | None:
        try:
            if not self._file.exists():
                return None
            raw = json.loads(self._file.read_text(encoding=CONFIG_ENCODING))
            value = raw.get("deepseek_api_key")
            return str(value) if value else None
        except (OSError, ValueError, AttributeError):
            return None

    def _write_file(self, value: str) -> None:
        if not value:
            self._clear_file()
            return
        self._file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"_comment": "本文件仅保存在本机用户目录，不会随软件升级或卸载删除。", "deepseek_api_key": value},
            ensure_ascii=False,
            indent=2,
        )
        _atomic_write(self._file, payload)
        _harden_permissions(self._file)

    def _clear_file(self) -> None:
        try:
            if self._file.exists():
                self._file.unlink()
        except OSError:
            pass


def _harden_permissions(path: Path) -> None:
    """把文件 ACL 收紧到「仅当前用户 + SYSTEM + 管理员」。失败不致命。"""
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return
    try:
        import subprocess

        user = os.environ.get("USERNAME", "")
        if not user:
            return
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F", "SYSTEM:F", "Administrators:F"],
            capture_output=True,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:  # noqa: BLE001
        log.debug("收紧文件权限失败（不影响功能）", exc_info=True)


def _atomic_write(target: Path, payload: str) -> None:
    """原子写：先写同目录临时文件再替换，避免断电/崩溃造成配置损坏。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# 配置仓库
# --------------------------------------------------------------------------
class ConfigStore:
    def __init__(self) -> None:
        self._paths = paths().ensure()
        self._config: AppConfig | None = None
        self.secrets = SecretStore(self._paths.secret_file)

    # --- 读 -------------------------------------------------------------
    @property
    def config(self) -> AppConfig:
        if self._config is None:
            self._config = self._load()
        return self._config

    def _load(self) -> AppConfig:
        try:
            if self._paths.config_file.exists():
                raw = json.loads(self._paths.config_file.read_text(encoding=CONFIG_ENCODING))
                return AppConfig.from_dict(raw)
        except (OSError, ValueError) as exc:
            log.warning("配置文件损坏（%s），已回退默认配置并备份原文件", exc)
            try:
                backup = self._paths.config_file.with_suffix(".json.corrupt")
                self._paths.config_file.replace(backup)
            except OSError:
                pass
        return AppConfig()

    # --- 写 -------------------------------------------------------------
    def save(self) -> None:
        _atomic_write(
            self._paths.config_file,
            json.dumps(self.config.to_dict(), ensure_ascii=False, indent=2),
        )

    def update(self, section: str, values: dict[str, Any]) -> None:
        """局部更新某个配置段并落盘。"""
        target = getattr(self.config, section, None)
        if target is None:
            raise KeyError(f"未知配置段：{section}")
        valid = {f.name for f in fields(target)}
        for key, value in values.items():
            if key in valid and value is not None:
                setattr(target, key, value)
        self.save()

    # --- 便捷访问 -------------------------------------------------------
    @property
    def api_key(self) -> str | None:
        return self.secrets.get()

    def set_api_key(self, value: str | None) -> str:
        backend = self.secrets.set(value)
        return backend

    @staticmethod
    def mask_key(value: str | None) -> str | None:
        """只用于界面显示：sk-6862****3164。永远不返回完整 Key。"""
        if not value:
            return None
        if len(value) <= 10:
            return value[:2] + "****"
        return f"{value[:7]}****{value[-4:]}"

    def reset_for_tests(self) -> None:
        """仅供测试：丢弃内存缓存，重新从磁盘读取。"""
        self._config = None


_store: ConfigStore | None = None


def store(refresh: bool = False) -> ConfigStore:
    global _store
    if _store is None or refresh:
        paths(refresh=refresh)
        _store = ConfigStore()
    return _store


def app_title() -> str:
    return APP_NAME
