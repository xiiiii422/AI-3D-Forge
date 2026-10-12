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

from .paths import paths

log = logging.getLogger(__name__)

KEYRING_SERVICE = "AI-3D-Forge"
KEYRING_USER = "deepseek-api-key"
# 智谱单独一个凭据槽：混合模式需要「DeepSeek 付费生成 + 智谱免费复核」两把 Key 共存。
KEYRING_USER_GLM = "glm-api-key"


def provider_of_base(base_url: str) -> str:
    """按 base_url 判提供商（与 llm.detect_provider 同语义；config 不能反向 import llm）。"""
    url = (base_url or "").lower()
    if "deepseek" in url:
        return "deepseek"
    if "bigmodel.cn" in url or "zhipu" in url:
        return "glm"
    return "openai"

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
    # 「混合·性价比」模式：生成/修改用主模型（付费质量档），
    # 规格解析/打印性评审/AI 复核改用这个免费档（同 base_url 同 Key，只换模型）。
    # 空串 = 不启用混合，全部走主模型。
    economy_model: str = ""
    # 经济档的服务商地址（可与主提供商不同，如主=DeepSeek 付费、经济=智谱免费）。
    # 空串 = 经济档与主提供商同地址。
    economy_base_url: str = ""


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


# 外观材质：'solid' = 实面（默认），'glass' = 液态玻璃（半透 + 背景模糊 + 高光描边）。
APPEARANCES = ("solid", "glass")
# 玻璃透明度（百分比，0 = 完全不透明）。上限不是拍脑袋定的，是拿截图像素实测出来的
# （口径与数据：webui/scripts/verify-glass-contrast.mjs → webui/scripts/shots/contrast-report.json）：
#   * 普通面板（卡片/侧栏/工作台）上的文字，即使拖到 90% 也还在 4.5:1 以上
#     （最低读数 5.07:1）—— 所以「超过 60% 字就读不动」这句话是错的，不写在这里。
#   * 随透明度真实恶化的是「底纹泄漏」：面板底亮度散布从 0（实底）涨到 0.072（60%）、
#     0.094（90%）—— 再往右面板就不是一块干净的底了。这是观感判断，不冒充 WCAG。
#   * 会真的掉到 4.5:1 以下的是**浮层**（背后压着整页文字的弹窗/菜单）：拿掉下面那条
#     下限复测（NO_FLOAT_FLOOR=1），透明度 25% 时弹窗正文 4.14:1、60% 时只剩 1.84:1。
#     因此浮层在 CSS 里另有一层不透明度下限
#     （tokens.css 的 --glass-solid-float: max(94%, …)，由 scripts/selfcheck.py 守着），
#     实测读数不再随滑块变化（4.78~4.86:1）。
# 读数口径：「底」只取字形外圈 3–11px 那一圈（排除 2px 抗锯齿晕圈），不是整个裁切框 ——
# 拿远一点的开关/色块当「字底下的底」会把实底基线也量成不合格。
# 宁可让用户拿不到那种“好看但读不了”的档位。
GLASS_TRANSPARENCY_MIN = 0
GLASS_TRANSPARENCY_MAX = 60
GLASS_TRANSPARENCY_DEFAULT = 25


def clamp_glass_transparency(value: Any) -> int:
    """把透明度收成合法整数；无法解析时回到默认值（不是 0，也不是抛错）。"""
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return GLASS_TRANSPARENCY_DEFAULT
    return max(GLASS_TRANSPARENCY_MIN, min(GLASS_TRANSPARENCY_MAX, number))


@dataclass
class UiSettings:
    theme: str = "light"  # light | dark | system
    # 界面材质与玻璃透明度：只影响观感，不影响任何功能布局
    appearance: str = "solid"  # solid | glass
    glass_transparency: int = GLASS_TRANSPARENCY_DEFAULT
    last_project_id: str | None = None


@dataclass
class SlicerSettings:
    """切片软件（「送到切片软件」用）。

    default_id 存的是检测结果的 id（如 bambu_studio）；软件被卸载后
    slicers.resolve 会当作没设过，不会拿一个无效 id 去启动。只存 id，
    **不存路径** —— 路径每次从注册表/安装目录实扫，免得升级换了目录还指旧路。
    """

    default_id: str | None = None
    # 自动检测总有漏网的机器，留一个手动添加的主程序路径（只接受 .exe）
    custom_paths: list[str] = field(default_factory=list)


@dataclass
class AppConfig:
    version: int = 1
    first_run_done: bool = False
    api: ApiSettings = field(default_factory=ApiSettings)
    openscad: OpenScadSettings = field(default_factory=OpenScadSettings)
    printer: PrinterSettings = field(default_factory=PrinterSettings)
    behavior: BehaviorSettings = field(default_factory=BehaviorSettings)
    ui: UiSettings = field(default_factory=UiSettings)
    slicers: SlicerSettings = field(default_factory=SlicerSettings)

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
            ("slicers", SlicerSettings),
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
        # 后端必须**按提供商分开记**：两把 Key 本来就允许存在不同地方
        # （混合模式：DeepSeek 付费那把进凭据管理器、智谱免费那把落本地文件）。
        # 共用一个字段时，「上一次读/写的是哪把」会决定「这一把存在哪儿」的回答：
        # 本机实测 deepseek 写进凭据管理器、glm 落文件之后，`backend_for()`（deepseek）
        # 回答的是 config_file —— 而设置页与诊断页拿的就是这个值，于是告诉用户
        # 「Key 在本机配置文件里」，用户按这个说法去删/改根本找不到那把 Key。
        self._backend_by_provider: dict[str, str] = {}

    def _remember(self, provider: str, backend: str) -> str:
        self._backend = backend
        self._backend_by_provider[provider] = backend
        return backend

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
    def get(self, provider: str | None = None) -> str | None:
        """按提供商取 Key（默认 deepseek）。优先级：凭据管理器 > 配置文件 > 环境变量。

        环境变量按提供商区分（deepseek 读 DEEPSEEK_API_KEY/AIFORGE_API_KEY，
        glm 读 GLM_API_KEY）—— 混合模式两把 Key 靠这个共存。
        刻意把环境变量放最后：用户显式保存/清除的 Key 才是真意图。
        """
        prov = provider or "deepseek"
        kr_user = KEYRING_USER_GLM if prov == "glm" else KEYRING_USER
        kr = self._keyring()
        if kr is not None:
            try:
                value = kr.get_password(KEYRING_SERVICE, kr_user)
                if value:
                    self._remember(prov, "credential_manager")
                    return value
            except Exception as exc:  # noqa: BLE001
                log.info("读取凭据管理器失败：%s", exc)

        data = self._read_file(prov)
        if data:
            self._remember(prov, "config_file")
            return data

        if prov == "glm":
            env = os.environ.get("GLM_API_KEY")
        else:
            env = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("AIFORGE_API_KEY")
        if env and env.strip():
            self._remember(prov, "env")
            return env.strip()
        return None

    def set(self, value: str | None, provider: str | None = None) -> str:
        """写入 Key（按提供商分槽）；传空值表示清除该槽。返回实际使用的存储后端。"""
        prov = provider or "deepseek"
        kr_user = KEYRING_USER_GLM if prov == "glm" else KEYRING_USER
        value = (value or "").strip()
        kr = self._keyring()
        if kr is not None:
            try:
                if value:
                    kr.set_password(KEYRING_SERVICE, kr_user, value)
                else:
                    try:
                        kr.delete_password(KEYRING_SERVICE, kr_user)
                    except Exception:  # noqa: BLE001 - 不存在即视为已清除
                        pass
                self._clear_file(prov)
                return self._remember(prov, "credential_manager")
            except Exception as exc:  # noqa: BLE001
                log.warning("写入凭据管理器失败（%s），改用本地文件", exc)

        self._write_file(value, prov)
        return self._remember(prov, "config_file")

    @property
    def backend(self) -> str:
        return self.backend_for()

    def backend_for(self, provider: str | None = None) -> str:
        prov = provider or "deepseek"
        remembered = self._backend_by_provider.get(prov)
        if remembered:
            return remembered
        if prov == "glm":
            if os.environ.get("GLM_API_KEY"):
                return "env"
        elif os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("AIFORGE_API_KEY"):
            return "env"
        # 探测一次
        if self._read_file(prov):
            return "config_file"
        try:
            import keyring
            from keyring.backends import Windows

            kr_user = KEYRING_USER_GLM if prov == "glm" else KEYRING_USER
            if isinstance(keyring.get_keyring(), Windows.WinVaultKeyring):
                if keyring.get_password(KEYRING_SERVICE, kr_user):
                    return "credential_manager"
        except Exception:  # noqa: BLE001
            pass
        return "config_file"

    # --- 文件后端（受限权限，仅当前用户可读；两把 Key 共存于一个文件）------
    @staticmethod
    def _file_key(provider: str) -> str:
        return "glm_api_key" if provider == "glm" else "deepseek_api_key"

    def _read_file(self, provider: str | None = None) -> str | None:
        try:
            if not self._file.exists():
                return None
            raw = json.loads(self._file.read_text(encoding=CONFIG_ENCODING))
            value = raw.get(self._file_key(provider or "deepseek"))
            return str(value) if value else None
        except (OSError, ValueError, AttributeError):
            return None

    def _write_file(self, value: str, provider: str | None = None) -> None:
        prov = provider or "deepseek"
        existing: dict = {}
        if self._file.exists():
            try:
                existing = json.loads(self._file.read_text(encoding=CONFIG_ENCODING))
            except (OSError, ValueError):
                existing = {}
        if not isinstance(existing, dict):
            existing = {}
        if value:
            existing[self._file_key(prov)] = value
        else:
            existing.pop(self._file_key(prov), None)
        remaining = {k: v for k, v in existing.items() if k != "_comment" and v}
        if not remaining:
            self._clear_file()
            return
        self._file.parent.mkdir(parents=True, exist_ok=True)
        existing.setdefault("_comment", "本文件仅保存在本机用户目录，不会随软件升级或卸载删除。")
        _atomic_write(self._file, json.dumps(existing, ensure_ascii=False, indent=2))
        _harden_permissions(self._file)

    def _clear_file(self, provider: str | None = None) -> None:
        if provider is None:
            try:
                if self._file.exists():
                    self._file.unlink()
            except OSError:
                pass
            return
        self._write_file("", provider)


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

        from .procguard import run as guarded_run  # 与 subprocess 一样局部导入：本模块被很早引用

        user = os.environ.get("USERNAME", "")
        if not user:
            return
        guarded_run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F", "SYSTEM:F", "Administrators:F"],
            capture_output=True,
            timeout=15,
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
        return self.secrets.get(provider_of_base(self.config.api.base_url))

    def api_key_for(self, base_url: str) -> str | None:
        """混合模式用：按给定 base_url 取对应提供商的 Key。"""
        return self.secrets.get(provider_of_base(base_url))

    def set_api_key(self, value: str | None) -> str:
        backend = self.secrets.set(value, provider_of_base(self.config.api.base_url))
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
