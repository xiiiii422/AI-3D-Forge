"""AI 3D Forge —— SCAD 源码安全校验。

两道防线：
  1. 静态检查：拒绝能让 OpenSCAD 读写本地文件、联网或生成病态几何的语句。
  2. 规模保护：拒绝会生成百万级面片、把用户机器卡死的设计。

对用户而言，失败信息必须是「人话 + 该怎么办」，而不是语法分析报告。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .parts import catalog as parts_lib

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


def _angle_target(text: str, open_index: int) -> str:
    """取出 `<...>` 里的内容（用于报错信息与零件库白名单比对）。"""
    end = text.find(">", open_index)
    if end == -1:
        return ""
    return text[open_index + 1 : end].strip()


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
    """去掉 // 行注释与 /* */ 块注释，避免注释里的文字触发误判。

    必须**一遍扫过、并且认字符串**。旧实现是「先块注释正则、再行注释正则」两遍，
    两遍都不看字符串，实测两个坑：
      * 字符串里的 `//` 被当成行注释，把同一行后面的**真代码**整段吃掉 ——
        `text("1//2"); import("C:/Windows/win.ini");` 能顺利通过 `validate()`
        （实测 `ok=True`，`use <evil.scad>` 同理）。这是安全边界上的洞：承诺是
        「只允许生成纯几何模型」，而读本地文件的语句只要躲在这样的字符串后面
        就能过去。`text()` 刻字是本产品最常见的输出之一，不是构不出的情形。
      * 行注释里的 `/*` 会被当成块注释开头，一路吞到下一个 `*/`（或落单时什么
        都不做，行为取决于后面有没有闭合）。

    删掉的注释一律**压成一个空格**（跟旧正则一致），但**换行原样保留**：
    调用方按行遍历（`extract_top_level_numbers` / `top_level_assignments` 还要按
    括号深度定层级），`_locate` 报的「第 N 行」也必须跟原文对得上 —— 旧实现把
    跨行块注释压成一个空格，从那以后的行号全部往上错一位。
    为什么是“一个空格”而不是“按注释长度补空格”：`jobs.py` 拿
    `strip_comments(before).strip() == strip_comments(after).strip()` 判「AI 只改了
    注释、代码没动」，补齐空格会让两段不同长度的注释得出不同的空格数，这个
    相等判断就会错。

    字符串**内容**这里不抹（留下原样）——抹字符串是 `strip_strings` 的活，
    多个调用方（jobs / changes / complex_e2e）拿 `strip_comments` 的结果找字面量。
    """
    out: list[str] = []
    total = len(source)
    i = 0
    state = ""  # "" 代码 | "line" 行注释 | "block" 块注释 | "str" 字符串
    while i < total:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < total else ""
        if state == "":
            if ch == "/" and nxt == "/":
                out.append(" ")
                state = "line"
                i += 2
            elif ch == "/" and nxt == "*":
                out.append(" ")
                state = "block"
                i += 2
            elif ch == '"':
                out.append(ch)
                state = "str"
                i += 1
            else:
                out.append(ch)
                i += 1
        elif state == "line":
            # 注释本体丢掉，行尾换行留下（\r 跟着注释一起丢）
            if ch == "\n":
                out.append("\n")
                state = ""
            i += 1
        elif state == "block":
            if ch == "*" and nxt == "/":
                state = ""
                i += 2
            else:
                if ch == "\n":
                    out.append("\n")  # 块注释内部的换行必须留着，行号才对得上
                i += 1
        else:  # state == "str"：内容原样留着，只是不让里面的 // 与 /* 被当成注释
            if ch == '"':
                out.append(ch)
                state = ""
                i += 1
            elif ch == "\\" and nxt and nxt != "\n":
                out.append(ch)
                out.append(nxt)  # 转义后的那个字符不算字符串结束（\" 不是收尾引号）
                i += 2
            elif ch == "\n":
                # OpenSCAD 的字符串不跨行：遇到换行就退回代码态，
                # 否则一个漏写的引号会把后半份代码全当成字符串跳过去。
                out.append("\n")
                state = ""
                i += 1
            else:
                out.append(ch)
                i += 1
    return "".join(out)


def strip_strings(source: str) -> str:
    """去掉字符串字面量内容（保留引号），避免 echo("import") 之类误判。

    必须**不允许跨行**，跟 `strip_comments` 对字符串的假设保持一致（见那里
    的注释：OpenSCAD 的字符串不跨行）。旧实现的字符集 `[^"\\]` 把换行也算
    在内，遇到**没闭合**的引号就会一路吃掉后面的换行和真代码，直到下一个
    引号才停 —— 实测：`x = "abc;` + 下一行 `import("C:/Windows/win.ini");` 能让
    `validate()` 返回 `ok=True`（那个 `import(` 被当成字符串内容抹掉了，根本
    没进 FORBIDDEN_CALLS 的扫描面），与文件开头承诺的「拒绝能让 OpenSCAD
    读写本地文件的语句」不符。AI 少打一个引号是现实会发生的事（本文件
    处理过的其他坑也都是同类：不成对的符号把后面的真代码带偷），修好这一
    类不匹配比事后依赖「反正 OpenSCAD 也会语法报错」更可靠 —— 那道门本来就是
    编译之前的唯一静态拦截，不能让它在语法错之前就先撒谎说安全。
    """
    return re.sub(r'"(?:[^"\\\r\n]|\\.)*"', '""', source)


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

    def _locate(match) -> str:
        """把命中位置翻成「第 N 行：原文片段」。

        为什么必须给：只说「有系统命令/不安全操作」时，AI 修复两轮都删不干净
        （实测 glm 偶发产出这类代码）；给出具体行与片段才能精准删除。
        """
        line_no = cleaned.count("\n", 0, match.start()) + 1
        lines = cleaned.splitlines()
        snippet = lines[line_no - 1].strip()[:120] if lines else ""
        return f"第 {line_no} 行：{snippet}"

    # --- 危险函数 ---------------------------------------------------------
    for name, description in FORBIDDEN_CALLS.items():
        hit = re.search(rf"(?<![A-Za-z0-9_$]){re.escape(name)}\s*\(", lowered)
        if hit:
            issues.append(
                ScadIssue(
                    severity="error",
                    title="模型代码包含不安全的操作",
                    detail=f"检测到 {description}（{_locate(hit)}）。出于安全考虑，本软件只允许生成纯几何模型。",
                    suggestion="请删除该调用及其相关代码，只使用 OpenSCAD 内置几何体重新输出完整代码。",
                )
            )

    # --- 读入外部源文件（include / use）------------------------------------
    # 尖括号写法，圆括号那套匹配不到，必须单独查。
    #
    # 唯一的例外：**本程序自带的固定零件库**（src/aiforge/parts/aiforge_parts.scad）。
    # 为什么值得开这个口子：螺纹这类功能性几何让模型每次现写几乎必错（实测：不封闭、
    # 或者被 CGAL 丢掉整块几何，用户拿到一个拧不上的光滑圆筒）。零件库是我们自己
    # 写、自己编译验证过的文件，不含任何用户数据。
    # 例外必须**精确到文件名**：不允许路径分隔符、不允许 ..、不允许绝对路径，
    # 也不允许大写/空格变体 —— 否则就等于把 include 全放开了。
    for directive in INCLUDE_DIRECTIVE_RE.finditer(cleaned):
        target = _angle_target(cleaned, directive.end() - 1)
        if directive.group(1).lower() == "use" and target == parts_lib.LIBRARY_NAME:
            continue
        issues.append(
            ScadIssue(
                severity="error",
                title="模型代码包含不安全的操作",
                detail=(
                    f"检测到 {directive.group(1)} <{target or '…'}> 指令：它会读入本机的 .scad 源文件，"
                    "属于文件读取操作。出于安全考虑，本软件只允许生成纯几何模型"
                    f"（唯一允许的是自带的固定零件库 use <{parts_lib.LIBRARY_NAME}>）。"
                ),
                suggestion=f"请只引用自带的零件库（use <{parts_lib.LIBRARY_NAME}>），不要引用其它文件。",
            )
        )

    # --- 显式文件读写 -----------------------------------------------------
    # 实测误报：`module shell() {…}` 是掏空壳体的**常用合法模块名**，
    # 旧的 \bshell\s*\( 会把它当成「执行系统命令」拦掉，任何提供商都会中招
    # （glm 给手机支架写 module shell() 就被连修两轮仍终止）。
    # OpenSCAD 本身并不能执行系统命令；真正要拦的是「调用了一个未定义的
    # system/exec/shell」—— 本地已定义的同名 module/function 一律放行。
    defined_names = set(re.findall(r"\b(?:module|function)\s+([a-z_][a-z0-9_]*)", lowered))
    sys_hit = None
    for nm in ("system", "exec", "shell"):
        if nm in defined_names:
            continue
        hit = re.search(rf"\b{nm}\s*\(", lowered)
        if hit:
            sys_hit = hit
            break
    if sys_hit:
        issues.append(
            ScadIssue(
                severity="error",
                title="模型代码尝试执行系统命令",
                detail=f"检测到执行系统命令的调用（{_locate(sys_hit)}），已拒绝。",
                suggestion="请删除该行及其相关调用，只使用 OpenSCAD 内置几何体重新输出完整代码。",
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
    # 固定零件库里的模块也算「产出几何」：一旦调用它们，
    # 真正的几何在库里（我们自己的文件），这里不能误判成"什么都没画"。
    library_modules = "|".join(re.escape(part.module) for part in parts_lib.PARTS)
    has_geometry = re.search(
        r"\b(?:cube|sphere|cylinder|polyhedron|circle|square|polygon|text|"
        r"linear_extrude|rotate_extrude|surface|union|difference|intersection|hull|minkowski|"
        rf"import|{library_modules})\s*\(",
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

    约定（由系统提示词强制 AI 遵守，见 llm.py 的 SCAD_CONVENTIONS；
    `parts/catalog.py::render_source` 生成的也是这个顺序）：
        // @name 桌面手机支架
        // @summary 一句话说明
        // @param <变量名> <默认数值mm> <说明> [最小] [最大]
    例如：// @param height 100 支架高度 40 300
    注意**默认值在说明前面**：下面两个解析函数都取 parts[1] 当数值、
    `extract_param_labels` 取 parts[2] 当标签。这里以前写的是
    `// @param height 高度 mm 100 20 300`（说明在前），跟提示词正好相反 ——
    照着旧注释去「修」解析器，就会把规格名↔参数名那座桥拆掉。
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


def extract_param_labels(source: str) -> dict[str, str]:
    """取出 `// @param 变量名 默认值 说明 …` 里的「变量名 → 说明文字」。

    为什么这是**规格名与源码参数名之间最可靠的一座桥**：
    规格（`height`）与源码参数（`total_h`）是两次不同的 AI 调用产出的，
    命名约定经常对不上。硬去猜缩写（`total_h` 里的 `h` 是不是高度？）
    既容易猜错，也解释不清。而 `@param` 注释里的说明是**生成这份源码的
    那次 AI 自己写的**，用的词表与规格同源（`total_h` → 「总高」），
    归一化后就是规格名本身。

    实测背景：规格写 `height=50`、源码写 `total_h=100` 时，改名桥断裂会让
    一次**完全正确**的改动被同时判成「没改到」和「越权改了 total_h」，
    最终回退成"什么都没改"，而界面还报成功。
    """
    labels: dict[str, str] = {}
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped.startswith("//"):
            continue
        body = stripped[2:].strip()
        if not body.lower().startswith("@param"):
            continue
        parts = body[6:].split()
        if len(parts) < 3 or not IDENT_RE.match(parts[0]):
            continue
        try:
            float(parts[1])
        except ValueError:
            continue
        labels[parts[0]] = parts[2]
    return labels


def is_assigned(source: str, name: str) -> bool:
    """源码里是否存在对 `name` 的赋值（**包括非纯数值的表达式赋值**）。

    用来守住一条底线：当 `height = design_h * 1.5;` 这种表达式赋值存在时，
    不能再把规格里的 `height` 解析成另一个变量（`design_h`）去改写 ——
    改那个变量得到的是别的尺寸（150 × 1.5 = 225），
    而界面会显示「已按你的目标值改动」。宁可放弃兜底，也不交半成品。
    """
    if not name:
        return False
    return re.search(
        rf"(?m)^[ \t]*{re.escape(name)}[ \t]*=", strip_comments(source)
    ) is not None


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
