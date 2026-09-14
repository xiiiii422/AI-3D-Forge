"""AI 3D Forge —— SCAD 源码安全校验。

两道防线：
  1. 静态检查：拒绝能让 OpenSCAD 读写本地文件、联网或生成病态几何的语句。
  2. 规模保护：拒绝会生成百万级面片、把用户机器卡死的设计。

对用户而言，失败信息必须是「人话 + 该怎么办」，而不是语法分析报告。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 这些函数会读取本机文件或访问网络。AI 生成的代码绝不应该需要它们。
FORBIDDEN_CALLS = {
    "import": "读取外部文件（import）",
    "surface": "读取外部数据文件（surface）",
    "import_stl": "读取外部 STL 文件",
    "import_dxf": "读取外部 DXF 文件",
    "import_off": "读取外部 OFF 文件",
    "dxf_linear_extrude": "读取外部 DXF 文件",
    "dxf_rotate_extrude": "读取外部 DXF 文件",
    "import_svg": "读取外部 SVG 文件",
}

# `include <...>` / `use <...>` 同样是**读本机文件**的指令 ——
# 它们把另一个 .scad 源文件读进来，和 import()/surface() 属于同一类操作，
# 但用的是尖括号而不是圆括号，所以上面那套「函数名 + (」的检查抓不到它们。
# 实测（修复前）：`include <C:/Windows/win.ini>;` 与 `use </etc/passwd>;`
# 都能顺利通过校验，与「只允许生成纯几何模型」的既有承诺不符，故单独拦。
# 注意用 (?<![A-Za-z0-9_$]) 避免误伤 `include_margin = 5;` 这类普通变量名。
INCLUDE_DIRECTIVE_RE = re.compile(
    r"(?<![A-Za-z0-9_$])(include|use)\s*<", re.IGNORECASE
)

# 视为可疑的模块（可能用于生成病态几何，仅告警不拦截）
RISKY_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"\b(?:sphere|circle)\s*\(\s*[^)]*\$\s*fn\s*=\s*(\d{4,})", re.IGNORECASE),
        "片段数过高",
        "球体/圆的分辨率设置非常高，会生成海量面片并显著拖慢生成速度。",
    ),
    (
        re.compile(r"\bfor\s*\([^)]*\b1\s*:\s*(\d{4,})\b", re.IGNORECASE),
        "循环次数过大",
        "循环次数非常大，可能生成超出 3D 打印合理范围的模型。",
    ),
]

# OpenSCAD 预定义的、不允许作为参数名的标识符
RESERVED = {
    "$fn", "$fa", "$fs", "$t", "$vpr", "$vpt", "$vpd", "$children", "$preview",
    "true", "false", "undef", "PI", "inf", "nan",
}

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 单个模型的最大合理包围盒（毫米）。超出即认为不是 3D 打印件。
MAX_DIMENSION_MM = 2000.0
MIN_DIMENSION_MM = 0.2


@dataclass
class ScadIssue:
    severity: str  # "error" | "warn"
    title: str
    detail: str
    suggestion: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "suggestion": self.suggestion,
        }


@dataclass
class ScadValidation:
    ok: bool
    issues: list[ScadIssue]
    model_name: str | None
    summary: str | None
    params: dict[str, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "issues": [i.to_dict() for i in self.issues],
            "model_name": self.model_name,
            "summary": self.summary,
            "params": self.params,
        }


def strip_comments(source: str) -> str:
    """去掉 // 行注释与 /* */ 块注释，避免注释里的文字触发误判。"""
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    source = re.sub(r"//[^\n]*", " ", source)
    return source


def strip_strings(source: str) -> str:
    """去掉字符串字面量内容（保留引号），避免 echo("import") 之类误判。"""
    return re.sub(r'"(?:[^"\\]|\\.)*"', '""', source)


def validate(source: str) -> ScadValidation:
    """校验 AI 生成的 SCAD 源码。"""
    issues: list[ScadIssue] = []

    if not source or not source.strip():
        return ScadValidation(
            ok=False,
            issues=[
                ScadIssue(
                    severity="error",
                    title="没有生成任何模型代码",
                    detail="AI 返回的内容为空。",
                    suggestion="请重新描述一次你的需求，或换一种说法。",
                )
            ],
            model_name=None,
            summary=None,
            params={},
        )

    if len(source) > 400_000:
        issues.append(
            ScadIssue(
                severity="error",
                title="模型代码过长",
                detail=f"生成了 {len(source)} 个字符的代码，超出安全上限。",
                suggestion="请把需求拆成更简单的形状，或减少细节描述。",
            )
        )

    cleaned = strip_strings(strip_comments(source))
    lowered = cleaned.lower()

    # --- 危险函数 ---------------------------------------------------------
    for name, description in FORBIDDEN_CALLS.items():
        if re.search(rf"(?<![A-Za-z0-9_$]){re.escape(name)}\s*\(", lowered):
            issues.append(
                ScadIssue(
                    severity="error",
                    title="模型代码包含不安全的操作",
                    detail=f"检测到 {description}。出于安全考虑，本软件只允许生成纯几何模型。",
                    suggestion="请重新描述需求，不要引用外部文件。",
                )
            )

    # --- 读入外部源文件（include / use）------------------------------------
    # 尖括号写法，圆括号那套匹配不到，必须单独查。
    include_hit = INCLUDE_DIRECTIVE_RE.search(cleaned)
    if include_hit:
        issues.append(
            ScadIssue(
                severity="error",
                title="模型代码包含不安全的操作",
                detail=(
                    f"检测到 {include_hit.group(1)} <…> 指令：它会读入本机的另一个 .scad 源文件，"
                    "属于文件读取操作。出于安全考虑，本软件只允许生成纯几何模型。"
                ),
                suggestion="请重新描述需求，不要引用外部文件或库。",
            )
        )

    # --- 显式文件读写 -----------------------------------------------------
    if re.search(r"\b(?:system|exec|shell)\s*\(", lowered):
        issues.append(
            ScadIssue(
                severity="error",
                title="模型代码尝试执行系统命令",
                detail="检测到执行系统命令的调用，已拒绝。",
                suggestion="请重新描述需求。",
            )
        )

    # --- 规模告警 ---------------------------------------------------------
    for pattern, title, detail in RISKY_PATTERNS:
        if pattern.search(cleaned):
            issues.append(
                ScadIssue(
                    severity="warn",
                    title=title,
                    detail=detail,
                    suggestion="生成时间可能较长，若超过 2 分钟可点击取消并简化需求。",
                )
            )

    # --- 必须真的产出几何 -------------------------------------------------
    has_geometry = re.search(
        r"\b(?:cube|sphere|cylinder|polyhedron|circle|square|polygon|text|"
        r"linear_extrude|rotate_extrude|surface|union|difference|intersection|hull|minkowski|"
        r"import)\s*\(",
        lowered,
    )
    if not has_geometry:
        issues.append(
            ScadIssue(
                severity="error",
                title="模型代码里没有任何几何体",
                detail="AI 生成的代码没有创建任何形状，无法生成 3D 模型。",
                suggestion="请更具体地描述你想要的东西，例如「一个直径 60mm 的圆形杯垫」。",
            )
        )

    # --- 元数据 -----------------------------------------------------------
    model_name, summary, params = extract_metadata(source)

    ok = not any(issue.severity == "error" for issue in issues)
    return ScadValidation(ok=ok, issues=issues, model_name=model_name, summary=summary, params=params)


def extract_metadata(source: str) -> tuple[str | None, str | None, dict[str, float]]:
    """从源码注释与参数区提取模型名、设计说明与参数表。

    约定（由系统提示词强制 AI 遵守）：
        // @name 桌面手机支架
        // @summary 一句话说明
        // @param height 高度 mm 100 20 300
    """
    model_name: str | None = None
    summary: str | None = None
    params: dict[str, float] = {}

    for line in source.splitlines():
        stripped = line.strip()
        if not stripped.startswith("//"):
            continue
        body = stripped[2:].strip()
        if body.lower().startswith("@name"):
            model_name = body[5:].strip() or None
        elif body.lower().startswith("@summary"):
            summary = body[8:].strip() or None
        elif body.lower().startswith("@param"):
            parts = body[6:].split()
            if len(parts) >= 2 and IDENT_RE.match(parts[0]):
                try:
                    params[parts[0]] = float(parts[1])
                except ValueError:
                    continue

    # 注释没写参数时，退回到解析顶层的简单数值赋值
    if not params:
        params = extract_top_level_numbers(source)

    return model_name, summary, params


TOP_LEVEL_ASSIGN_RE = re.compile(
    r"^[ \t]*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(-?\d+(?:\.\d+)?)\s*;[ \t]*(?://.*)?$"
)


def extract_top_level_numbers(source: str) -> dict[str, float]:
    """提取「顶层的、纯数字的」变量赋值 —— 这些才是可以安全改写参数的变量。"""
    found: dict[str, float] = {}
    depth = 0
    for raw_line in strip_comments(source).splitlines():
        line = raw_line
        # 只有处于最外层作用域（不在 {} 内）的赋值才可安全覆盖
        if depth == 0:
            match = TOP_LEVEL_ASSIGN_RE.match(line)
            if match:
                name = match.group(1)
                if name.lower() not in RESERVED and not name.startswith("$"):
                    try:
                        found[name] = float(match.group(2))
                    except ValueError:
                        pass
        depth += line.count("{") + line.count("(") + line.count("[")
        depth -= line.count("}") + line.count(")") + line.count("]")
        if depth < 0:
            depth = 0
    return found


def top_level_assignments(source: str) -> set[str]:
    """列出**最外层作用域**里被赋值的变量名（包含 `$` 开头的特殊变量）。

    为什么要单独有这么一个函数：判断「源码里有没有 `$fn`」不能用
    `"$fn" in source` 这种子串检查 —— 注释里提一句、模块内部局部赋值一次、
    甚至字符串里出现，都会让子串检查为真，于是本该注入的 `$fn` 被跳过，
    圆弧面质量静默降级（实测：同一个圆柱，注入后 380 面、跳过注入只有 116 面，
    肉眼可见棱面）。

    这里用与 `apply_params` 同一套「括号/花括号深度」判断，只认真正位于最外层的赋值。

    与 `extract_top_level_numbers` 的区别：那个只收「纯数字赋值」且**排除** `$` 变量
    （`$fn` 不是给用户调的普通参数）；这里要的恰恰是 `$` 变量，
    而且不关心右值是数字还是表达式 —— 作者显式赋值过就算声明了。
    """
    found: set[str] = set()
    # 赋值号两侧不能是 = ! < >，避免把 == / <= / >= 当成赋值
    pattern = re.compile(r"^[ \t]*([A-Za-z_$][A-Za-z0-9_$]*)\s*(?<![=!<>])=(?!=)")
    depth = 0
    for raw_line in strip_comments(source).splitlines():
        if depth == 0:
            match = pattern.match(raw_line)
            if match:
                found.add(match.group(1))
        depth += raw_line.count("{") + raw_line.count("(") + raw_line.count("[")
        depth -= raw_line.count("}") + raw_line.count(")") + raw_line.count("]")
        if depth < 0:
            depth = 0
    return found


def apply_params(source: str, values: dict[str, float]) -> tuple[str, list[str], list[str]]:
    """把用户调整的参数值写回源码顶层的数值赋值。

    只改写「顶层纯数字赋值」，绝不触碰注释、也不触碰表达式赋值 —— 保证不会破坏模型。
    返回 (新源码, 已应用, 被忽略)。
    """
    applied: list[str] = []
    ignored: list[str] = []
    if not values:
        return source, applied, ignored

    lines = source.splitlines(keepends=True)
    depth = 0
    for index, raw_line in enumerate(lines):
        line = raw_line.rstrip("\r\n")
        newline = raw_line[len(line):]
        if depth == 0:
            match = TOP_LEVEL_ASSIGN_RE.match(line)
            if match:
                name = match.group(1)
                if name in values and name.lower() not in RESERVED and not name.startswith("$"):
                    try:
                        number = float(values[name])
                    except (TypeError, ValueError):
                        ignored.append(name)
                        match = None
                    if match is not None:
                        formatted = f"{number:g}"
                        # 必须按**匹配到的位置**替换，不能用 str.replace 去换「值那串字符」。
                        # 参数名里含相同数字时 replace 会先命中名字：
                        #   m3 = 3;  → 设置 m3=5  →  m5 = 3;   （名字被改，值没动）
                        #   d10 = 10; → 设置 d10=12 →  d12 = 10;
                        # 而这类名字在 3D 打印里极其常见（m3/m4 螺丝孔、d10 孔径、wall2）。
                        # 更糟的是它还会把参数报成「已应用」，用户看到成功、模型却没变。
                        lines[index] = (
                            line[: match.start(2)] + formatted + line[match.end(2):] + newline
                        )
                        applied.append(name)
                        # 保持深度统计正确
                        depth += line.count("{") + line.count("(") + line.count("[")
                        depth -= line.count("}") + line.count(")") + line.count("]")
                        if depth < 0:
                            depth = 0
                        continue
        depth += line.count("{") + line.count("(") + line.count("[")
        depth -= line.count("}") + line.count(")") + line.count("]")
        if depth < 0:
            depth = 0

    for name in values:
        if name not in applied and name not in ignored:
            ignored.append(name)

    return "".join(lines), applied, ignored


def strip_code_fences(text: str) -> str:
    """去掉 AI 常见的 ``` 包裹与前言后语，只留下 SCAD 代码本体。"""
    if not text:
        return ""
    fenced = re.search(r"```(?:openscad|scad|javascript|js)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()

    lines = text.splitlines()
    # 丢弃明显的自然语言前后缀
    start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("//") or re.match(
            r"^(?:module|function|\$|[A-Za-z_][A-Za-z0-9_]*\s*=|include|use|union|difference|intersection|"
            r"cube|sphere|cylinder|polyhedron|translate|rotate|scale|mirror|color|for|if|hull|minkowski|"
            r"linear_extrude|rotate_extrude|\}|\{|\[)",
            stripped,
        ):
            start = index
            break
    end = len(lines)
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].strip()
        if stripped.endswith((";", "}", "]", ")")) or stripped.startswith("//"):
            end = index + 1
            break
    return "\n".join(lines[start:end]).strip()


def ensure_no_bom(text: str) -> str:
    """OpenSCAD 2021.01 无法解析带 BOM 的文件（实测：Parser error, line 1）。"""
    if text.startswith("\ufeff"):
        text = text[1:]
    # 中文 Windows 下 AI 偶尔会输出全角符号，做最小纠正
    return text.replace("\u3000", " ").replace("；", ";")
