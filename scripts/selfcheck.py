"""AI 3D Forge —— 后端自检脚本。

面向开发者，验证「不依赖界面」的核心能力：
    python scripts/selfcheck.py

覆盖：
  * 全部模块可导入（发布版打包后最怕的 ModuleNotFoundError）
  * SCAD 安全校验 / 参数提取 / 参数改写
  * Mesh 解析（用现场生成的立方体验证封闭性、体积、包围盒）
  * OpenSCAD 检测与 CLI 冒烟测试
  * 环境检测报告
  * 项目读写（原子写、历史归档、导出）
  * API Key 掩码与存储后端
  * HTTP 接口（用 TestClient 跑一遍关键路由）

退出码 0 表示全部通过；非 0 表示有失败项（会打印明细）。
"""

from __future__ import annotations

import console_utf8  # noqa: F401,E402 —— 必须最先导入：GBK 控制台下 print 中文/符号会弄崩脚本（见 src/aiforge/stdio.py）
import os
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for candidate in (str(SRC), str(ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

# 测试期间把用户数据重定向到 D 盘构建目录，绝不污染真实用户目录
os.environ.setdefault("AIFORGE_DATA_DIR", str(ROOT / "build" / "work" / "testdata"))
os.environ.setdefault("AIFORGE_LOCAL_DIR", str(ROOT / "build" / "work" / "testlocal"))
os.environ.setdefault("AIFORGE_PROJECTS_DIR", str(ROOT / "build" / "work" / "testprojects"))

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
SKIPPED: list[tuple[str, str]] = []
OFFLINE = False

# 打印一律走 _SAFE_PRINT（为什么要走，见下面 check() 里的说明）。
# 兜底模块没加载成功时退回原生 print：至少不能因为找一个辅助函数再叠一个 AttributeError。
_SAFE_PRINT = getattr(console_utf8, "safe_print", print)

# `--offline` 时跳过的检查：需要真实 OpenSCAD 二进制、Windows 注册表/图形界面，
# 或者会访问网络。CI（ubuntu / 无 OpenSCAD 的 windows）用这个模式。
# 注意：跳过的项会**单独计数并逐条打印原因**，绝不混进"通过"。
# 键名必须能在下面注册的检查里找到 —— 「文档里的自检项数与真源一致」会当场查死键。
# （这里曾有 `"快捷方式解析"`：`shortcuts.py` 连同两个没人调的端点被删了，检查项
#  也没了，但这一行还留着。它在 --offline 下永远匹配不到任何东西，却会骗过任何
#  拿 `len(OFFLINE_SKIPS)` 算「跳了几项」的人 —— 文档里那个数就是这么错的。）
OFFLINE_SKIPS: dict[str, str] = {
    "WebView2 运行时检测": "需要 Windows 注册表与系统组件",
    "应用图标资源": "图标由 build.ps1 生成，离线环境没有构建产物",
    "打包依赖完整性": "需要构建期依赖（PyInstaller 等）",
    "OpenSCAD 自动安装": "需要下载安装 OpenSCAD（网络 + 约 21MB）",
    "OpenSCAD 检测与 CLI": "需要本机已安装 OpenSCAD",
    "STL 网格分析": "需要 OpenSCAD 编译立方体后再分析",
    "SCAD → STL 编译": "需要 OpenSCAD 把 SCAD 编译成 STL",
    "安装失败不破坏已有环境": "需要模拟一次真实的 OpenSCAD 安装",
    "子进程不变量守护": "依赖 Windows 作业对象（非 Windows 上语义不同）",
    "守护存活双向保证": "依赖 Windows 作业对象（要验的是“工人被内核带走”）",
    "几何完整性自动修复": "需要真实几何数据（由 OpenSCAD 产出）",
    "固定零件库可编译": "需要对每个零件真实编译（依赖 OpenSCAD）",
    "孔径射线实测": "需要 OpenSCAD 编译带孔/实心对照模型",
    "模型外观复核": "需要渲染模型（依赖 OpenSCAD）",
    "接口响应形状": "部分断言依赖已构建的前端产物 webui/dist",
}


class SkipSelfcheck(Exception):
    """运行时环境不够，这一项**现在验不了**（不是失败，也不算通过）。

    为什么需要它（而不是只靠 `OFFLINE_SKIPS` 那份名单）：静态名单按**名字**跳，
    它不知道这台机器到底有没有 OpenSCAD、有没有构建产物。结果是同一项在开发机上跑、
    在 CI / 新 clone 上直接挂（本轮把镜像仓库克隆到项目外跑 `--offline`，实测四条红：
    需要真编译的、要 `build/` 里那个生成图标的、依赖 `build/venv` 的预算守卫、
    以及只能靠静态名单解决的那条路由检查）。而 `OFFLINE_SKIPS` 仍按名字服务于
    「需要网络/会装机」这类不能跑在 CI 里的项 —— 两者职责不同。
    跳过必须单独计数并打印原因，绝不混进「通过」（跟 `--offline` 同一个红线）。
    """


def _openscad_ready() -> bool:
    """这台机器到底能不能真编译（新 clone / CI 上通常不能）。

    不能拿 `--offline` 名单代拾：同一份代码在装了 OpenSCAD 的机器上应当硬断言，
    在没装的机器上应当**说清楚验不了**；静态名单只能二选一，就会要么漏覆盖、要么假红。
    """
    try:
        from aiforge import openscad as _osc

        return _osc.resolve_executable() is not None
    except Exception:  # noqa: BLE001 - 探测本身不能把自检带崩
        return False


def check(name: str, fn) -> None:
    if OFFLINE and name in OFFLINE_SKIPS:
        reason = OFFLINE_SKIPS[name]
        SKIPPED.append((name, reason))
        _SAFE_PRINT(f"  [SKIP] {name} —— {reason}（CI 离线模式；发布前请跑完整版）")
        return
    try:
        result = fn()
        PASSED.append(f"{name}" + (f" —— {result}" if result else ""))
        _SAFE_PRINT(f"  [OK]   {name}" + (f" —— {result}" if result else ""))
    except SkipSelfcheck as exc:
        # 放在通用 `except Exception` **前面**：否则“验不了”会被当成失败（假报警），
        # 更会被当成“红=得修”而逼人去把环境搭出来。
        SKIPPED.append((name, str(exc)))
        _SAFE_PRINT(f"  [SKIP] {name} —— {exc}（当前机器环境验不了；不是失败也不是通过）")
    except Exception as exc:  # noqa: BLE001
        FAILED.append((name, f"{type(exc).__name__}: {exc}"))
        # 为什么不能直接用 print：失败分支上的那行字里带异常消息，而那正是最容易
        # 出现打不出字符的地方（实测：GBK 控制台下这里报 UnicodeEncodeError，
        # 整个自检中途死掉，连「哪几项失败 / 退出码」都看不到了）。
        _SAFE_PRINT(f"  [FAIL] {name} —— {type(exc).__name__}: {exc}")
        if os.environ.get("AIFORGE_SELFCHECK_TRACE") == "1":
            traceback.print_exc()


def section(title: str) -> None:
    _SAFE_PRINT(f"\n=== {title} ===")


# --------------------------------------------------------------------------
def t_imports() -> str:
    import importlib

    modules = [
        "aiforge",
        "aiforge.paths",
        "aiforge.version",
        "aiforge.stdio",
        "aiforge.config",
        "aiforge.logging_setup",
        "aiforge.single_instance",
        "aiforge.envcheck",
        "aiforge.openscad",
        "aiforge.scad",
        "aiforge.mesh",
        "aiforge.compiler",
        "aiforge.llm",
        "aiforge.jobs",
        "aiforge.projects",
        "aiforge.server",
        "aiforge.app",
        "aiforge.api.env",
        "aiforge.api.openscad_routes",
        "aiforge.api.jobs_routes",
        "aiforge.api.projects_routes",
        "aiforge.api.settings_routes",
        "aiforge.api.app_routes",
        "aiforge.api.errors",
    ]
    for name in modules:
        importlib.import_module(name)
    return f"{len(modules)} 个模块"


def t_scad_validation() -> str:
    from aiforge import scad

    good = scad.validate("// @name 支架\n// @summary 说明\nheight = 100;\ncube([20,20,height]);")
    assert good.ok, good.issues
    assert good.model_name == "支架", good.model_name
    assert good.params.get("height") == 100.0, good.params

    bad = scad.validate('import("evil.stl"); cube([1,1,1]);')
    assert not bad.ok, "应当拦截 import()"

    risky = scad.validate("cube([1,1,1]); sphere(r=5, $fn=20000);")
    assert risky.ok, "规模问题应为告警而非拦截"
    assert any(i.severity == "warn" for i in risky.issues), risky.issues

    empty = scad.validate("// 只有注释")
    assert not empty.ok, "没有几何体应当判定失败"

    fenced = scad.strip_code_fences("这是代码：\n```openscad\ncube([2,2,2]);\n```\n希望有帮助！")
    assert fenced == "cube([2,2,2]);", repr(fenced)

    bom = scad.ensure_no_bom("\ufeffcube([1,1,1]);")
    assert not bom.startswith("\ufeff"), "必须剥离 BOM（OpenSCAD 2021.01 会解析失败）"

    # ---- include / use 也是「读本机文件」，必须一起拦 ----
    # 实测踩到的真实漏洞：它们用**尖括号**而不是圆括号，
    # 所以「函数名 + (」那套检查完全抓不到：
    #     include <C:/Windows/win.ini>;   → 修复前放行
    #     use </etc/passwd>;              → 修复前放行
    # 这与本文件开头承诺的「拒绝能让 OpenSCAD 读写本地文件的语句」不符。
    for evil in (
        "include <C:/Windows/win.ini>;\ncube([1,1,1]);",
        "use </etc/passwd>;\ncube([1,1,1]);",
        "include <../../secret.scad>;\ncube([1,1,1]);",
        "include<no_space.scad>;\ncube([1,1,1]);",
        "USE <UP.SCAD>;\ncube([1,1,1]);",
    ):
        result = scad.validate(evil)
        assert not result.ok, f"应当拦截读文件的指令：{evil!r}"

    # 反过来不能被误伤：变量名/注释/字符串里出现这些词是正常代码
    for fine in (
        "include_margin = 5;\ncube([include_margin, 10, 10]);",
        "use_radius = 3;\ncylinder(r=use_radius, h=10);",
        "// use <MCAD/foo.scad> 这种写法本软件不支持\ncube([1,1,1]);",
        'text("use <x>");',
        "module my_use_thing() { cube(1); }\nmy_use_thing();",
    ):
        result = scad.validate(fine)
        assert result.ok, f"不该误拦正常代码：{fine!r} -> {result.issues}"

    # 系统命令/危险函数的报错必须带「行号+片段」，修复提示才能精准删除
    bad = "cube([1,1,1]);\nsystem(\"rm -rf /\");\n"
    result = scad.validate(bad)
    assert not result.ok, "系统命令必须被拦截"
    detail = "；".join(i.detail for i in result.issues if i.severity == "error")
    assert "第 2 行" in detail and "system" in detail, detail
    # 但本地已定义的同名 module（如掏空壳常用的 shell）是合法代码，不能误拦
    fine_shell = "module shell() { difference() { cube(10); cube(8); } }\nshell();\n"
    result = scad.validate(fine_shell)
    assert result.ok, f"module shell() 不该被误拦 -> {result.issues}"

    # ---- 字符串里的 // 不是注释：不许它把同一行后面的真代码吃掉 ----
    # 实测（修复前）：`strip_comments` 是「先块注释正则、再行注释正则」两遍，
    # 两遍都不看字符串，于是下面两条能**顺利通过安全校验**（ok=True）：
    #     text("1//2"); import("C:/Windows/win.ini");
    #     text("a//b"); use <evil.scad>;
    # 跟本文件开头承诺的「拒绝能让 OpenSCAD 读写本地文件的语句」不符；而
    # `text()` 刻字是本产品最常见的输出之一，不是构不出的情形。
    for sneaky in (
        'text("1//2"); import("C:/Windows/win.ini");\ncube([1,1,1]);',
        'text("a//b"); use <evil.scad>;\ncube([1,1,1]);',
        'text("/*"); import("evil.stl"); /* 尾\ncube([1,1,1]);',
    ):
        result = scad.validate(sneaky)
        assert not result.ok, f"字符串里的 // 把同行代码吃掉了，必须拦：{sneaky!r}"

    # ---- 未闭合的引号也不许把**下一行**的真代码当成字符串内容抹掉 ----
    # 与上面那个坑是对称的：`strip_comments` 修好了「字符串里的注释符」，但
    # `strip_strings` 自己也有一个同类问题 —— 它的字符集以前把换行也算进字符串内容，
    # 遇到没闭合的引号就一路跳行吹走后面的代码。实测（修复前）：
    #     x = "abc;                          ← 少打一个闭合引号
    #     import("C:/Windows/win.ini");       ← 被当成上一段字符串的内容抹掉
    #     y = "def";                          ← 真正的闭合引号在这里才出现
    # 整份代码能顺利过 `validate()`（ok=True），与「拒绝读写本地文件」的承诺不符。
    unterminated_quote = 'x = "abc;\nimport("C:/Windows/win.ini");\ny = "def";\ncube([1,1,1]);'
    result = scad.validate(unterminated_quote)
    assert not result.ok, f"未闭合引号不该把下一行的 import 当成字符串内容放过去：{result.issues}"
    detail = "；".join(i.detail for i in result.issues if i.severity == "error")
    assert "第 2 行" in detail, f"报错行号必须跟原文对齐：{detail}"
    # 反向：闭合正常的字符串不能被误伤（不然 echo("import") 这类都会被拦）
    for fine in (
        'echo("import");\ncube([1,1,1]);',
        'text("use <x>", size=5);\ncube([1,1,1]);',
        'x = "a\\"b";\ncube([1,1,1]);',  # 转义引号不能把闭合引号误判成开头引号
    ):
        result = scad.validate(fine)
        assert result.ok, f"不该误拦正常代码：{fine!r} -> {result.issues}"

    # 反过来：注释里出现不成对的引号 / 行注释里写 /*，都不该把后面的代码带倒。
    # （旧实现同样在这里错：先把整行删掉再抹字符串，就不会遇到不成对引号；
    # 一旦顺序反过来而不做成一遍扫描，这些就是新产生的误拦。）
    for fine in (
        '// 不要用 "import(" 这种写法\ncube([1,1,1]);',
        "// 见 /* 的说明\ncube([1,1,1]);",
        'text("AI 3D Forge", size=5);\ncube([1,1,1]);',
        'text("1//2");\ncube([1,1,1]);',
    ):
        result = scad.validate(fine)
        assert result.ok, f"不该误拦正常代码：{fine!r} -> {result.issues}"

    # 删注释必须保住行号：跨行块注释不能把后面的代码「往上摊」，
    # 否则报给 AI 的「第 N 行」指到别的行，修复两轮都删不干净。
    blocky = '/* 说明\n   第二行 */\nsystem("dir");\n'
    assert len(scad.strip_comments(blocky).splitlines()) == len(blocky.splitlines()), (
        "strip_comments 改变了行数 —— 按行遍历的调用方（extract_top_level_numbers / "
        "top_level_assignments）会跟 _locate 报的行号一起错位"
    )
    result = scad.validate(blocky)
    detail = "；".join(i.detail for i in result.issues if i.severity == "error")
    assert not result.ok and "第 3 行" in detail, f"行号必须跟原文对齐：{detail}"

    # 注释压成**一个空格**而不是按长度补空格：jobs.py 拿
    # `strip_comments(before).strip() == strip_comments(after).strip()` 判「AI 只改了
    # 注释、代码没动」；按长度补空格会让两段不同长度的注释得出不同空格数，
    # 这个相等判断就错 —— 只改注释的返工会被当成真改动。
    short = 'cube(10); // 短\nheight = 5;\n'
    long_ = 'cube(10); // 长得多的说明了又长一点\nheight = 5;\n'
    assert scad.strip_comments(short).strip() == scad.strip_comments(long_).strip(), (
        "同一行代码 + 不同长度的注释，删注释后应当逐字相等："
        f"{scad.strip_comments(short)!r} vs {scad.strip_comments(long_)!r}"
    )

    return (
        "安全拦截（含 include/use 读文件）/ 报错带行号片段 / 合法 module shell 不误拦 / "
        "字符串里的 // 与 /* 不吃同行代码 / 未闭合引号不吹走下一行真代码 / 注释里的不成对引号不误拦 / "
        "删注释保行号 均正确"
    )


def t_scad_params() -> str:
    from aiforge import scad

    source = "// @param height 100 高度 40 300\nwidth = 60;\nheight = 100; // 高度\nmodule body() { cube([width, 20, height]); }\nbody();\n"
    updated, applied, ignored = scad.apply_params(source, {"height": 150})
    assert applied == ["height"], (applied, ignored)
    assert "height = 150;" in updated, updated
    assert "width = 60;" in updated, "不应改动未指定的参数"

    # 表达式赋值不能被改写（否则会破坏模型）
    expr = "h = 2 + 3;\nw = 10;\n"
    updated2, applied2, _ = scad.apply_params(expr, {"h": 99, "w": 20})
    assert "h = 2 + 3;" in updated2, "表达式赋值必须保持原样"
    assert "w = 20;" in updated2, updated2
    assert applied2 == ["w"], applied2

    # 模块内部（有缩进/在 {} 里）的赋值不应被视为顶层参数
    nested = "module a() {\n  size = 5;\n  cube([size,size,size]);\n}\na();\n"
    found = scad.extract_top_level_numbers(nested)
    assert "size" not in found, found

    # ---- 参数名里含数字时，绝不能改到名字上 ----
    # 实测踩到的真实 Bug：旧实现用「把值那串字符替换掉第一次出现的位置」来改写，
    # 于是参数名里含相同数字时会先命中名字：
    #     m3 = 3;   设置 m3=5    →  m5 = 3;    （名字被改，值没动）
    #     d10 = 10; 设置 d10=12  →  d12 = 10;
    # 而且它还把参数报成「已应用」—— 用户看到成功、模型却没变。
    # m3/m4（螺丝孔）、d10（孔径）、wall2 这类名字在 3D 打印里非常常见。
    digit_cases = [
        ("m3 = 3;\n", "m3", 5.0),
        ("h100 = 100;\n", "h100", 200.0),
        ("wall2 = 2;\n", "wall2", 3.0),
        ("d10 = 10;  // 孔径\n", "d10", 12.0),
        ("m4 = 4;\ndepth = 8;\n", "m4", 6.5),
    ]
    for source, name, value in digit_cases:
        before = set(scad.extract_top_level_numbers(source))
        updated, applied_here, _ = scad.apply_params(source, {name: value})
        assert applied_here == [name], (source, applied_here)
        # 关键：变量名必须原样保留，值必须真的写进去
        assert f"{name} = " in updated, f"变量名被改坏了：{source!r} → {updated!r}"
        reparsed = scad.extract_top_level_numbers(updated)
        assert reparsed.get(name) == value, (
            f"{name} 没有真正生效：{source!r} → {updated!r}（解析回来 {reparsed}）"
        )
        # 变量集合必须与改写前完全一致：改名会凭空多出一个变量、少掉原来那个
        assert set(reparsed) == before, (
            f"改写改变了变量集合（说明变量名被动了）：{before} → {set(reparsed)}"
        )

    # 值里带负号/小数也要按位置替换，不能碰名字
    neg, neg_applied, _ = scad.apply_params("d2 = 2;\n", {"d2": -1.5})
    assert neg_applied == ["d2"] and "d2 = -1.5;" in neg, neg
    return "顶层参数改写安全（含参数名含数字的边界情况）"


def t_mesh_analysis() -> str:
    from aiforge import compiler, mesh

    work = ROOT / "build" / "work" / "meshtest"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    # 用 OpenSCAD 真实生成一个 10mm 立方体，再把它转写成二进制 STL ——
    # 这样夹具的三角面绕序一定自洽（手工拼 12 个面极容易把某个面写反，
    # 而绕序错误会让散度定理算出错误的体积，那是夹具的错、不是分析器的错）。
    real = compiler.compile_to_stl("cube([10,10,10], center=false);", work, "ref")
    assert real.ok, real.message
    points, _ = mesh.read_triangles(real.stl_path)
    triangles = [tuple(points[i : i + 3]) for i in range(0, len(points), 3)]
    assert len(triangles) == 12, f"立方体应有 12 个三角面，实际 {len(triangles)}"

    import struct

    def write_binary(path: Path, faces: list[tuple]) -> None:
        with path.open("wb") as handle:
            handle.write(b"solid selftest\n".ljust(80, b"\0"))
            handle.write(struct.pack("<I", len(faces)))
            for face in faces:
                handle.write(struct.pack("<12f", 0, 0, 0, *face[0], *face[1], *face[2]))
                handle.write(struct.pack("<H", 0))

    binary_path = work / "cube_binary.stl"
    write_binary(binary_path, triangles)

    metrics = mesh.analyze(binary_path)
    assert not metrics.is_ascii, "该文件应被识别为二进制 STL"
    assert metrics.triangles == 12, metrics.triangles
    assert metrics.watertight, f"立方体应当是封闭实体（开放边 {metrics.boundary_edges}）"
    assert abs(metrics.volume_mm3 - 1000.0) < 0.5, f"体积应为 1000mm³，实际 {metrics.volume_mm3}"
    assert abs(metrics.bbox[0] - 10) < 1e-6 and abs(metrics.bbox[2] - 10) < 1e-6, metrics.bbox
    assert metrics.sits_on_plate, "底面在 z=0 应判定为贴平台"
    assert abs(metrics.surface_area_mm2 - 600.0) < 0.5, f"表面积应为 600mm²，实际 {metrics.surface_area_mm2}"

    # 去掉一个面 → 必须被判定为「非封闭」，并报出开放边数量
    write_binary(binary_path, triangles[:-1])
    broken = mesh.analyze(binary_path)
    assert not broken.watertight, "缺面的网格不应判定为封闭"
    assert broken.boundary_edges > 0, broken.boundary_edges
    assert any("封闭" in issue for issue in broken.issues), broken.issues

    # ---- 散件检测：一个成品被做成互不相连的几个零件 ----
    # 这是用户实测踩到的坑（「桌面垃圾桶」被做成「夹口 + 普通垃圾桶」两件），
    # 而包围盒、体积、封闭性全都正常，只有数连通分量才看得出来。
    write_binary(binary_path, triangles)
    single = mesh.analyze(binary_path)
    assert single.solid_parts == 1, f"单个立方体应数出 1 个零件，实际 {single.solid_parts}"
    assert single.stray_parts == 0, single.stray_parts

    # 两个相距很远、互不相连的立方体 → 必须数出 2 个零件
    far = [
        tuple((x + 50.0, y, z) for (x, y, z) in face) for face in triangles
    ]
    write_binary(binary_path, triangles + far)
    two = mesh.analyze(binary_path)
    assert two.solid_parts == 2, f"两个分离立方体应数出 2 个零件，实际 {two.solid_parts}"
    assert two.stray_parts == 1, two.stray_parts
    assert any("互不相连" in issue for issue in two.issues), two.issues
    assert "实体零件数：2" in mesh.summarize_for_ai(two, "two.stl")

    # 紧贴但不相交（相差 0.5mm）仍然算两个零件 —— 这正是 AI 最常犯的错：
    # 「摆得近」不等于「连成一体」
    near = [
        tuple((x, y + 10.5, z) for (x, y, z) in face) for face in triangles
    ]
    write_binary(binary_path, triangles + near)
    adjacent = mesh.analyze(binary_path)
    assert adjacent.solid_parts == 2, (
        f"仅仅贴近并不相交，仍应算 2 个零件，实际 {adjacent.solid_parts}"
    )

    # ASCII STL 也必须能解析
    write_binary(binary_path, triangles)
    ascii_path = work / "cube_ascii.stl"
    lines = ["solid cube"]
    for face in triangles:
        lines.append("facet normal 0 0 0")
        lines.append("  outer loop")
        for vertex in face:
            lines.append(f"    vertex {vertex[0]} {vertex[1]} {vertex[2]}")
        lines.append("  endloop")
        lines.append("endfacet")
    lines.append("endsolid cube")
    ascii_path.write_text("\n".join(lines), encoding="utf-8")
    ascii_metrics = mesh.analyze(ascii_path)
    assert ascii_metrics.is_ascii, "该文件应被识别为 ASCII STL"
    assert abs(ascii_metrics.volume_mm3 - 1000.0) < 0.5, ascii_metrics.volume_mm3

    # ---- 密封内腔不能被算成「第二个零件」 ----
    # 密封空心件的曲面天生是两块（外壳面 + 内腔面），它们在拓扑上确实不相连，
    # 但属于同一个实体。光数连通分量会把这种完全正常的模型报成「2 个零件」，
    # 实测确认过这个误报。判别依据是面朝向：内腔面的有向体积为负。
    sealed = compiler.compile_to_stl(
        "difference(){ cube([60,60,40], center=true); cube([54,54,34], center=true); }",
        work,
        "sealed",
    )
    assert sealed.ok, sealed.message
    sealed_metrics = mesh.analyze(sealed.stl_path)
    assert sealed_metrics.solid_parts == 1, (
        f"密封空心件是 1 个实体（内腔面不是零件），实际 {sealed_metrics.solid_parts}"
    )
    assert sealed_metrics.stray_parts == 0, sealed_metrics.stray_parts
    assert not any("互不相连" in issue for issue in sealed_metrics.issues), sealed_metrics.issues

    # ---- 腔体孤岛：整件连通，但特征长进了腔体内部 ----
    # 实测缺陷：桌面垃圾桶的「外壁加强筋」用「固定半径 + rotate 转一圈」摆放，
    # 矩形桶身的壁面到中轴距离随角度变化，于是 6 根筋里有 4 根飘进桶内腔。
    # 这类模型仍然只有一个连通实体，零件数检查完全看不到，必须靠截面判据。
    shell = (
        "module blank(){ hull(){ linear_extrude(0.01) square([60,90],center=true);"
        " translate([0,0,118]) linear_extrude(0.01) square([100,150],center=true); } }"
        "module cavity(){ translate([0,0,3]) hull(){ linear_extrude(0.01)"
        " square([54,84],center=true); translate([0,0,112]) linear_extrude(0.01)"
        " square([94,144],center=true); } }"
        "module ribs(){ for(a=[0:90:270]) rotate([0,0,a]) translate([26,0,0]) hull(){"
        " cube([6,4,0.01]); translate([20,0,118]) cube([6,4,0.01]); } }"
    )
    # 正确顺序：先实心外坯、最后掏腔 → 穿腔的部分被自动切掉，不该报孤岛
    good = compiler.compile_to_stl(
        shell + "difference(){ union(){ blank(); ribs(); } cavity(); }", work, "good"
    )
    assert good.ok, good.message
    good_metrics = mesh.analyze(good.stl_path)
    assert good_metrics.solid_parts == 1, good_metrics.solid_parts
    assert not good_metrics.cavity_islands, (
        f"先实心后掏腔不该有腔体孤岛，实际 {good_metrics.cavity_islands}"
    )
    # 错误顺序：先掏腔、再 union 外壁特征 → 特征穿进腔体，必须报出来
    bad = compiler.compile_to_stl(
        shell + "union(){ difference(){ blank(); cavity(); } ribs(); }", work, "bad"
    )
    assert bad.ok, bad.message
    bad_metrics = mesh.analyze(bad.stl_path)
    assert bad_metrics.solid_parts == 1, (
        f"穿腔模型仍是 1 个连通实体（这正是零件数检查看不见它的原因），实际 {bad_metrics.solid_parts}"
    )
    assert bad_metrics.cavity_islands, "先掏腔后加特征会让筋穿进腔体，必须检出孤岛"
    assert any("内腔范围内" in issue for issue in bad_metrics.issues), bad_metrics.issues
    assert "腔体孤岛检查" in mesh.summarize_for_ai(bad_metrics, "bad.stl")
    assert "截面未发现被空腔包围的悬空材料" in mesh.summarize_for_ai(
        good_metrics, "good.stl"
    )
    # 腔体里真的漂着一块（与任何东西都不相连）也要能检出
    floating = compiler.compile_to_stl(
        "difference(){ cube([60,60,40],center=true); cube([54,54,34],center=true); }"
        " translate([0,0,-2]) cylinder(h=12, r=5, center=true, $fn=48);",
        work,
        "floating",
    )
    assert floating.ok, floating.message
    floating_metrics = mesh.analyze(floating.stl_path)
    assert floating_metrics.solid_parts == 2, floating_metrics.solid_parts
    assert floating_metrics.cavity_islands, "腔内漂着的材料块必须被检出"

    # ---- 壁厚必须用射线法实测，不能用包围盒最小边代替 ----
    # 这是修掉的一个真实缺陷：旧版把包围盒最小边当壁厚，
    # 对**空心容器完全失效** —— 实测的桌面垃圾桶 110×186×110mm、壁厚 2.4mm，
    # 包围盒最小边 110mm，于是壁做得多薄都不会报警；
    # 而容器恰好是这个软件最主要的用途。
    # 下面两个模型**包围盒最小边完全相同**（都是 100mm），只有壁厚不同（3mm vs 0.8mm），
    # 旧版对这两个给出的结论一模一样（都不报警）。新实现必须区分开。
    shell = (
        "hull(){ linear_extrude(0.01) square([60,90],center=true);"
        " translate([0,0,110]) linear_extrude(0.01) square([100,150],center=true); }"
    )

    def cavity(inset: float, height: float) -> str:
        return (
            f"translate([0,0,{inset}]) hull(){{ linear_extrude(0.01)"
            f" square([{60 - 2 * inset},{90 - 2 * inset}],center=true);"
            f" translate([0,0,{height}]) linear_extrude(0.01)"
            f" square([{100 - 2 * inset},{150 - 2 * inset}],center=true); }}"
        )

    thick = compiler.compile_to_stl(
        f"difference(){{ {shell} {cavity(3.0, 104.0)} }}", work, "wall_thick"
    )
    assert thick.ok, thick.message
    thick_metrics = mesh.analyze(thick.stl_path)
    thin = compiler.compile_to_stl(
        f"difference(){{ {shell} {cavity(0.8, 108.4)} }}", work, "wall_thin"
    )
    assert thin.ok, thin.message
    thin_metrics = mesh.analyze(thin.stl_path)

    assert thick_metrics.wall is not None and thin_metrics.wall is not None, (
        "空心件的壁厚必须被测出来（这正是旧实现测不到的场景）"
    )
    assert abs(min(thick_metrics.bbox) - min(thin_metrics.bbox)) < 0.01, (
        "两个用例的包围盒必须几乎相同，否则这条断言证明不了「包围盒区分不出来」"
    )
    assert thick_metrics.wall.p05_mm > 2.5, thick_metrics.wall.to_dict()
    assert thin_metrics.wall.p05_mm < 1.1, thin_metrics.wall.to_dict()
    # 3mm 壁在 1.2mm 门槛之上、0.8mm 壁在门槛之下：结论必须相反
    assert not thick_metrics.too_thin_wall, thick_metrics.issues
    assert thin_metrics.too_thin_wall, thin_metrics.issues
    assert any("实测壁厚" in issue for issue in thin_metrics.issues), thin_metrics.issues
    # 报警必须带上具体坐标，否则用户/AI 不知道该加厚哪里
    assert thin_metrics.wall.thinnest, "必须给出最薄处坐标"
    point, value = thin_metrics.wall.thinnest[0]
    assert len(point) == 3 and value < 1.1, thin_metrics.wall.thinnest[0]
    assert "壁厚（射线法实测）" in mesh.summarize_for_ai(thin_metrics, "t.stl")

    # 实心体的「厚度」就是它自己那一维，不能因为「没有薄壁」而测不出来
    solid = compiler.compile_to_stl("cube([40,40,40], center=true);", work, "wall_solid")
    assert solid.ok, solid.message
    solid_metrics = mesh.analyze(solid.stl_path)
    assert solid_metrics.wall is not None
    assert abs(solid_metrics.wall.median_mm - 40.0) < 1.0, solid_metrics.wall.to_dict()

    # 回落默认值必须与「2 × 喷嘴」这条规则自洽。
    # 旧版这里写 4.0，是规则值（0.8）的 5 倍，会把 1.2mm 的薄片判成
    # 「连一层走线都放不下」—— 1.2mm 在 0.4mm 喷嘴上明明是三层。
    defaults = mesh.AnalysisLimits()
    assert abs(defaults.rough_min_dim_mm - 2.0 * defaults.nozzle_mm) < 1e-6, (
        f"回落默认的最小可成型尺寸 {defaults.rough_min_dim_mm} 与 2×喷嘴"
        f"（{2.0 * defaults.nozzle_mm}）不一致"
    )
    assert defaults.rough_min_dim_mm <= defaults.min_wall_mm, (
        "最小外形尺寸必须比壁厚门槛宽松，否则壁厚检查永远被它盖住"
    )

    # ---- 拓扑判定不能在「一定封闭」的模型上误报 ----
    # 为什么这条必须有：开放边/非流形边会直接变成用户看到的警告，
    # 而且修复环节的采纳条件里有「封闭性不变差」这一条 ——
    # 一旦封闭性判定误报，修复就永远无法被采纳，用户只能一直拿着最差的那版。
    # 背景：OpenSCAD 的 CSG 输出里本该重合的顶点可能相差 1~3µm，
    # 而边统计按 1e-5mm 取整去重，所以「差一点点」最容易被判成不封闭。
    closed_cases = {
        "立方体": "cube([20,20,20], center=true);",
        "球体": "sphere(r=12, $fn=64);",
        "两立方体布尔并": (
            "union(){ cube([20,20,20],center=true);"
            " translate([10,0,0]) cube([20,20,20],center=true); }"
        ),
        "布尔差(挖孔)": (
            "difference(){ cube([20,20,20],center=true);"
            " translate([0,0,3]) cylinder(h=30,r=5,center=true,$fn=48); }"
        ),
        "hull 渐变体": (
            "hull(){ linear_extrude(0.01) square([30,30],center=true);"
            " translate([0,0,25]) linear_extrude(0.01) square([16,16],center=true); }"
        ),
        "圆角板(offset)": "linear_extrude(height=8) offset(r=3) square([30,20], center=true);",
        "薄壁壳": (
            "difference(){ cube([40,40,30],center=true);"
            " translate([0,0,3]) cube([36,36,40],center=true); }"
        ),
    }
    for label, code in closed_cases.items():
        compiled = compiler.compile_to_stl(code, work, f"closed_{abs(hash(label)) % 10**8}")
        assert compiled.ok, f"{label} 编译失败"
        topology = mesh.analyze(compiled.stl_path)
        assert topology.watertight, (
            f"{label} 在数学上完全封闭，却被判成不封闭 —— 这是误报。"
            f"开放边 {topology.boundary_edges}、非流形边 {topology.non_manifold_edges}"
        )
        assert topology.boundary_edges == 0 and topology.non_manifold_edges == 0, (
            f"{label} 的边统计不该有非零项："
            f"开放边 {topology.boundary_edges}、非流形边 {topology.non_manifold_edges}"
        )

    # ---- 支撑与耗材用量估算 ----
    # 用户要的是「这个件要花多少料」。这里钉住三件事：
    #   1) 不需要支撑的形状不能凭空报出支撑；
    #   2) 悬空结构必须报出支撑，且量级合理（棱柱估算，是上界不是精确值）；
    #   3) 用量必须是个**区间**而不是假装精确的单一数字，
    #      且下限 ≤ 上限、线材长度与克数自洽。
    flat = compiler.compile_to_stl("cube([40,40,40], center=true);", work, "usage_flat")
    assert flat.ok, flat.message
    flat_metrics = mesh.analyze(flat.stl_path)
    assert flat_metrics.support is not None
    assert flat_metrics.support.support_volume_mm3 <= 0.01, (
        f"立方体坐落在平台上，不该有支撑，实际 {flat_metrics.support.support_volume_mm3}"
    )

    # 悬空的球：下面全是空的，必须报出大量支撑
    overhang = compiler.compile_to_stl(
        "cylinder(h=3, r=4, $fn=48); translate([0,0,40]) sphere(r=18, $fn=64);",
        work,
        "usage_overhang",
    )
    assert overhang.ok, overhang.message
    overhang_metrics = mesh.analyze(overhang.stl_path)
    assert overhang_metrics.support is not None
    assert overhang_metrics.support.support_volume_mm3 > 1000, (
        f"悬空的球必须报出支撑，实际 {overhang_metrics.support.support_volume_mm3}"
    )
    assert overhang_metrics.support.ratio_pct > 20, overhang_metrics.support.to_dict()
    # 支撑估算必须是「上界」而不是离谱值：不可能比整个包围盒还大
    bbox_volume = (
        overhang_metrics.bbox[0] * overhang_metrics.bbox[1] * overhang_metrics.bbox[2]
    )
    assert overhang_metrics.support.support_volume_mm3 <= bbox_volume, (
        f"支撑估算 {overhang_metrics.support.support_volume_mm3} 超过了包围盒体积 {bbox_volume}"
    )
    assert any("支撑" in issue for issue in overhang_metrics.issues), overhang_metrics.issues

    # 换摆放方向应当能找到更省支撑的摆法（球转到侧面，支撑大幅减少）
    points, _ = mesh.read_triangles(overhang.stl_path)
    candidates = mesh.orientation_candidates(
        points, len(points) // 3, overhang_metrics, overhang_metrics.support.angle_limit_deg
    )
    assert len(candidates) == 6, f"应给出 6 个轴向候选，实际 {len(candidates)}"
    best = min(candidates, key=lambda c: c["support_volume_mm3"])
    assert best["support_volume_mm3"] < overhang_metrics.support.support_volume_mm3, (
        "应当存在比当前摆放更省支撑的方向"
    )
    assert all("build_height_mm" in c for c in candidates)

    # 用量估算：区间 + 自洽性
    usage = mesh.estimate_usage(
        overhang_metrics, material="PLA", density_g_cm3=1.24, support=overhang_metrics.support
    )
    assert usage.total_grams > 0
    assert usage.total_grams_upper >= usage.total_grams, (
        "上限（实体等效）必须不低于下限（按填充估算）"
    )
    assert usage.model_solid_cm3 > usage.model_printed_cm3, "按 15% 填充估算必然小于实体等效"
    # 线材长度必须与体积对得上：长度 = 体积 / 截面积
    cross_section = 3.14159265 * (1.75 / 2) ** 2
    expected_length = (
        (usage.model_printed_cm3 + usage.support_cm3 * 0.15) * 1000.0 / cross_section / 1000.0
    )
    assert abs(usage.filament_length_m - expected_length) < 0.02, (
        usage.filament_length_m,
        expected_length,
    )
    assert usage.notes, "估算必须带上假设说明，不能只甩一个数字"
    # 支撑必须是「估算」这件事要写进说明里 —— 用户不能把它当成切片结果
    assert any("偏大的上界" in note for note in usage.notes), usage.notes
    assert "耗材估算" in mesh.summarize_for_ai(overhang_metrics, "o.stl")
    # 用量随密度线性变化：换成 ABS（1.04）应当更轻
    abs_usage = mesh.estimate_usage(
        overhang_metrics, material="ABS", density_g_cm3=1.04, support=overhang_metrics.support
    )
    assert abs_usage.total_grams < usage.total_grams, (abs_usage.total_grams, usage.total_grams)

    return (
        f"二进制/ASCII 双格式、封闭性、体积({metrics.volume_mm3:.1f}mm³)、"
        f"表面积({metrics.surface_area_mm2:.0f}mm²)、包围盒、散件检测、"
        f"腔体孤岛检测、射线法壁厚、支撑与耗材估算 全部正确"
    )


def t_openscad_install() -> str:
    from aiforge import openscad

    # 已经装好时必须直接复用，不做任何下载。
    # 这条对用户体验很关键：重装软件 / 点「自动修复」时不该让用户再等一次 20MB。
    import time

    reusable = openscad._reusable_installation()  # noqa: SLF001
    if reusable is not None:
        exe, version = reusable
        started = time.perf_counter()
        info = openscad.install("portable")
        elapsed = time.perf_counter() - started
        assert info.found and info.cli_ok, info.detail
        assert Path(info.path or "").is_file(), info.path
        # 复用路径应当很快：不下载、不解压，只做一次 CLI 冒烟测试
        assert elapsed < 60, f"复用已有安装耗时过长（{elapsed:.1f}s），可能又下载了一次"
        return f"复用已有 OpenSCAD {version}（{elapsed:.1f}s，未重新下载）"

    # 机器上确实没有：走完整安装（需要下载，可能较慢）
    info = openscad.install("portable")
    assert info.found and info.cli_ok, info.detail
    assert Path(info.path or "").is_file(), info.path
    assert info.version, "应能读到版本号"
    return f"OpenSCAD {info.version} 已自动安装到 {info.path}"


def t_openscad_detect() -> str:
    from aiforge import openscad

    info = openscad.detect(deep=True)
    assert info.found, info.detail
    assert info.cli_ok, info.detail
    ok, message, elapsed = openscad.smoke_test(info.path)
    assert ok, message
    return f"{info.version}，CLI 冒烟测试 {elapsed:.1f}s"


def t_compiler() -> str:
    from aiforge import compiler, openscad as scad_cli

    work = ROOT / "build" / "work" / "compiletest"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    result = compiler.compile_to_stl("height = 30;\ncube([20, 20, height]);", work, "demo")
    assert result.ok, result.message
    assert result.stl_path and result.stl_path.is_file(), "STL 应已生成"

    metrics = compiler.analyze_stl(result.stl_path)
    assert metrics.watertight, "生成的长方体应当是封闭实体"
    assert abs(metrics.bbox[2] - 30) < 0.01, metrics.bbox
    assert abs(metrics.volume_mm3 - 12000) < 5, metrics.volume_mm3

    # 参数覆盖
    result2 = compiler.compile_to_stl("height = 30;\ncube([20, 20, height]);", work, "demo2", {"height": 55})
    m2 = compiler.analyze_stl(result2.stl_path)
    assert abs(m2.bbox[2] - 55) < 0.01, f"参数覆盖失败：{m2.bbox}"

    # 语法错误必须被识别为失败并给出可读信息
    bad = compiler.compile_to_stl("cube([1,1,1)", work, "bad")
    assert not bad.ok, "语法错误应判定为失败"
    assert bad.message and "错误" in bad.message, bad.message

    svg = compiler.make_thumbnail_svg(m2, "测试模型")
    assert svg.startswith("<svg"), "缩略图应为 SVG"

    # ---- 静默丢几何：编译成功、STL 非空，但少了一整块形状 ----
    # 实测踩到的真实故障：AI 把 3D 的 rotate() 写在 linear_extrude() 的**子节点**位置
    #     linear_extrude(height=72) rotate([90,0,90]) square(...)
    # OpenSCAD 只回一句 warning（Scaling a 2D object with 0 - removing object）
    # 就把**整个主体丢掉**，只剩 union 里的底座 —— 于是 STL 非空、编译 ok=True，
    # 界面显示「模型生成成功」，而用户要的 100mm 高手机支架变成一块 8.7mm 的板。
    # 危害全在「静默」二字，所以必须专门识别并让调用方拿到证据。
    good_part = "module good(){ linear_extrude(height=8) square([78,70]); }\n"
    bad_part = (
        "module bad(){ translate([3,0,0]) linear_extrude(height=72)"
        " rotate([90,0,90]) square([70,100]); }\n"
    )
    silent = compiler.compile_to_stl(
        good_part + bad_part + "union(){ good(); bad(); }", work, "silent_drop"
    )
    assert silent.ok, "这个模型确实会「编译成功」—— 正是问题所在"
    assert silent.dropped_geometry, (
        "必须识别出「几何被丢掉」的警告，否则整条流水线都会以为成功了"
    )
    assert any("removing object" in text for text in silent.dropped_geometry), silent.dropped_geometry

    # 健康模型不能误报
    healthy = compiler.compile_to_stl(good_part + "good();", work, "healthy")
    assert healthy.ok and not healthy.dropped_geometry, healthy.dropped_geometry

    # ---- $fn 注入：判据必须是「顶层有没有真的赋值」，而不是「源码里有没有这三个字符」 ----
    # 实测踩到的真实 Bug：旧判断是 `if "$fn" not in cleaned`（整份源码的子串检查），
    # 于是注释里提一句 $fn、模块内部局部设一次 $fn，都会让注入被跳过 ——
    # 而 $fn 是按「喷嘴 + 模型尺寸」算出来的表面质量参数，
    # 跳过注入就意味着圆弧面出现肉眼可见的棱面（实测同一个圆柱 380 面 → 116 面）。
    # 提示词本身就会让 AI 写 `// @param $fn …` 注释，所以这很容易命中。
    body = "cylinder(h=20, r=10);\n"
    injected_fn = 96
    fn_cases = {
        "完全没提 $fn": body,
        "注释里提到 $fn": f"// 建议 $fn 大一点\n{body}",
        "@param 注释提到 $fn": f"// @param $fn {injected_fn} 曲面细分 64 128\n{body}",
        "模块内部有 $fn": f"module s() {{ $fn = 12; sphere(3); }}\ns();\n{body}",
    }
    for label, code in fn_cases.items():
        built, _applied, _ignored = compiler.build_scad_source(code, None, injected_fn)
        assert f"$fn = {injected_fn};" in built.split("\n")[0], (
            f"{label}：应当注入 $fn = {injected_fn}，实际首行是 {built.splitlines()[0]!r}"
        )
    # 作者显式写了**高于下限**的 $fn：必须原样尊重（这是作者的取舍，不能覆盖）
    respected, _a, _i = compiler.build_scad_source(f"$fn = {injected_fn + 40};\n" + body, None, injected_fn)
    assert f"$fn = {injected_fn + 40};" in respected, respected.splitlines()[:2]
    assert f"$fn = {injected_fn};" not in respected, respected.splitlines()[:2]
    # 顶层赋值在模块后面也算数（不能只看第一行）
    later, _a, _i = compiler.build_scad_source(body + f"$fn = {injected_fn + 40};\n", None, injected_fn)
    assert f"$fn = {injected_fn};" not in later, "顶层已有赋值就不该再注入"

    # ---- 过小的 $fn 必须被抬到下限 -------------------------------------------
    # 用户反馈原话：「建模圆形时边数太少，完全达不到圆的效果」。
    # 实测 AI 会自己写 `$fn = 24`（甚至 8），把 Ø80 的圆画成 24 边形；
    # 而逐图元写法（cylinder(..., $fn = 24)）优先级最高，顶层注入根本盖不住。
    # 规则改为：作者的值只要**不低于按尺寸算出的下限**就尊重，低了就抬上来。
    fn_floor = max(compiler.FN_FLOOR, int(injected_fn * 0.6))
    for low in (8, 24):
        raised, _a, _i = compiler.build_scad_source(f"$fn = {low};\n" + body, None, injected_fn)
        assert f"$fn = {fn_floor};" in raised, f"顶层 $fn={low} 应当被抬到 {fn_floor}：{raised.splitlines()[:2]}"
    inline, _a, _i = compiler.build_scad_source("cylinder(h=20, r=10, $fn = 8);\n", None, injected_fn)
    assert "$fn = 8" not in inline, inline.splitlines()[:3]
    assert "$fn = 8;" not in inline and ")" in inline, inline.splitlines()[:3]
    # 抬升时必须保留原来的结尾符：曾经一律替换成 `$fn = N;`，
    # 于是 `cylinder(..., $fn = 52;)` 直接语法错误 —— 修显示问题却把模型弄编译不过。
    inline_ok = compiler.compile_to_stl("cylinder(h=20, r=10, $fn = 8);\n", work, "fn_inline")
    assert inline_ok.ok, (inline_ok.message, inline_ok.fatal)
    # 并且抬升真的改变了细分度（对照：直接让 OpenSCAD 编译未经规范化的 $fn = 8）
    plain_cyl = compiler.compile_to_stl(body, work, "fn_plain", None, None, injected_fn)
    coarse_cyl = compiler.compile_to_stl("$fn = 8;\n" + body, work, "fn_coarse", None, None, injected_fn)
    raw_scad = work / "fn_raw.scad"
    raw_stl = work / "fn_raw.stl"
    raw_scad.write_text("$fn = 8;\n" + body, encoding="utf-8")
    scad_cli.run_cli(["-o", str(raw_stl), str(raw_scad)])
    raw_triangles = compiler.analyze_stl(raw_stl).triangles
    raised_triangles = compiler.analyze_stl(coarse_cyl.stl_path).triangles
    assert raised_triangles > raw_triangles * 3, (
        f"抬升后细分度必须显著提高：原始 $fn=8 是 {raw_triangles} 面，抬升后 {raised_triangles} 面"
    )
    assert compiler.analyze_stl(plain_cyl.stl_path).triangles >= raised_triangles, "注入值不应低于抬升后的下限"

    # 正确写法（旋转在挤出**外面**）不能被误判，而且形状真的对
    fixed = compiler.compile_to_stl(
        "rotate([90,0,90]) linear_extrude(height=30) square([20,40]);", work, "extrude_fixed"
    )
    assert fixed.ok and not fixed.dropped_geometry, fixed.dropped_geometry
    fixed_metrics = compiler.analyze_stl(fixed.stl_path)
    assert abs(fixed_metrics.volume_mm3 - 24000) < 50, fixed_metrics.volume_mm3
    # 平面内旋转（只绕 Z）也是合法的，同样不能误报
    flat = compiler.compile_to_stl(
        "linear_extrude(height=30) rotate([0,0,90]) square([20,40]);", work, "extrude_flat"
    )
    assert flat.ok and not flat.dropped_geometry, flat.dropped_geometry

    return f"编译/参数覆盖/错误识别/静默丢几何检测/$fn 注入/缩略图 全部正确（封闭实体，{m2.triangles} 面）"


def t_stl_robustness() -> str:
    """STL 读取的健壮性：损坏的文件必须**明确报错**，不能算出垃圾或直接崩。

    为什么必须有：这条是实测撞出来的 —— 一个头部声明 998,000 面、实际只有
    758,480 面的文件（写入被中断），旧实现"悄悄按实际长度截断"继续解析。
    但真实步长不是 50 字节，于是整段错位，读出来的坐标是 NaN 和 3e34，
    一路走到 `int(round(nan))` 才炸成
    `ValueError: cannot convert float NaN to integer` ——
    用户看到的是和建模毫无关系的一句话；就算不炸，也是一份残缺网格被当成
    正常模型分析，封闭性/零件数/壁厚全是错的，报告会说「模型有 300 个散件」。
    """
    import struct

    from aiforge import mesh

    work = ROOT / "build" / "work" / "meshtest"
    work.mkdir(parents=True, exist_ok=True)

    def write_binary(path: Path, faces: list, declared: int | None = None) -> None:
        with path.open("wb") as handle:
            handle.write(b"solid selftest\n".ljust(80, b"\0"))
            handle.write(struct.pack("<I", len(faces) if declared is None else declared))
            for face in faces:
                handle.write(struct.pack("<12f", 0, 0, 0, *face[0], *face[1], *face[2]))
                handle.write(struct.pack("<H", 0))

    face = (((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),)

    # 1) 坐标里有 NaN → 必须报「文件已损坏」，不能崩在 int(round(nan))
    nan_path = work / "nan.stl"
    write_binary(nan_path, [((float("nan"), 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))])
    try:
        mesh.read_triangles(nan_path)
    except mesh.StlError as exc:
        assert "有效数字" in exc.message or "损坏" in exc.message, exc.message
        assert exc.hint, "报错必须给出下一步怎么做"
    else:
        raise AssertionError("含 NaN 坐标的 STL 必须报错，不能算出垃圾指标")

    # 2) 声明面数远大于实际（写入被中断）→ 必须报「不完整」并带上两个数字
    short_path = work / "short.stl"
    write_binary(short_path, list(face) * 3, declared=100_000)
    try:
        mesh.read_triangles(short_path)
    except mesh.StlError as exc:
        assert "不完整" in exc.message, exc.message
        assert "100,000" in exc.message, exc.message
    else:
        raise AssertionError("声明 10 万面却只有 3 面的文件必须报「不完整」")

    # 3) 只差几个面的尾部竞态仍然容忍 —— 这是原有的宽松行为，不能顺手改严
    tail_path = work / "tail.stl"
    write_binary(tail_path, list(face) * 20, declared=26)
    points, _ = mesh.read_triangles(tail_path)
    assert len(points) == 60, len(points)

    # 4) ASCII 里的 1e999 会变成 inf → 同样必须报错
    inf_path = work / "inf.stl"
    inf_path.write_text(
        "solid s\nfacet normal 0 0 0\nouter loop\n"
        "vertex 0 0 0\nvertex 1e999 0 0\nvertex 0 1 0\n"
        "endloop\nendfacet\nendsolid s\n",
        encoding="utf-8",
    )
    try:
        mesh.read_triangles(inf_path)
    except mesh.StlError as exc:
        assert "有效数字" in exc.message or "损坏" in exc.message, exc.message
    else:
        raise AssertionError("坐标是无穷大的 ASCII STL 必须报错")

    # 4b) **ASCII 被截断**也必须报错。二进制那条路早就有「头部声明 N 面、
    #     实际只有 M 面」的自校验，而 ASCII 没有 —— 它不是小众格式：本产品的
    #     OpenSCAD 默认就写 ASCII（实测一个圆管是 171,329 字节 / 1,024 个 facet）。
    #     写一半被中断的 ASCII 文件能被解析器**完好地**读出一堆合法坐标：
    #     没有 NaN、没有错位，只是后面的面没了 —— 于是残缺网格被当成正常模型
    #     送去分析，孔径判据直接翻面（实测同一 SCAD：一轮 512 面 hole=None，
    #     另一轮 1024 面 hole=17.60）。
    #     三条守卫各验一条，另加一条**阳性对照**（完整文件必须照常解析）。
    facets3 = "".join(
        f"facet normal 0 0 0\nouter loop\nvertex {i} 0 0\nvertex {i + 1} 0 0\n"
        f"vertex {i} 1 0\nendloop\nendfacet\n"
        for i in range(3)
    )
    full_ascii = f"solid t\n{facets3}endsolid t\n"
    ok_path = work / "ascii_ok.stl"
    ok_path.write_text(full_ascii, encoding="utf-8")
    points, is_ascii = mesh.read_triangles(ok_path)
    assert is_ascii and len(points) == 9, (is_ascii, len(points))

    # i) 切在 facet 中间：删掉一个 vertex 行 → 顶点数不是 3 的倍数
    #    （故意只删一行：删多了会连带触发其它条件，那样就分不清是哪条守卫拦的）
    cut_at = full_ascii.rindex("vertex ")
    mid_text = full_ascii[:cut_at] + full_ascii[full_ascii.index("\n", cut_at) + 1:]
    mid_path = work / "ascii_mid.stl"
    mid_path.write_text(mid_text, encoding="utf-8")
    try:
        mesh.read_triangles(mid_path)
    except mesh.StlError as exc:
        assert "3 的倍数" in exc.message, exc.message
    else:
        raise AssertionError("切掉半个三角面的 ASCII STL 必须报错，不能静悄悄少算一面")

    # ii) 只丢一个 facet 关键字：顶点数仍是 3 的倍数，靠「面数对不上」才能发现
    mism_path = work / "ascii_mismatch.stl"
    mism_path.write_text(full_ascii.replace("facet normal", "#", 1), encoding="utf-8")
    try:
        mesh.read_triangles(mism_path)
    except mesh.StlError as exc:
        assert "实际只读到" in exc.message, exc.message
    else:
        raise AssertionError("facet 数与顶点数矛盾的 ASCII STL 必须报错")

    # iii) 刚好切在边界上、且缺 endsolid：前两条都查不出来，只能靠收尾行
    tailmiss_path = work / "ascii_no_endsolid.stl"
    tailmiss_path.write_text(f"solid t\n{facets3}", encoding="utf-8")
    try:
        mesh.read_triangles(tailmiss_path)
    except mesh.StlError as exc:
        assert "endsolid" in exc.message, exc.message
    else:
        raise AssertionError("缺 endsolid 收尾的 ASCII STL 必须报错（那就是没写完）")

    # 5) 朝向变换必须逐位等于「逐个分量现算」的语义。
    #    这一条钉住的是性能改动：原来每个分量都现建一次 {"x":0,...} 字典再做字符串
    #    操作，一个面 36 次，100 万面上就是 3600 万次（占整条链路 45% 的时间）。
    #    现在改成「朝向只编译一次」，语义必须一模一样。
    slots = {"x": 0, "y": 1, "z": 2}

    def reference(axes: tuple[str, str, str], values: tuple[float, float, float]):
        out = []
        for axis in axes:
            value = values[slots[axis[-1]]]
            out.append(-value if axis.startswith("-") else value)
        return tuple(out)

    sample = (1.5, -2.25, 3.75)
    orientations = (
        ("x", "y", "z"),
        ("x", "-y", "-z"),
        ("-x", "z", "-y"),
        ("y", "z", "x"),
        ("z", "x", "-y"),
        ("-z", "-x", "y"),
    )
    for axes in orientations:
        normals, centroids, drop = mesh._transform_faces(axes, [sample], [sample])
        expected = reference(axes, sample)
        assert normals[0] == expected, (axes, normals[0], expected)
        # 单个点落到平台上之后高度必然是 0，且 drop 就是它在原朝向下的高度
        assert centroids[0] == (expected[0], expected[1], 0.0), (axes, centroids[0])
        assert drop == expected[2], (axes, drop)

    # 6) 顶点量化缓存（同坐标只算一次）不改变焊接结果这一点，由
    #    「散件检测」那组断言用真实夹具覆盖：1 件 / 2 件 / 仅贴近不算相交 /
    #    密封内腔不算第二件，四种情形都走这条缓存路径。
    #    这里再补一条最小契约：同一坐标必须焊成一个零件。
    tetra = [
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
        ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        ((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
        ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
    ]
    flat: list = []
    for item in tetra:
        flat.extend(item)
    one, stray, _details = mesh._count_parts(flat, len(tetra))
    assert (one, stray) == (1, 0), f"共顶点的四面体应焊成 1 个零件，实际 {(one, stray)}"

    return "损坏 STL（NaN/inf/二进制截断/ASCII 截断）明确报错、尾部竞态仍容忍、朝向变换语义逐位不变"


def t_analysis_heartbeat() -> str:
    """长网格分析必须持续推「还在跑」的心跳。

    为什么必须有：网格分析是纯 Python 逐面计算，面数大的模型要跑几十秒
    （实测 100 万面约 50 秒），这段时间里一个事件都不推。而前端的卡死看门狗
    判据是「120 秒没有任何进展 → 判定卡住并取消作业」—— 也就是说，
    一个**完全正常**的大模型分析会被自己的看门狗杀掉。
    这里用「把分析放慢」的方式确定性地验证心跳，不依赖真实大模型。
    """
    import asyncio
    import struct
    import time as time_mod

    from aiforge import compiler, jobs, mesh

    work = ROOT / "build" / "work" / "meshtest"
    work.mkdir(parents=True, exist_ok=True)
    cube = work / "heartbeat_cube.stl"
    face = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    with cube.open("wb") as handle:
        handle.write(b"\0" * 80)
        handle.write(struct.pack("<I", 12))
        for _ in range(12):
            handle.write(struct.pack("<12f", 0, 0, 0, *face[0], *face[1], *face[2]))
            handle.write(struct.pack("<H", 0))

    job = jobs.create_job(project_id="heartbeat-test", revision=1, mode="create")
    original = compiler.analyze_stl
    forwarded: list = []

    def slowed(path, limits=None, declared_gaps=None):
        # 50ms 的分析 + 10ms 心跳 = 至少 4 次心跳，确定性足够
        forwarded.append(declared_gaps)
        time_mod.sleep(0.05)
        return original(path, limits, declared_gaps)

    compiler.analyze_stl = slowed  # type: ignore[assignment]
    try:
        metrics = asyncio.run(
            jobs._analyze_with_heartbeat(
                job, cube, mesh.AnalysisLimits(), heartbeat_s=0.01
            )
        )
        # 传源码时必须把「源码里声明的间隙」一起送去实测 —— 这是「夹口那一刀完全
        # 没切到实体」唯一的揭穿路径；接线断掉不会报错，只会默默退回全绿。
        asyncio.run(
            jobs._analyze_with_heartbeat(
                job,
                cube,
                mesh.AnalysisLimits(),
                source="clamp_gap = 14;\ncube(10);\n",
                heartbeat_s=0.01,
            )
        )
    finally:
        compiler.analyze_stl = original  # type: ignore[assignment]

    assert metrics.triangles == 12, metrics.triangles
    assert forwarded[0] is None, f"不传 source 时不该做空隙实测（白白多跑一遍射线扫描）：{forwarded[0]}"
    assert forwarded[1] and forwarded[1][0][2] == "clamp_gap", f"source 里的声明间隙没被透传：{forwarded[1]}"
    beats = [m.text for m in job.messages if "仍在分析" in m.text]
    assert beats, "长分析期间必须推心跳，否则前端看门狗会把正常作业取消掉"
    assert any("秒" in text for text in beats), beats
    return f"长分析心跳正常（{len(beats)} 次），不会被看门狗误杀；声明间隙已透传到实测"


def t_fit_scope_dimension() -> str:
    """「杯径 70」这类**配合尺寸**不许当成整件外形去核对，更不许据此缩整个模型。

    实测现场（复杂模型端到端：水杯架）：规格 label 就是「杯径」，name 是 cup_dia
    这种形式，程序把它配到了「水平包络」上，于是同时踩两个坑：
      * 修复日志报【问题 8】「要求 70mm，实测水平最大（XY）132.0mm（超出 89%）」——
        一条根本不存在的缺陷，白烧一轮修复，用户看到的是「它非说我做错了」；
      * 逐轴校正算出 70/132 = 0.53，而它正好落在旧的 0.5~2.0 放行区间里 ——
        真跑起来会把夹口、底座、加强筋连整体缩掉 47%。

    两头都得钉住：真正的外形尺寸（外径/总高）偏大照报、该缩仍要能缩；
    配合尺寸**偏小**照报（整件连 70 都不到，那个杯径必然是没做到的）。
    """
    from aiforge import jobs, mesh, reqtests
    from aiforge.design import DesignSpec, SpecItem, diameter_is_fit_scope

    # ---- 词形判定：只认「剥掉直径词根后还剩个主体名」的这种形式 ----
    for label, name in (
        ("杯径", "cup_dia"), ("口径", "mouth_diameter"),
        ("杯口直径", "cup_opening_dia"), ("轮径", "wheel_dia"),
    ):
        assert diameter_is_fit_scope(label, name), f"{label}/{name} 是在说被容纳的东西"
    for label, name in (
        ("外径", "outer_diameter"), ("直径", "diameter"), ("总直径", "total_dia"),
        ("最大外径", "max_outer_dia"), ("高度_mm", "height_mm"), ("壁厚", "wall_thickness"),
    ):
        assert not diameter_is_fit_scope(label, name), f"{label}/{name} 是整件外形或不是直径"

    fit_spec = DesignSpec(
        kind="水杯架",
        specs=[SpecItem(name="cup_dia", label="杯径", value=70.0, unit="mm", user_specified=True)],
    )
    declared = jobs._spec_declared_dimensions(fit_spec)
    assert declared[jobs.HORIZONTAL_ENVELOPE_AXIS][1] == 70.0, declared

    holder = mesh.MeshMetrics(
        triangles=776, solid_parts=1, bbox=(132.0, 63.0, 95.0), watertight=True, watertight_ratio=1.0
    )
    assert jobs._spec_dimension_problems(fit_spec, holder) == [], "座子比杯径大是正常的，不许报缺陷"
    assert jobs._integrity_rank(holder, fit_spec)[2] == 0, "误报还在的话，采纳判据会白烧一轮修复"

    # 需求核对表也不许判红，但要说实话：包围盒证明不了这个尺寸做到了
    suite = reqtests.evaluate(
        metrics=holder, requirement="能放 70mm 的水杯", declared_dimensions=declared
    )
    dim_tests = [t for t in suite.tests if t.kind == "dimension"]
    assert dim_tests and all(t.status != reqtests.FAIL for t in dim_tests), [
        (t.status, t.actual) for t in dim_tests
    ]
    assert not jobs._hard_failures(suite), "配合尺寸偏大不该拖红整张核对表"
    assert "外观复核" in dim_tests[0].actual, dim_tests[0].actual

    # 反方向（偏小）仍然是真缺陷：整件 50 放不下 70 的杯子
    tiny = mesh.MeshMetrics(
        triangles=776, solid_parts=1, bbox=(50.0, 40.0, 90.0), watertight=True, watertight_ratio=1.0
    )
    short_problems = jobs._spec_dimension_problems(fit_spec, tiny)
    assert len(short_problems) == 1 and "只有 50.0mm" in short_problems[0], short_problems

    # ---- 确定性逐轴缩放：不许拿配合尺寸去缩整件 ----
    runner = jobs.GenerationRunner.__new__(jobs.GenerationRunner)
    source = "cup_dia = 70;\ncube([132, 63, 95]);\n"
    assert (
        runner._axis_dimension_fix(source, holder, fit_spec) is None  # noqa: SLF001
    ), "0.53 的倍率会把水杯架连夹口带底座缩掉 47%"
    # 放大方向放行：整件连 70 都不到，按比例放大是合理的确定性兜底
    grown = runner._axis_dimension_fix(source, tiny, fit_spec)  # noqa: SLF001
    assert grown and "scale([1.400000, 1.400000, 1.000000])" in grown, grown

    # ---- 保住原有覆盖：真正的外形尺寸两头都照旧 ----
    outer = DesignSpec(
        kind="圆形笔筒",
        specs=[
            SpecItem(name="outer_diameter", label="外径", value=80.0, unit="mm", user_specified=True),
            SpecItem(name="height", label="总高", value=100.0, unit="mm", user_specified=True),
        ],
    )
    blob = mesh.MeshMetrics(
        triangles=776, solid_parts=1, bbox=(92.3, 145.8, 100.0), watertight=True, watertight_ratio=1.0
    )
    blob_problems = jobs._spec_dimension_problems(outer, blob)
    assert len(blob_problems) == 1 and "外径 要求 80mm" in blob_problems[0], blob_problems
    # 0.714 这种「多了一截、确实该缩」的合法校正必须仍然生效
    legit = runner._axis_dimension_fix(source, mesh.MeshMetrics(bbox=(80.0, 80.0, 140.0)), spec=outer)  # noqa: SLF001
    assert legit and "scale([1.000000, 1.000000, 0.714286])" in legit, legit
    # 而缩到 2/3 以下（外径 80 vs 实测 170 → 0.588）属于需求理解错了：只报问题，不乱缩
    refused = jobs._spec_dimension_problems(outer, mesh.MeshMetrics(bbox=(80.0, 80.0, 170.0)))
    assert refused, "超出 50% 的外形尺寸仍然要报"
    assert (
        runner._axis_dimension_fix(source, mesh.MeshMetrics(bbox=(80.0, 80.0, 170.0)), spec=outer)  # noqa: SLF001
        is None
    ), "要缩到 2/3 以下才能凑上的目标不该由程序硬缩"
    return (
        "配合尺寸（杯径/口径/cup_dia）不再驱动【问题 8】与整件缩小（0.53 被拒）、"
        "偏小仍报；外径/总高双向核对与 0.714 合法校正保持不变"
    )


def t_declared_gap_verdict() -> str:
    """声明了的间隙/夹口在网格里量不到时，判定链必须一路把它拖成 fail。

    为什么单独钉这一条（实测的完整假绿链条）：交付给用户的手机夹源码写着
    `clamp_gap = 14`，而 difference 的刀具被 `rotate([-90,0,0])` 搬到了 z<0 的
    模型外 —— 夹口那一刀**完全没切到实体**。但编译成功、STL 非空、没有 warning，
    零件数 1、封闭、包围盒对、壁厚甚至因为「根本没有腔」而显得充足，
    需求核对 9 项全绿、打印性判定 pass / 85 分。唯一看出问题的是外观复核，
    而它当时既不参与 verdict，也不上结果卡。

    这里用两份**合成网格**（40mm 实心块 / 同样大小但中间挖一个 16mm 空腔）
    把判据钉死：不编译、不依赖 OpenSCAD，所以 CI 离线也一定跑到；而它验的正是
    那条链的四个环节 —— 测得到吗 → 打分 → 回喂 AI 的措辞 → 最终判定降级。
    反例（有空腔时不许降级）同样重要：假失败会把本来正常的模型打成不可打印。
    """
    from aiforge import jobs, mesh, reqtests

    def box(lo, hi):
        """一个长方体的 12 个三角面（顶点拉平的三元组列表）。"""
        x0, y0, z0 = lo
        x1, y1, z1 = hi
        v = [
            (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
            (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
        ]
        faces = [
            (0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6),
            (0, 4, 5), (0, 5, 1), (1, 5, 6), (1, 6, 2),
            (2, 6, 7), (2, 7, 3), (3, 7, 4), (3, 4, 0),
        ]
        out: list = []
        for a, b, c in faces:
            out.extend((v[a], v[b], v[c]))
        return out

    def metrics_of(triangles):
        m = mesh.MeshMetrics(
            triangles=len(triangles) // 3, vertices=8, watertight=True, solid_parts=1, stray_parts=0
        )
        m.bbox_min = (min(p[0] for p in triangles), min(p[1] for p in triangles), min(p[2] for p in triangles))
        m.bbox_max = (max(p[0] for p in triangles), max(p[1] for p in triangles), max(p[2] for p in triangles))
        m.bbox = tuple(hi - lo for hi, lo in zip(m.bbox_max, m.bbox_min))
        return m

    declared = [("夹口张开", 14.0, "clamp_gap")]
    tris_solid = box((0.0, 0.0, 0.0), (40.0, 40.0, 40.0))
    tris_void = tris_solid + box((15.0, 12.0, 15.0), (25.0, 28.0, 25.0))
    solid, voided = metrics_of(tris_solid), metrics_of(tris_void)
    for m, tris in ((solid, tris_solid), (voided, tris_void)):
        m.declared_gaps = list(declared)
        m.interior_gaps = mesh.measure_interior_gaps(tris, m.triangles, m, min_gap_mm=14.0)
    assert solid.interior_gaps and voided.interior_gaps, "合成网格太小，这条自检等于没跑"

    # 实心块：三轴都找不到任何「被材料夹住」的空档 —— 这就是那一刀没切到的形状
    assert solid.interior_gaps.max_mm == 0.0, solid.interior_gaps.to_dict()
    # 挖了 16mm 空腔：沿 Y 必须量到（判据能揭穿「没切到」全靠这个数）
    y_gap = voided.interior_gaps.per_axis[1]
    assert 15.0 <= y_gap <= 16.2, voided.interior_gaps.to_dict()

    suite_bad = reqtests.evaluate(metrics=solid, requirement="带夹口的手机夹", declared_gaps=declared)
    gap_tests = [t for t in suite_bad.tests if t.kind == "feature-gap"]
    assert gap_tests and all(t.status == reqtests.FAIL for t in gap_tests), [
        (t.status, t.actual) for t in gap_tests
    ]
    assert jobs._hard_failures(suite_bad), "间隙实测失败不算硬伤 = 假绿复活"
    # 回喂给 AI 的措辞：必须带上轴向与连续材料长度，光说「没空隙」它不知道往哪改
    feed = jobs._gap_problems(solid)
    assert len(feed) == 1 and "【问题 10】" in feed[0] and "clamp_gap" in feed[0], feed
    assert "横跨这个区域" in feed[0] and "同一坐标系" in feed[0], "两条实测踩过的坑都得写进回喂"

    verdict_bad = {"verdict": "pass", "score": 85, "issues": []}
    why = jobs._downgrade_printability(verdict_bad, suite_bad, "", [])
    assert why, "实测硬失败却不降级"
    assert verdict_bad["verdict"] == "fail" and verdict_bad["score"] <= 49, verdict_bad
    assert verdict_bad["issues"][0]["severity"] == "error", verdict_bad["issues"][0]

    # 反例：空隙真存在时一句都不许报（假失败比漏报更糟）
    suite_ok = reqtests.evaluate(metrics=voided, requirement="带夹口的手机夹", declared_gaps=declared)
    ok_tests = [t for t in suite_ok.tests if t.kind == "feature-gap"]
    assert ok_tests and all(t.status == reqtests.PASS for t in ok_tests), [
        (t.status, t.actual) for t in ok_tests
    ]
    assert not jobs._gap_problems(voided), jobs._gap_problems(voided)
    verdict_ok = {"verdict": "pass", "score": 85, "issues": []}
    assert jobs._downgrade_printability(verdict_ok, suite_ok, "", []) is None, verdict_ok
    assert verdict_ok == {"verdict": "pass", "score": 85, "issues": []}, verdict_ok

    # 源码 → 声明间隙：只认顶层纯数值赋值，且不能被 clamp_w 这类**实体宽度**骗到
    found = jobs._declared_gaps_from_source("clamp_gap = 14;\nclamp_w = 60;\nwall = 3;\n")
    assert [n for _l, _v, n in found] == ["clamp_gap"], found
    assert jobs._declared_gaps_from_source("span = 20;\n") == [], "普通长度不该被当成间隙"

    # 给 AI 的客观数据里必须带上这条矛盾：不给它，打印性复核会闭着眼说「可以打印」
    ai_summary = mesh.summarize_for_ai(solid, "model.stl")
    assert "间隙实测" in ai_summary and "夹口张开=14mm" in ai_summary, ai_summary[-400:]
    assert "根本没切到实体" in ai_summary, ai_summary[-400:]
    assert "间隙实测" not in mesh.summarize_for_ai(
        mesh.MeshMetrics(triangles=12, bbox=(40.0, 40.0, 40.0)), "model.stl"
    ), "没声明间隙时不该占提示词篇幅"

    return (
        f"实心块最大被夹空档 {solid.interior_gaps.max_mm:g}mm → feature-gap FAIL/硬伤/降级为 "
        f"{verdict_bad['verdict']}·{verdict_bad['score']}；挖 16mm 空腔的块沿 Y 量到 {y_gap:.1f}mm → 不降级"
    )


def t_announce_spec_values() -> str:
    """需求复述必须把「我替你定的具体数值」念出来，用户才有可纠正的对象。

    实测用户抱怨「对话大模型无法领会意思」，很大一块是没露面就生效的假设：
    旧版只说「有几点你没说清楚，我按常见做法先定了」，一个数字都不给 ——
    用户既不知道夹口开度取了多少，也没法说「改成 12」。所以这里钉住：
    假设值逐个出现、非用户指定的项不得混进来、全是指定值时不多嘴。
    """
    from aiforge import design, jobs

    job = jobs.create_job(project_id="announce-test", revision=1, mode="create")
    spec = design.DesignSpec(
        kind="手机夹",
        summary="带夹口的手机支架",
        specs=[
            design.SpecItem(name="height", label="总高", value=120.0, user_specified=True),
            design.SpecItem(name="clamp_gap", label="夹口开度", value=14.0),
            design.SpecItem(name="wall", label="壁厚", value=2.4),
            design.SpecItem(name="lanyard_hole", label="挂绳孔"),  # 规格里没数值，AI 自己在代码里定
        ],
        open_questions=["夹口朝向哪边？"],
    )
    # _announce_spec 不碰 self，直接拿未绑定方法跑（不依赖网络与完整执行器）
    jobs.GenerationRunner._announce_spec(None, job, spec)  # type: ignore[arg-type]
    text = "\n".join(m.text for m in job.messages)
    assert "夹口开度 14mm" in text and "2.4mm" in text, text
    assert "挂绳孔" in text, text
    assert "夹口朝向哪边" in text, text
    assert "哪一项不对" in text, text
    # 用户自己说过的数字不能当成「我替你定的」（那会让用户怀疑自己的需求被改过）
    assert "我按常见做法取了值：" not in text or "总高 120mm" not in text, text

    plain = jobs.create_job(project_id="announce-test-plain", revision=1, mode="create")
    plain_spec = design.DesignSpec(
        kind="转接环",
        specs=[design.SpecItem(name="outer", label="外径", value=72.0, user_specified=True)],
    )
    jobs.GenerationRunner._announce_spec(None, plain, plain_spec)  # type: ignore[arg-type]
    assert all("我按常见做法取了值" not in m.text for m in plain.messages), [
        m.text for m in plain.messages
    ]
    return "假设值逐个念出来（14mm / 2.4mm / 未定项 / 不确定处），用户可直接纠正；无假设时不多嘴"


def t_helper_job_scope() -> str:
    """jobs.py 里「以 Job 为首参」的模块级助手，调用时首参必须在作用域内。

    为什么必须有：给网格分析加心跳时，我把 4 处
    `await asyncio.to_thread(compiler.analyze_stl, ...)` 换成了
    `await _analyze_with_heartbeat(job, ...)`。其中一处所在的方法里**没有**
    局部 `job`（那个方法用的是 `self.job`），于是那一行永远抛
    `NameError: name 'job' is not defined`。

    阴的地方在于：这条路径（模型超出成型范围 → 确定性等比缩放兜底）平时走不到，
    自检 38 项全绿、前端体检全绿、连跑两轮端到端的第一轮也没触发，
    **只有端到端跑到「越界用例」才炸出来**。

    这类「名字不在作用域里」的错 Python 编译期查不出来（它不是语法错误，
    而是运行到那一行才报），所以用 AST 静态查一遍最划算 ——
    项目里 `t_module_aliases` 已经是同一个套路。
    """
    import ast

    source = (SRC / "aiforge" / "jobs.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    # 模块级名字（常量、模块导入等）也算在作用域内
    module_names = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    module_names.add(target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                module_names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_names.add(node.name)

    watched = {"_analyze_with_heartbeat", "publish_spec"}
    problems: list[str] = []

    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        params = {a.arg for a in fn.args.args}
        assigned = {
            t.id
            for s in ast.walk(fn)
            if isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign))
            for t in (s.targets if isinstance(s, ast.Assign) else [s.target])
            if isinstance(t, ast.Name)
        }
        for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
            func = call.func
            if not isinstance(func, ast.Name) or func.id not in watched or not call.args:
                continue
            first = call.args[0]
            if isinstance(first, ast.Name):
                if first.id in params or first.id in assigned or first.id in module_names:
                    continue
                problems.append(f"jobs.py:{call.lineno} {func.id}() 的首参 {first.id!r} 在 {fn.name}() 里没有定义")
            elif isinstance(first, ast.Attribute):
                continue  # self.job 这类属性访问由 Python 自己保证
            else:
                problems.append(f"jobs.py:{call.lineno} {func.id}() 的首参不是名字（{ast.dump(first)[:40]}）")

    assert not problems, "；".join(problems)
    return f"{len(watched)} 个以 Job 为首参的助手，调用处首参全部在作用域内"


def t_install_no_destroy() -> str:
    """一次失败的「重新安装」绝不能把原来那份能用的 OpenSCAD 砸掉。

    风险路径：用户已经装好了 OpenSCAD（程序自己管理的 `runtime\\openscad`），
    某次重新安装时压缩包损坏 / 磁盘满 / 杀软拦截解压。
    旧实现是「**先删正式目录、再解压**」——解压一失败，用户就从
    「有得用」直接变成「什么都没有」，而且重新下载还要再等 20MB。
    这和几何修复里「绝不采纳更差的那一版」是同一条原则：
    **失败的操作不能留下比操作前更糟的状态。**

    修法：解压到旁路目录 → 找到 exe → 先在旁路里跑冒烟测试 →
    全部通过才替换正式目录；任何一步失败都只清掉旁路，正式目录原封不动。
    这条测试**不联网**：打桩下载与校验，直接喂一个损坏的压缩包。
    """
    import zipfile  # noqa: F401  （仅用于说明喂进去的是 zip 路径）

    from aiforge import openscad

    work = ROOT / "build" / "work" / "installtest"
    shutil.rmtree(work, ignore_errors=True)
    target = work / "runtime" / "openscad"
    old_exe = target / "openscad-2021.01" / "openscad.exe"
    old_exe.parent.mkdir(parents=True, exist_ok=True)
    old_exe.write_bytes(b"fake old install")

    archive = work / "broken.zip"
    archive.write_bytes(b"this is not a zip file at all")

    real_dir = openscad.managed_install_dir
    real_download = openscad.download_asset
    real_verify = openscad.verify_download
    openscad.managed_install_dir = lambda: target  # type: ignore[assignment]
    openscad.download_asset = lambda asset, progress=None, dest_dir=None: archive  # type: ignore[assignment]
    openscad.verify_download = lambda path, asset, expected=None: (True, "自检打桩：视为校验通过")  # type: ignore[assignment]
    try:
        try:
            openscad.install_portable()
        except openscad.OpenScadError as exc:
            assert "解压失败" in exc.message, exc.message
            assert "没有被改动" in (exc.hint or ""), exc.hint
        else:
            raise AssertionError("损坏的压缩包必须报错，不能当成安装成功")
    finally:
        openscad.managed_install_dir = real_dir  # type: ignore[assignment]
        openscad.download_asset = real_download  # type: ignore[assignment]
        openscad.verify_download = real_verify  # type: ignore[assignment]

    assert old_exe.is_file() and old_exe.read_bytes() == b"fake old install", (
        "失败的安装把原来那份 OpenSCAD 删掉了 —— 用户从「有得用」变成「什么都没有」"
    )
    assert not (work / "runtime" / "openscad.new").exists(), (
        "失败后必须清掉旁路目录，不能留下半个安装"
    )

    # 下载三次都失败后，不能把半截的 .part 留在用户盘上：
    # 它正是「因为不完整才会重下」的那个文件，留着只是白占最多 20MB。
    import httpx as httpx_mod

    downloads = work / "downloads"
    real_stream = httpx_mod.stream

    def boom(method, url, **kwargs):
        # 模拟「已经写了一部分才断线」：把半截内容落到 .part 上，然后抛错。
        # 必须真的留下 .part，否则这条断言永远看不出清理有没有做
        # （自检里踩过：桩只抛异常、不落文件，于是"停用清理"也能通过）。
        name = str(url).rsplit("/", 1)[-1]
        downloads.mkdir(parents=True, exist_ok=True)
        (downloads / (name + ".part")).write_bytes(b"half downloaded")
        raise httpx_mod.ConnectError("自检打桩：传输中断")

    httpx_mod.stream = boom  # type: ignore[assignment]
    try:
        try:
            openscad.download_asset("OpenSCAD-2021.01-x86-64.zip", dest_dir=downloads)
        except openscad.OpenScadError as exc:
            assert "无法从 OpenSCAD 官方源下载" in exc.message, exc.message
            assert exc.hint, "下载失败必须给出下一步（检查网络或手动下载）"
        else:
            raise AssertionError("网络不可达时必须报错")
    finally:
        httpx_mod.stream = real_stream  # type: ignore[assignment]
    leftovers = sorted(p.name for p in downloads.glob("*.part"))
    assert not leftovers, f"下载失败后必须清掉半截文件，实际残留：{leftovers}"
    assert not (downloads / "OpenSCAD-2021.01-x86-64.zip").exists(), "失败不该产出正式文件"

    return "安装失败不会破坏已有安装（旁路替换），失败的下载不留半截文件"


def t_install_test_keeps_config() -> str:
    """安装验收脚本不许覆盖维护者自己的 config.json。

    为什么必须有：`scripts\\test_install.ps1` 会往**真实**用户目录里种一份
    「哨兵配置」来验证「升级/卸载/重装都不丢用户数据」。但脚本原来的清理逻辑
    只保护**目录**（`$preExistingUserData` 时不动 AppData 与 Documents），
    **不保护那个文件本身** —— 而开发机按定义就有真实的 config.json
    （主题、机型、材料、首次运行状态）。于是在开发机上跑一次验收，
    维护者自己的设置就被那句 `Set-Content $sentinelConfig` 永久替换掉了，
    而且因为目录被"正确保留"，看起来什么都没坏。

    这类"测试工具破坏它本应保护的东西"的问题必须静态钉住：
    备份必须发生在种哨兵之前，恢复必须在**任何**收尾分支里都执行
    （-KeepUserData 只意味着"保留测试安装和测试项目"，
    绝不意味着"把盖在用户设置上的哨兵留下"）。
    """
    source = (ROOT / "scripts" / "test_install.ps1").read_text(encoding="utf-8")
    assert "installtest-backup" in source, "缺少真实 config.json 的备份步骤"
    plant = source.find("Sentinel config planted")
    assert plant > 0, "找不到种哨兵的那一步（脚本被改过了吗？）"
    backup = source.find("config.json.installtest-backup")
    assert 0 < backup < plant, "备份必须发生在覆盖 config.json 之前"
    restore = source.find("Restored the original config.json")
    assert restore > plant, "缺少恢复步骤"
    # 「无条件执行」不能只靠"文本在判断之后"来判断 —— 写在 else 分支里也满足那句。
    # 用**缩进**判定层级：恢复用的那个 if 必须与 `if (-not $KeepUserData)` 同层
    # （或更外层），深一层就说明它被塞进了某个分支里。
    lines = source.splitlines()
    guard_indent = restore_indent = None
    for line in lines:
        stripped = line.strip()
        if guard_indent is None and stripped.startswith("if (-not $KeepUserData)"):
            guard_indent = len(line) - len(line.lstrip())
        if restore_indent is None and stripped.startswith("if ($configBackup"):
            restore_indent = len(line) - len(line.lstrip())
    assert guard_indent is not None, "找不到 -KeepUserData 判断"
    assert restore_indent is not None, "找不到恢复用的 if"
    assert restore_indent <= guard_indent, (
        f"恢复配置必须写在 -KeepUserData 判断**之外**："
        f"判断缩进 {guard_indent}、恢复缩进 {restore_indent} —— 恢复缩进更深，"
        "说明它被塞进了某个分支里，带 -KeepUserData 跑一次照样会把哨兵留在用户设置上"
    )
    return "安装验收脚本会备份并恢复真实 config.json（备份先于覆盖、恢复无条件执行）"


def t_child_process_guard() -> str:
    """应用退出后，自己起的子进程不能继续跑（用户实测反馈的 Bug）。

    为什么必须钉住：编译模型会起 `openscad.exe`。用户在编译期间关窗口（或从任务管理器
    结束进程）时，Python 死了而子进程**不会跟着死** —— 软件关了 OpenSCAD 还在后台算完，
    占满一个核，有时还留下半截 STL，下次打开看到"凭空出现"的模型。

    本项目的退出路径还是 `runtime_state.force_exit()` → `os._exit()`，
    **故意跳过** atexit/finally，所以"退出时清理"的写法根本靠不住。
    这里验证的是四层兜底各自真的成立：
      1. Windows 作业对象带 `KILL_ON_JOB_CLOSE`（内核保证：进程一死子进程全灭）；
      2. 登记表 + `terminate_all()` 能真的杀掉子进程；
      3. 超时路径不留孤儿；启动清扫**只**杀我们自己装的 OpenSCAD；
      4. `process_alive()` 这个“只问不动”的原语真的只问不动 —— 探一下不会把对方打死（见用例 5 注释）。
    """
    import subprocess as sp
    import sys
    import time as time_mod

    from aiforge import procguard

    # 1) 作业对象：Windows 上必须真的建立起来，并且 KillOnJobClose 已生效
    active = procguard.install()
    if os.name == "nt":
        assert active, "Windows 上作业对象必须可用（否则退出后子进程会变孤儿）"
        assert procguard.job_object_active()
        # 把作业的限制标志读回来确认，而不是只信我们"设过"
        import ctypes as ct

        kernel32 = ct.WinDLL("kernel32", use_last_error=True)
        kernel32.QueryInformationJobObject.restype = ct.c_int
        kernel32.QueryInformationJobObject.argtypes = [ct.c_void_p, ct.c_int, ct.c_void_p, ct.c_uint32, ct.c_void_p]
        info = procguard._ExtendedLimitInformation()  # noqa: SLF001 - 自检直接查内部句柄
        ok = kernel32.QueryInformationJobObject(
            ct.c_void_p(procguard._job_handle),  # noqa: SLF001
            procguard._JobObjectExtendedLimitInformation,  # noqa: SLF001
            ct.byref(info),
            ct.sizeof(info),
            None,
        )
        assert ok, "读回作业对象限制失败"
        assert info.BasicLimitInformation.LimitFlags & 0x2000, (
            f"KillOnJobClose 没设上（LimitFlags={info.BasicLimitInformation.LimitFlags:#x}）"
        )

    # 2) 登记表 + 退出时回收：起一个长睡眠子进程，terminate_all 必须真的结束它
    sleeper = sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        procguard.register(sleeper)
        time_mod.sleep(0.2)
        assert sleeper.poll() is None, "子进程应当还活着"
        killed = procguard.terminate_all()
        assert killed >= 1, f"应当至少结束 1 个子进程，实际 {killed}"
        deadline = time_mod.time() + 5
        while time_mod.time() < deadline and sleeper.poll() is None:
            time_mod.sleep(0.05)
        assert sleeper.poll() is not None, "terminate_all 之后子进程仍在运行"
    finally:
        if sleeper.poll() is None:
            sleeper.kill()

    # 3) 超时路径：必须抛 TimeoutExpired，并且**不留孤儿**
    procguard.reset_for_tests()
    timed_out = False
    try:
        procguard.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.4,
            stdout=sp.PIPE,
            stderr=sp.STDOUT,
        )
    except sp.TimeoutExpired:
        timed_out = True
    assert timed_out, "超时必须抛 TimeoutExpired（调用方依赖它给用户提示）"

    # 4) 启动清扫只碰"我们装的 OpenSCAD"：把睡眠进程的 PID 写进记录文件，
    #    它的可执行文件是 python.exe → 绝不能被误杀。
    record = ROOT / "build" / "work" / "guard-test" / "children.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    innocent = sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        record.write_text('{"pids": [%d]}' % innocent.pid, encoding="utf-8")
        procguard._record_path = record  # noqa: SLF001 - 自检直接指定记录文件
        managed = ROOT / "build" / "work" / "guard-test" / "runtime" / "openscad"
        swept = procguard.sweep_orphans(managed)
        assert swept == [], f"不该杀非 OpenSCAD 进程，实际杀了 {swept}"
        time_mod.sleep(0.3)
        assert innocent.poll() is None, "用户自己的进程被误杀了"
    finally:
        procguard._record_path = None  # noqa: SLF001
        if innocent.poll() is None:
            innocent.kill()

    # 5) “探测存活”不得弄死被探测的进程，也不得把死的报成活的。
    #    Windows 上最“自然”的写法 `os.kill(pid, 0)` 两边都错：signal 0 在那里就是
    #    CTRL_C_EVENT（实测 `signal.CTRL_C_EVENT == 0`），是往目标所在控制台的整组进程投信号 ——
    #    本机拿它做投毒时，跑它的那条流水线自己被打死了（退出码 0xC000013A）；
    #    而对一个已退出的 pid 它根本不报错（实测返回正常），等于报“还活着”。
    #    所以两条各验一侧：活的 -> 报活且当场没死；死的 -> 必须报死。
    #    另一格判据：“OpenProcess 开得开”不等于活 —— 实测一个已退出（码 7）但还没收尸的
    #    pid 照样开得开句柄，所以必须比对 STILL_ACTIVE。
    probe = sp.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    time_mod.sleep(0.3)
    assert procguard.process_alive(probe.pid) is True, "活着的进程被 `process_alive` 报成不在"
    assert probe.poll() is None, "「探测存活」把被探测的进程弄死了（用户那边就是探一下旧实例，旧实例退出）"
    probe.kill()
    gone_deadline = time_mod.time() + 10
    # 先问“没收尸”的形状（实测：这种 pid 的 OpenProcess 照样开得开），再收尸后再问一次
    while time_mod.time() < gone_deadline and procguard.process_alive(probe.pid):
        time_mod.sleep(0.2)
    assert procguard.process_alive(probe.pid) is False, "已退出的进程仍被报成活着（只看了句柄开不开）"
    probe.wait(timeout=10)  # 收尸：进入“已退出且句柄已释放”的形状
    assert procguard.process_alive(probe.pid) is False, "收尸后仍被报成活着"

    procguard.terminate_all()
    return (
        "退出时不留孤儿：作业对象带 KillOnJobClose、登记表可回收、超时不留孤儿、清扫不误杀、"
        "存活探测只问不动（活的报活且不死、死的报死）"
    )


def t_child_guard_survival() -> str:
    """真把父进程打死，验“该跟着死的死了、不该死的没死”两边各自成立。

    为什么非得做到这一步（起真子进程、真 `os._exit`、真去外面查进程表）：
    `CREATE_BREAKAWAY_FROM_JOB` 那次翻车就是这个形态 —— 代码里"我们设了创建标志"
    句句成立，接口报的 `detached: true` 也是真的，可用户那边：本软件一关，
    刚送过去的 Bambu Studio 跟着没了（本机实测：带标志的子进程仍然在作业里）。
    所以"能不能独立于本软件存活"这类断言，只有把父进程真打死才算验过。
    """
    import json
    import subprocess as sp
    import sys
    import time as time_mod

    from aiforge import procguard

    procguard.install()
    child_src = """
import json, os, sys, time
sys.path.insert(0, {src!r})
from aiforge import procguard

procguard.install()
sleep_cmd = [sys.executable, "-c", "import time; time.sleep(120)"]
worker = procguard.Popen(sleep_cmd)  # 工人：该跟着一起死
free, detached = procguard.spawn_detached(sleep_cmd)  # 用户的程序：不该被带走
print(json.dumps({{"worker": worker.pid, "free": free.pid, "detached": detached}}), flush=True)
time.sleep(1.0)
os._exit(0)  # 与 runtime_state.force_exit() 同一条路：跳过 atexit / finally
"""
    work_dir = ROOT / "build" / "work" / "guard-test"
    work_dir.mkdir(parents=True, exist_ok=True)
    script = work_dir / "spawn-two-children.py"
    script.write_text(child_src.format(src=str(ROOT / "src")), encoding="utf-8")

    out = sp.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        # 必须写死编码并允许替代符：子进程是我们自己的脚本（`aiforge.stdio` 把它的输出
        # 统一成 UTF-8），而 `text=True` 单独用时按**本机 locale** 解码。在中文 Windows 上
        # 就是 cp936，遇到 UTF-8 字节会在线程里抛 UnicodeDecodeError —— 更糟的是 Python
        # 不会把异常抛给调用方，只是把 `out.stdout` 置成 None，于是下面那句
        # “没报出 pid” 报的是解码问题以外的原因，谁都看不出真凶。
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert out.stdout is not None, "子进程 stdout 读不出来（解码失败？这条断言就是防它静默失明）"
    lines = [line for line in out.stdout.splitlines() if line.strip().startswith("{")]
    assert lines, f"子进程没报出 pid（rc={out.returncode}）：{(out.stderr or '')[:300]}"
    pids = json.loads(lines[-1])
    worker_pid, free_pid = int(pids["worker"]), int(pids["free"])
    assert pids["detached"] is True, "送出去的程序必须报 detached=True（现在是问内核问出来的）"

    # 父进程已经硬退出了，现在轮到看两个子进程的真实下场（去外面问进程表，
    # 不信任何一方的说法：这里是父进程外的另一个进程，谁也别想蒙我）
    deadline = time_mod.time() + 15
    worker_gone = free_alive = False
    while time_mod.time() < deadline:
        worker_gone = not _pid_alive(worker_pid)
        free_alive = _pid_alive(free_pid)
        if worker_gone and free_alive:
            break
        time_mod.sleep(0.3)
    try:
        assert free_alive, f"用户的程序 pid={free_pid} 被本软件带走了一起杀了"
        if os.name != "nt":
            # 非 Windows 没有作业对象，工人靠优雅退出时回收（另有一条检查验它）
            procguard._terminate_pid(worker_pid)  # noqa: SLF001 - 实验产物必须收尾
            return f"非 Windows：父进程硬退出后用户的程序 pid={free_pid} 存活（无作业对象可依赖）"
        assert worker_gone, f"工人进程 pid={worker_pid} 没随父进程死掉（作业对象没管住它）"
        return f"父进程硬退出后：工人 pid={worker_pid} 被内核带走，用户的程序 pid={free_pid} 存活"
    finally:
        procguard._terminate_pid(free_pid)  # noqa: SLF001 - 实验产物，收尾不能留


def t_single_instance() -> str:
    """单实例锁：真两进程验收「第二个被拒 / 提示里的 pid 拿得到 / 崩溃后不残留」。

    这条是打包冒烟翻车后补的：控制台打出「AI 3D Forge 已经在运行中（进程号 None）」。
    查下去是同一个函数里的两个结构缺陷（都用真两进程实测复现）：

    1. pid 写在被强制锁住的那一个字节上 —— `msvcrt.locking` 是**强制**锁，别的进程读锁区
       直接 `PermissionError`，于是 `running_pid()` 永远返回 None：一个结构上不可能生效的提示。
       修法是锁只占第 0 字节、pid 从第 1 字节起写（这两格是投毒实测钉住的）。
       读方那两个看起来像必须的写法（`buffering=0`、给 `read` 明确长度）**实测拆掉
       守卫仍绿** —— 因为 `acquire()` 总把文件裁到 `1+len(pid)`；它们是防御，
       不得写成修复（子进程探了 14 种读法：唯一会被弹回的是「有尾巴 + 不带长度」）。
       —— 这条否定结论同时拦住了一个很易犯的错：拿「加了才跑得通」当理由而不真验。
    2. `open(p, "a+b")` 的当前位置是 **EOF 而不是 0**，而 `locking` 从当前位置开始锁 ——
       锁哪一字节取决于锁文件当时多长。实测：残留 30 字节旧 pid 时第二个实例 acquire 成功，
       即两个服务同时起、抢端口、弹两个窗口 —— 这个模块存在的唯一理由没做到。
    3. 上一条的修法留下一个新风险：**pid 挪了位置，旧版写的记录就会被读成「少掉首位数字」
       的数**（本机真残留过 6 字节的 `336456` → 读成 `36456`）。这数可能正好是一个活着的
       外来进程 —— 实测过把这种形状喂进产品代码：`running_pid()` 报出外来 pid，
       `focus_running_window()` 真把那个程序最小化的窗口还原到了前台（用例 3）。
       修法是：认不出是“我们自己”的 pid 就不说、也不用（用例 1 是同一条的正向对照）。

    所以除了三段正向断言，还留一条**空跑对照**：确认从第 0 字节整读真的会失败 ——
    读得动就说明没人持锁，那整条用例只是在自我表扬。它只认 `PermissionError`；
    而「子进程报 ACQUIRED」也不等于前提成立（产品里那条 `except Exception: return True`
    会把「锁文件根本没建出来」吞成成功），所以还要先钉一句锁文件真的存在。
    两处都是本机撞见过一次「pid 读到 None」的红之后补的（那一红未能复现，但
    它暴露的形状是真的：前提没成立时后面的断言只会报一个让人猜不到的原因）。
    顺着那条红查到的是**并发互踩**：沙箱名字先前不带 pid，两条自检同时跑就会互相删
    对方的锁（实测 1 过 2 红），现在沙箱按 pid 分名 —— 验证工具自己不可靠，报出来的红只会误导人。
    """
    import stat
    import subprocess as sp
    import time as time_mod

    from aiforge import paths as paths_mod
    from aiforge import procguard
    from aiforge import single_instance as si

    child_src = (
        "import os, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from aiforge import single_instance as si\n"
        "print('ACQUIRED' if si.acquire() else 'REFUSED', os.getpid(), flush=True)\n"
        "sys.stdin.readline()\n"
        "si.release()\n"
    )
    # 一个只会“把指定字节写进去并锁住第 0 字节”的持有者：用它伪造旧版布局，
    # 而不是在本进程里摆弄锁（本进程要当“被拒的那个第二实例”，拿着锁就没法验）。
    holder_src = (
        "import msvcrt, sys\n"
        "fh = open(sys.argv[1], 'w+b')\n"
        "fh.seek(0)\n"
        "fh.write(sys.argv[2].encode('ascii'))\n"
        "fh.flush()\n"
        "fh.seek(0)\n"
        "msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)\n"
        "print('HELD', flush=True)\n"
        "sys.stdin.readline()\n"
    )
    # 沙箱目录带 pid：同一个固定名字下开两条自检会互踩 —— 开头那一句 `rmtree(work)`
    # 会把另一条**正持着锁**的沙箱删掉。实测（三条 t_single_instance 并发）：1 过 2 红，
    # 红成什么形状看运气（子进程拿不到锁 / 锁文件不见了 → pid 读到 None）。
    # 不修这一条，下次误红还会被当成产品缺陷查一遍。
    work = ROOT / "build" / "work" / f"single-instance-test-{os.getpid()}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    child_script = work / "si_child.py"
    child_script.write_text(child_src, encoding="utf-8")
    holder_script = work / "si_holder.py"
    holder_script.write_text(holder_src, encoding="utf-8")

    old_data_dir = os.environ.get("AIFORGE_DATA_DIR")

    def use_case(name: str) -> Path:
        """把一个用例的数据目录指到独立沙箱（产品代码里路径是缓存的，必须刷）。"""
        case = work / name
        case.mkdir(parents=True, exist_ok=True)
        os.environ["AIFORGE_DATA_DIR"] = str(case)
        paths_mod.paths(refresh=True)
        si._handle = None  # noqa: SLF001 - 用例之间不得残留上一个的句柄
        si._lock_path = None  # noqa: SLF001
        return case

    def spawn_child():
        proc = sp.Popen(
            [sys.executable, str(child_script), str(SRC)],
            stdin=sp.PIPE,
            stdout=sp.PIPE,
            stderr=sp.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        line = proc.stdout.readline().strip()
        return proc, line

    def wait_gone(proc) -> None:
        proc.kill()
        try:
            proc.wait(timeout=15)
        except sp.TimeoutExpired:  # pragma: no cover
            pass

    try:
        # ---- 用例 1：别人正持着锁 ----
        case1 = use_case("holder")
        proc, line = spawn_child()
        assert line.startswith("ACQUIRED"), f"子进程没拿到锁，用例无法开始：{line}"
        child_pid = int(line.split()[-1])
        lock1 = case1 / "app.lock"
        # 子进程说「拿到了」不等于锁真的建起来了：`acquire()` 有一条
        # `except Exception: return True`（宁可放行也不能卡住启动），它连「文件没建出来」
        # 一起吞掉。本用例唯一的凭据是「另一个进程真持着锁」，所以先钉住前提再读 pid。
        assert lock1.is_file(), (
            f"子进程报了 ACQUIRED 却没留下锁文件（{case1}）—— 那是 acquire() 的兜底分支"
            "把失败吞成了成功，不能拿它当前提"
        )
        got = si.running_pid()
        assert got == child_pid, (
            f"「已在运行中」提示里的 pid 是 {got}（子进程真 pid={child_pid}，"
            f"锁文件 {lock1.stat().st_size} 字节）"
        )
        assert si.acquire() is False, "已有实例在跑时第二个实例拿到了锁（会抢端口、弹两个窗口）"
        if os.name == "nt":
            # 空跑对照：只有 `PermissionError` 才算「锁真的被别人拿着」。缺文件 / 别的
            # OSError 都是用例前提坏了 —— 把它们记成通过就是自欺（原先这里捕的是
            # `OSError`，等于把「根本没锁文件」也当成「有人持锁」）。
            try:
                lock1.read_bytes()  # 带缓冲、从第 0 字节读
            except PermissionError:
                pass
            except OSError as exc:
                raise AssertionError(f"读锁文件出了预期外的错（不是被锁住）：{exc!r}") from exc
            else:
                raise AssertionError("从第 0 字节整读竟然成功 → 没人持锁，这条用例是空跑（假安全感）")
        # 被拒那一刻用户看到的那句话（走真路径：report_refusal 而不是拼好的字符串）
        import contextlib
        import io

        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            said = si.report_refusal(headless=True)
        assert str(child_pid) in said, f"提示里没报出对方 pid：{said}"
        assert "None" not in said, f"提示里出现了 None：{said}"
        assert said.strip() in printed.getvalue(), "这句话根本没被写出去（打包版里它就是用户唯一的反馈）"
        # 弹窗条件真值表：错一格要么是验收脚本被模态框卡死，要么是用户依旧看不见
        dialog = si._should_dialog  # noqa: SLF001 - 守卫要验的就是这个决策本身
        assert dialog(frozen=True, has_console=False, headless=False, focused=False) is True
        assert not dialog(frozen=True, has_console=False, headless=True, focused=False), "--server 跑验收时不许弹模态框"
        assert not dialog(frozen=False, has_console=False, headless=False, focused=False), "源码环境不许弹"
        assert not dialog(frozen=True, has_console=True, headless=False, focused=False), "有控制台就别多一个要点的框"
        assert not dialog(frozen=True, has_console=False, headless=False, focused=True), "窗口已经带回前台了，别再弹框"
        assert "None" not in si.refusal_message(None), "拿不到 pid 时不许写「进程号 None」"
        # 唤回窗口这条：没有窗口可找时必须回答 False（答 True 就是「以为做了其实什么都没做」）
        assert si.focus_running_window(0) is False, "对方 pid=0（肯定没有窗口）却报「已带回前台」"
        assert si.focus_running_window(2 ** 30) is False, "一个不存在的 pid 不该被认成已唤回"
        wait_gone(proc)
        # 崩溃/被强杀后锁必须跟着没掉，否则用户从此打不开软件（内核收句柄要时间，轮询 10s）
        recovered = False
        deadline = time_mod.time() + 10
        while time_mod.time() < deadline:
            if si.acquire() is True:
                recovered = True
                break
            si._handle = None  # noqa: SLF001
            time_mod.sleep(0.3)
        assert recovered, "持锁进程被硬杀后锁残留，用户崩溃后再也打不开软件"
        si.release()

        # ---- 用例 2：锁文件里有旧的长残留（锁位飘移的复现形状）----
        case2 = use_case("stale-seed")
        (case2 / "app.lock").write_bytes(b"9" * 30)
        assert si.acquire() is True, "锁文件有旧残留时本实例反而拿不到锁"
        proc2, line2 = spawn_child()
        assert line2.startswith("REFUSED"), (
            f"锁文件有残留时第二个实例照样起来了（两个服务同时跑）：{line2}"
        )
        wait_gone(proc2)
        si.release()

        # ---- 用例 4：打不开锁文件 ≠ 已有实例在跑 ----
        # 排在用例 3 之前：那条在 Windows 分支末尾直接 `return`，放它后面永远跑不到。
        # 旧写法把 open / truncate / write 的 OSError 也 `return False`，于是两种很日常
        # 的形态都会让用户双击后听到「已经在运行中，请切换到已打开的窗口」而
        # 根本没有窗口（退出码还是 0）—— 软件从此打不开，只能手动删掉那个锁。
        # 用例 1 是同一条的反向对照：真有人持锁时必须照样拒，否则就是把互斥关掉了。
        for shape, case_name in (("readonly", "locked-readonly"), ("isdir", "locked-isdir")):
            case4 = use_case(case_name)
            blocker = case4 / "app.lock"
            if shape == "isdir":
                blocker.mkdir()
                note = "app.lock 变成一个目录（同步盘占位/上次异常留下的壳）"
            else:
                blocker.write_bytes(b"\x00" + str(os.getpid()).encode("ascii"))
                os.chmod(blocker, stat.S_IREAD)
                note = "锁文件被置成只读（备份/同步盘/误操作）"
            try:
                assert si.acquire() is True, (
                    f"{note}：被当成「已有实例在跑」，用户再也起不了软件"
                )
            finally:
                if blocker.is_file():
                    os.chmod(blocker, stat.S_IWRITE | stat.S_IREAD)
                si._handle = None  # noqa: SLF001 - 不许把沙箱句柄留给后面的用例

        # ---- 用例 3：旧版布局的残留记录会被错读成「别人的 pid」----
        # 1.0.2 把 pid 写在第 0 字节，本版从第 1 字节读：跨版本时读回来的是
        # 「少掉首位数字」的数（本机真残留过 6 字节的 `336456`）。危险不在数错了，
        # 而在于它可能正好是一个活着的外来进程：本机拿 `charmap.exe` 实测过一次 ——
        # 未修前 `focus_running_window()` 把那个程序最小化的窗口还原到了前台（一个被拒的
        # 第二实例动了用户另一个程序），修后回答 None / False。
        # 用例 1 是同一条的正向对照：真是我们的进程时 pid 必须照样读得到
        # —— 只验“拒外人”不验“认兄弟”，就会把唤窗整个关死也照样绿。
        if os.name == "nt":
            lock3 = use_case("old-layout") / "app.lock"
            # 一个肯定不是我们的活进程（镜像名 `ping.exe`），只为本机回环，不碰外网
            foreign = sp.Popen(
                ["ping", "-n", "30", "127.0.0.1"],
                stdout=sp.DEVNULL,
                stderr=sp.STDOUT,
            )
            holder3 = None
            try:
                record = f"1{foreign.pid}"  # 旧布局：整个数从第 0 字节写起，首位是喂给错读的诱饵
                holder3 = sp.Popen(
                    [sys.executable, str(holder_script), str(lock3), record],
                    stdin=sp.PIPE,
                    stdout=sp.PIPE,
                    stderr=sp.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                held = (holder3.stdout.readline() or "").strip()
                assert held == "HELD", f"持有者没真的锁住第 0 字节，用例无法开始：{held!r}"
                assert si.acquire() is False, "别人正持着锁，本实例却拿到了（前提坏了）"
                # 先证明「错读」真的发生了：不这一步，`running_pid()` 回答 None
                # 可能只是因为根本没读出数 —— 那就是假通过。
                raw = si._recorded_pid(lock3)  # noqa: SLF001 - 要看的就是未经身份过滤的原始读法
                assert raw == foreign.pid, (
                    f"没造出「错读成外来 pid」的形状（读到 {raw}，外来 pid={foreign.pid}，"
                    f"记录 {record}）—— 用例前提坏了"
                )
                assert procguard.process_alive(foreign.pid), "那个外来进程已退了，本用例是空跑"
                assert si.running_pid() is None, f"把别人的 pid 认成了自己的：{si.running_pid()}"
                said3 = si.refusal_message(si.running_pid())
                assert str(foreign.pid) not in said3, f"给用户的那句话里出现了外来 pid：{said3}"
                assert si.focus_running_window() is False, "拿外来 pid 唤了一次窗还报「已带回前台」"
            finally:
                if holder3 is not None:
                    wait_gone(holder3)
                if foreign.poll() is None:
                    foreign.kill()
                try:
                    foreign.wait(timeout=10)
                except sp.TimeoutExpired:  # pragma: no cover
                    pass
            return (
                "真两进程：第二个实例被拒且能报出对方 pid（含被拒那句话真的写出去了、无「None」，"
                "以及唤窗/弹框的决策真值表）；锁文件有旧残留时仍被拒（锁位钉死）；"
                "持锁进程被硬杀后锁可再拿（含「第 0 字节真读不到」的空跑对照）；"
                "锁文件只读/变成目录时放行而不是谎报已在运行；"
                "旧版布局错读出来的外来 pid 不被认成自己（也不拿它唤窗）"
            )
        return (
            "真两进程：第二个实例被拒且能报出对方 pid（含被拒那句话真的写出去了、无「None」，"
            "以及唤窗/弹框的决策真值表）；锁文件有旧残留时仍被拒（锁位钉死）；"
            "持锁进程被硬杀后锁可再拿（含「第 0 字节真读不到」的空跑对照）；"
            "锁文件只读/变成目录时放行而不是谎报已在运行"
        )
    finally:
        if old_data_dir is None:
            os.environ.pop("AIFORGE_DATA_DIR", None)
        else:
            os.environ["AIFORGE_DATA_DIR"] = old_data_dir
        paths_mod.paths(refresh=True)
        si.release()
        si._handle = None  # noqa: SLF001 - 不许把沙箱句柄留给后面的用例
        si._lock_path = None  # noqa: SLF001
        # 自己的沙箱自己收干净（ignore_errors：上面那个被硬杀的子进程可能还挂着句柄）
        shutil.rmtree(work, ignore_errors=True)


def _pid_alive(pid: int) -> bool:
    """问操作系统这个 pid 还在不在（不信任何缓存）。"""
    import subprocess as sp
    import sys

    if sys.platform == "win32":
        # **不解码**，直接在字节里找。原因（实测）：`text=True` 的解码编码跟着本机 locale /
        # `-X utf8` 变，而 tasklist 按 OEM 代码页写（中文版还会把会话名列写成「控制台」），
        # 一但对不上就抛 UnicodeDecodeError，`out.stdout` 变成 None —— 那时这个函数
        # 对任何 pid 都回答「不在」。用在轮询循环里，等于把「工人进程没被杀掉」这种
        # 安全断言变成**假通过**：静默失明最要命的一种方向。
        # CSV 格式给出确定的字段边界，`","<pid>","` 只会命中 PID 列，不会撞上别的数字。
        out = sp.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
        )
        return f'","{pid}","'.encode("ascii") in out.stdout
    try:
        os.kill(pid, 0)  # noqa: S606 - 探测存活的标准写法（signal 0）
        return True
    except OSError:
        return False


def t_no_unguarded_subprocess() -> str:
    """除 `procguard.py` 自己，业务代码不得直接 `subprocess.run/Popen` 派生进程。

    为什么钉这一条：子进程能不能随本软件一起死，完全取决于它有没有被登记并纳入作业。
    只要还有一个地方裸 `subprocess.run`，那份保证就漏了一个洞 —— 而洞只有在
    "用户关了软件后后台还在算"这种现场才会被发现，测试根本追不到。

    只认字面上的 `subprocess.xxx(...)` 不够：改个名 `import subprocess as sp` 就绕过了，
    所以先把本文件里 subprocess 的所有别名收集起来再判。
    """
    import ast

    allowed = {ROOT / "src" / "aiforge" / "procguard.py"}
    bare_spawn_names = {"run", "Popen", "call", "check_call", "check_output"}
    offenders: list[str] = []
    checked = 0
    for path in sorted((ROOT / "src" / "aiforge").rglob("*.py")):
        if path in allowed:
            continue
        checked += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = {"subprocess"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "subprocess":
                        aliases.add(alias.asname or "subprocess")
            elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                for alias in node.names:
                    if alias.name in bare_spawn_names:
                        offenders.append(
                            f"{path.relative_to(ROOT).as_posix()}:{node.lineno} "
                            f"from subprocess import {alias.name}"
                        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in bare_spawn_names
                and isinstance(func.value, ast.Name)
                and func.value.id in aliases
            ):
                offenders.append(
                    f"{path.relative_to(ROOT).as_posix()}:{node.lineno} {func.value.id}.{func.attr}"
                )
    assert not offenders, "这些派生进程没走 procguard（会被本软件遗漏回收）：" + "、".join(offenders)
    # 一什么都没扫到的守卫比没有守卫更糟（假安全感），所以卡住扇区规模
    assert checked >= 20, f"只扫到 {checked} 个模块，明显不对（目录结改了吗？）"
    return (
        f"扫过 {checked} 个模块（含别名）：派生进程全部走 procguard"
        "（工人纳入作业，用户的程序走 spawn_detached）"
    )


def _ast_scopes(tree):
    """把一棵树切成作用域：模块顶层 + 每个函数体（各自，不往上叠加）。"""
    import ast

    yield "<module>", tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node


def _walk_scope(scope):
    """一个作用域内的节点；进到嵌套函数/lambda 就停（那里的变量是另一回事）。"""
    import ast

    stack = [scope]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            stack.append(child)


def _dict_keys_of(node) -> set[str]:
    """从字面量里能静态看出来的 dict 键（`{{...}}` / `dict(a=1)` / `x["a"]`）。"""
    import ast

    keys: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Dict):
            keys.update(
                k.value for k in sub.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
            )
        elif (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "dict"
        ):
            keys.update(kw.arg for kw in sub.keywords or [] if kw.arg)
        elif (
            isinstance(sub, ast.Subscript)
            and isinstance(sub.slice, ast.Constant)
            and isinstance(sub.slice.value, str)
        ):
            keys.add(sub.slice.value)
    return keys


def _keys_from_value(value, helper_keys: dict[str, set[str]]) -> set[str]:
    """`_popen_kwargs()` / `dict(_popen_kwargs())` 这种表达式会展开出哪些键。"""
    import ast

    if not isinstance(value, ast.Call):
        return set()
    func = value.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    keys = set(helper_keys.get(name, set()))
    if name == "dict" and value.args:
        keys |= _keys_from_value(value.args[0], helper_keys)
    return keys


def t_no_kwargs_collision() -> str:
    """同一个调用里，具名参数不得与 `**` 展开出来的键重名。

    实测中过的枪（就在产品代码里，已修）：OpenSCAD 静默安装那一行写成了
    `subprocess.run(..., creationflags=CREATE_NO_WINDOW, **_popen_kwargs())`，
    而 `_popen_kwargs()` 里已经有一份 creationflags —— Python 在参数绑定阶段就
    报 TypeError，安装程序一行都跑不到。它藏在"只有真去装 OpenSCAD 才走得到"
    的分支里，接口测试与 E2E 都碰不到，只能静态钉住。

    中转变量也要认：`kwargs = dict(_popen_kwargs()); ...; run(cmd, timeout=..., **kwargs)`
    是编译那条路径的写法，只看 `**函数()` 会漏掉它。
    """
    import ast

    helper_names = {"_popen_kwargs"}
    sources = {
        path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in sorted((ROOT / "src" / "aiforge").rglob("*.py"))
    }

    # 1) helper 到底给出哪些键：不靠跑一次（跑一次只能拿到当前平台的键集），直接读字面量
    helper_keys: dict[str, set[str]] = {name: set() for name in helper_names}
    for tree in sources.values():
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in helper_names
            ):
                helper_keys[node.name] |= _dict_keys_of(node)
    assert helper_keys["_popen_kwargs"], "没从 `_popen_kwargs()` 里读到任何键 —— 这条守卫什么都没查"
    assert "creationflags" in helper_keys["_popen_kwargs"], (
        f"`_popen_kwargs()` 的键集里没有 creationflags（读到 {sorted(helper_keys['_popen_kwargs'])}）"
        " —— 这条守卫就是冲着重名参数写的，实现改了要同步改这里"
    )

    failures: list[str] = []
    sites = 0
    for path, tree in sources.items():
        rel = path.relative_to(ROOT).as_posix()
        for scope_name, scope in _ast_scopes(tree):
            # 2) 本作用域里的中转变量：`x = dict(helper())` / `x = {{...}}` / `x["k"] = v`
            var_keys: dict[str, set[str]] = {}
            for node in _walk_scope(scope):
                if not isinstance(node, ast.Assign):
                    continue
                keys = _keys_from_value(node.value, helper_keys)
                if isinstance(node.value, (ast.Dict, ast.Call)):
                    keys |= _dict_keys_of(node.value)
                for target in node.targets:
                    if isinstance(target, ast.Name) and keys:
                        var_keys.setdefault(target.id, set()).update(keys)
                    elif (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)
                    ):
                        var_keys.setdefault(target.value.id, set()).add(target.slice.value)
            # 3) 每个调用：把 `**` 展开出的键集拼起来，与具名参数求交
            for node in _walk_scope(scope):
                if not isinstance(node, ast.Call):
                    continue
                expanded: set[str] = set()
                for keyword in node.keywords or []:
                    if keyword.arg is not None:
                        continue
                    expanded |= _keys_from_value(keyword.value, helper_keys)
                    if isinstance(keyword.value, ast.Name):
                        expanded |= var_keys.get(keyword.value.id, set())
                if not expanded:
                    continue
                sites += 1
                given = {keyword.arg for keyword in node.keywords or [] if keyword.arg}
                clash = given & expanded
                if clash:
                    failures.append(f"{rel}:{node.lineno}（{scope_name}）重复传 {sorted(clash)}")
    assert not failures, "具名参数与 **kwargs 重名（参数绑定就会 TypeError）：" + "；".join(failures)
    assert sites >= 4, f"只查到 {sites} 处 `**` 展开，明显不对（守卫没生效？）"
    return f"{sites} 处 `**kwargs` 展开与具名参数零冲突（键集 {sorted(helper_keys['_popen_kwargs'])}）"


def t_subprocess_decoding() -> str:
    """读子进程的文本输出，不得让一次解码失败把结果“说没”了。

    为什么要钉（实测踩过）：`subprocess.run(..., text=True)` 不写 `encoding=` 时，解码
    编码跟着**本机 locale** 走（UTF-8 模式 / `-X utf8` / 区域设置都能改它），而子进程的输出
    编码是另一个参数（`PYTHONIOENCODING`，由 `aiforge.stdio` 统一成 UTF-8）。两个不一致时，
    Python **不会**把 UnicodeDecodeError 抛给调用方，而是报在工作线程里、然后把
    `CompletedProcess.stdout` 置为 None。接下来 `(proc.stdout or "")` 就把它当成“输出为空”。

    最坑的是它不一定报错，会**假通过**：`_pid_alive()` 以前就是 `str(pid) in (out.stdout or "")`，
    一旦 stdout 变 None，它对任何 pid 都回答“进程已死”——“工人进程真的被内核带走了”
    这条安全断言就再也不会红。所以这里除了静态规则，还把“严格解码 → None / errors=
    "replace" → 拿得到文本”真跑一遍当证据（同一个字节流，两种读法，结论摆在一起）。

    规则（只两条，不引入新依赖）：
      1) `text=True` / `universal_newlines=True` 的调用必须同时写 `encoding=` 与 `errors=`
         —— 两个都不许跟着环境变；
      2) 派生的是我们自己的 Python（`[sys.executable, ...]`）时，`encoding` 必须是 `"utf-8"`
         （输出由 `aiforge.stdio` 统一）。
    外部工具（tasklist / git / 切片器）的输出不是 UTF-8：那就**不要转成文本**，直接拿字节
    （见 `_pid_alive`：它扫的是 `out.stdout` 字节，根本不猜编码）。
    """
    import ast
    import subprocess

    spawn_names = {"run", "Popen", "call", "check_call", "check_output"}

    def _call_name(node: ast.Call) -> str:
        func = node.func
        return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")

    def _is_python_child(node: ast.Call) -> bool:
        """argv[0] 是不是 `sys.executable`（包括 `import sys as _sys` 这种改名）。"""
        if not node.args:
            return False
        first = node.args[0]
        if isinstance(first, (ast.List, ast.Tuple)):
            if not first.elts:
                return False
            first = first.elts[0]
        return (
            isinstance(first, ast.Attribute)
            and first.attr == "executable"
            and getattr(first.value, "id", "").rstrip("_").endswith("sys")
        )

    def _scan_decoding(src: str, where: str) -> list[str]:
        """返回命中的问题（带行号）；只看字面关键字，不执行被测代码。"""
        bad: list[str] = []
        # filename 必须给：`ast.parse` 也会像 compile 一样把 SyntaxWarning 直接发到
        # stderr，不给文件名的话报的是 `<unknown>:6`，谁也不知道去哪找（本轮实测）。
        tree = ast.parse(src, filename=where)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in spawn_names:
                continue
            given = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
            if not {"text", "universal_newlines"} & set(given):
                continue
            missing = sorted({"encoding", "errors"} - set(given))
            if missing:
                bad.append(
                    f"第 {node.lineno} 行：text 模式缺 {missing} —— 解码编码/容错策略不得跟着环境变，"
                    "解码失败只会把 stdout 变成 None"
                )
                continue
            if _is_python_child(node):
                value = given["encoding"].value if isinstance(given["encoding"], ast.Constant) else None
                if value != "utf-8":
                    bad.append(
                        f"第 {node.lineno} 行：子进程是我们自己的 Python，encoding 必须是 \"utf-8\""
                        f"（实测为 {value!r}）"
                    )
        return bad

    # 1) 阳性对照：三类违规都要报得出（不拿真实代码当试验品，隔离在字串里）
    planted = "\n".join(
        [
            "import subprocess, sys",
            "subprocess.run([sys.executable, 'x.py'], capture_output=True, text=True)",
            "subprocess.run(['tasklist'], capture_output=True, text=True, encoding='utf-8')",
            "subprocess.run([sys.executable, 'x.py'], capture_output=True, text=True,"
            " encoding='gbk', errors='replace')",
        ]
    )
    hits = _scan_decoding(planted, "<planted>")
    assert len(hits) == 3, f"投毒没报全（{hits}）—— 守卫自己坏了，不报就是全仓都没人看"
    # 2) 阴性对照：写全了的不得报（否则下一步就是被人用 noqa 绕过去）；
    #    字节模式（不转文本）也不得报 —— tasklist 这类工具就该这么读。
    clean = "\n".join(
        [
            "import subprocess, sys",
            "subprocess.run([sys.executable, 'x.py'], capture_output=True, text=True,"
            " encoding='utf-8', errors='replace')",
            "subprocess.run(['tasklist'], capture_output=True)",
            "subprocess.run(['tasklist'], capture_output=True, universal_newlines=True,"
            " encoding='utf-8', errors='replace')",
        ]
    )
    assert not _scan_decoding(clean, "<clean>"), f"写全了却被误报：{_scan_decoding(clean, '<clean>')}"

    # 3) 真扫仓：产品代码 + 工具脚本 + main.py
    offenders: list[str] = []
    text_calls = 0
    corpus = (
        sorted((ROOT / "scripts").rglob("*.py"))
        + sorted((ROOT / "src").rglob("*.py"))
        + [ROOT / "main.py"]
    )
    for path in corpus:
        src = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(ROOT).as_posix()
        for hit in _scan_decoding(src, rel):
            offenders.append(f"{rel}:{hit}")
        tree = ast.parse(src, filename=str(path))
        text_calls += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _call_name(node) in spawn_names
            and {keyword.arg for keyword in node.keywords} & {"text", "universal_newlines"}
        )
    assert text_calls >= 8, f"全仓只数到 {text_calls} 处 text 模式子进程，与实测不符（扫描失效？）"
    assert not offenders, "子进程输出读取不安全：" + "；".join(offenders)

    # 4) 真跑一遍：同一串字节，严格解码得到 None、errors="replace" 拿得到文本。
    #    这一段自己必须合本文件的规则，所以**不能在自检进程里**直接写一个不带 errors=
    #    的调用（那会被上面那条扫到，也刚好说明规则容易违反）：拿一个孙进程去跑两种读法，
    #    结果以 ASCII 行传回来。孙进程里还要把 threading.excepthook 静掉 —— 读者线程的
    #    异常默认直接打到本进程 stderr，那就是上一轮全量自检里“Exception in thread”噪音的来源。
    work_dir = ROOT / "build" / "work" / "guard-test"
    work_dir.mkdir(parents=True, exist_ok=True)
    writer = work_dir / "not-utf8-output.py"
    # GBK 的“有效”四个字节：对 UTF-8 严格解码就是非法序列。
    # 写成 bytes([...]) 而不是直接嵌字节：上一版把裸 GBK 字节写进 .py，Python 先按
    # UTF-8 读源码就拒载了（“误报”的方向反了，验证不到任何东西）。
    writer.write_text(
        "import sys\n"
        "sys.stdout.buffer.write(b'HEAD' + bytes([0xD3, 0xD0, 0xD0, 0xA8]) + b'TAIL')\n",
        encoding="ascii",
    )
    demo_src = (
        "import subprocess, sys, threading\n"
        "threading.excepthook = lambda args: None\n"
        f"W = {str(writer)!r}\n"
        "PY = sys.executable\n"
        "a = subprocess.run([PY, W], capture_output=True, text=True, encoding='utf-8', timeout=60)\n"
        "b = subprocess.run([PY, W], capture_output=True, text=True, encoding='utf-8',"
        " errors='replace', timeout=60)\n"
        "print('STRICT_IS_NONE', a.stdout is None)\n"
        "print('TOLERANT_HEAD', (b.stdout or '').startswith('HEAD'))\n"
        "print('TOLERANT_HAS_REPLACEMENT', '\\ufffd' in (b.stdout or ''))\n"
    )
    demo = subprocess.run(
        [sys.executable, "-c", demo_src],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    evidence = (demo.stdout or "") + (demo.stderr or "")
    assert "STRICT_IS_NONE True" in evidence, (
        f"严格解码居然没把 stdout 变成 None（{evidence!r}）"
        " —— 用例前提变了，这条守卫在看一个不存在的问题"
    )
    assert "TOLERANT_HEAD True" in evidence and "TOLERANT_HAS_REPLACEMENT True" in evidence, (
        f"errors='replace' 没能保住文本：{evidence!r}"
    )
    return (
        f"{text_calls} 处 text 模式子进程全部写明了 encoding+errors；"
        "实测对照：同一字节流严格解码得到 None（静默失明），errors='replace' 得到完整文本"
    )


def t_no_syntax_warnings() -> str:
    """每个会提交的 .py 编译时不得发出任何警告（尤其是 SyntaxWarning）。

    为什么单独钉（本轮实测撞出来的）：一个探针的文档串里写了 Windows 路径
    （`build` 反斜杠 `work`），“反斜杠 + w”不是合法转义 → Python 往 stderr 打
    SyntaxWarning。看上去只是“难看”，实际上三件事都真的碰过：

      * PowerShell 把子进程的 stderr 当错误看待（`NativeCommandError`），验收脚本
        会被一条警告带红；本轮就是靠“stderr 不为空”发现问题的。
      * 自检里多处 `(proc.stderr or "")` 会被拿去拼进断言消息/解析输出，警告混在
        里面就会误判（见 `t_subprocess_decoding`）。
      * “反斜杠 + 字母”少写一个 `r` 本身就是真 bug：写进文档串的这个字面量（反斜杠 d）
        实际得到的是字面 `d`，正则静默改义；Python 3.12 起这是警告，将来会变成 SyntaxError。

    实现只用标准库：`warnings.catch_warnings(record=True)` + `compile()`，不拉 ruff。
    """
    import warnings

    def compile_warnings(src: str, where: str) -> list[str]:
        """编译一份源码，返回它发出的警告文本（不执行它）。"""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                compile(src, where, "exec")
            except SyntaxError as exc:  # 语法错自有「产品代码可编译」那条管，这里不抢话
                return [f"SyntaxError: {exc}"]
        return [f"{item.category.__name__}: {item.message}" for item in caught]

    # 阳性对照：不写 r 前缀的正则字面量必须报（隔离在字串里，不动真代码）
    planted = compile_warnings('pattern = "\\d+\\.\\d+"\n', "<planted>")
    assert planted, "投毒一个非法转义序列居然没报 —— 这条守卫什么都没查"
    # 阴性对照：raw 字符串不得报（否则下一步就是到处加 noqa）
    assert not compile_warnings('pattern = r"\\d+\\.\\d+"\n', "<clean>"), "raw 字符串被误报"

    offenders: list[str] = []
    scanned = 0
    for path in _shipped_text_files():
        if path.suffix != ".py":
            continue
        scanned += 1
        rel = path.relative_to(ROOT).as_posix()
        for warning in compile_warnings(path.read_text(encoding="utf-8", errors="replace"), rel):
            offenders.append(f"{rel}: {warning}")
    assert scanned >= 50, f"只编译了 {scanned} 个 .py，与仓库规模不符（扫描失效？）"
    assert not offenders, "这些文件编译时会报警告：\n" + "\n".join(sorted(offenders))
    return f"{scanned} 个 .py 全部静默编译通过（含双向对照：少个 r 前缀报、raw 字符串不报）"


def t_progress_accuracy() -> str:
    """进度条必须「在动、且不骗人」。

    用户反馈「进度条不太规范，不能体现准确的进度」。旧实现每个阶段只有一个固定百分比
    （78 → 86 → 95），最长的生成阶段干脆是 None（界面显示不定态转圈）——
    既看不出在动，也看不出是不是卡住了。

    现在的规则（这条测试逐条钉住）：
      * 阶段内**按时间推进**，长阶段也在动；
      * 百分比**单调不减**，切阶段不往回退；
      * **永不越过阶段上限**，只有真正 done 才是 100%；
      * 「等待你确认」的阶段区间起点=终点，**故意不涨** —— 进度条不能替用户做决定。
    """
    import asyncio
    import contextlib
    import inspect
    import time
    import types

    from aiforge import jobs as jobs_mod

    job = jobs_mod.create_job(project_id="progress-test", revision=1, mode="create")
    try:
        job.set_stage("generating_scad")
        assert job.percent is not None, "最长的生成阶段不能是不定态（用户看不到进度）"
        floor, ceiling = jobs_mod.STAGE_BANDS["generating_scad"][1:]
        assert float(job.percent) == floor, (job.percent, floor)

        seen = [float(job.percent)]
        for _ in range(6):
            job.stage_started_at -= 3.0  # 模拟时间流逝
            job.creep()
            seen.append(float(job.percent))
        assert seen == sorted(seen), f"进度必须单调不减：{seen}"
        assert max(seen) > seen[0], f"时间过去之后必须真的在动：{seen}"
        assert max(seen) <= ceiling, f"不许越过阶段上限 {ceiling}：{seen}"

        job.set_stage("compiling")
        assert float(job.percent) >= max(seen), "切阶段不能让进度条往回退"

        # 等人确认：起点=终点，且 creep 明确返回 False
        job.set_stage("awaiting_confirmation")
        before = float(job.percent)
        for _ in range(5):
            job.stage_started_at -= 10.0
            assert job.creep() is False, "等待用户确认时进度条不该自己往前爬"
        assert float(job.percent) == before

        # 只有 done 才是 100%：其它阶段的终点都必须留余量
        for stage, (_text, _start, end) in jobs_mod.STAGE_BANDS.items():
            if stage in ("done", "awaiting_confirmation"):
                continue
            assert end < 100.0, f"{stage} 的进度上限必须留余量，实际 {end}"
        job.set_stage("done")
        assert float(job.percent) == 100.0

        # 真实时钟推进：不靠改时间戳，sleep 之后必须真的往前走
        real = jobs_mod.create_job(project_id="progress-real", revision=1, mode="create")
        real.set_stage("generating_scad")
        real_before = float(real.percent)
        time.sleep(0.4)
        assert real.creep() is True, "真实时间过去后 creep() 必须报告有变化"
        assert float(real.percent) > real_before
        real.status = "done"
    finally:
        job.status = "done"

    # 推进循环必须真的接在生成流程上（光有 creep 没人调，界面照样不动）
    run_src = inspect.getsource(jobs_mod.GenerationRunner.run)
    assert "create_task(self._tick_progress())" in run_src, "生成流程必须启动阶段内推进循环"
    assert "ticker.cancel()" in run_src, "作业结束必须停掉推进循环，否则会继续改 percent"

    # 推进循环本身：用真实的 Job 跑一圈（间隔临时调小，逻辑与线上是同一份代码）
    async def _drive_ticker() -> float:
        ticked = jobs_mod.create_job(project_id="progress-tick", revision=1, mode="create")
        ticked.set_stage("generating_scad")
        original = jobs_mod.PROGRESS_TICK_S
        jobs_mod.PROGRESS_TICK_S = 0.05
        task = asyncio.create_task(
            jobs_mod.GenerationRunner._tick_progress(types.SimpleNamespace(job=ticked))
        )
        try:
            await asyncio.sleep(0.35)
        finally:
            jobs_mod.PROGRESS_TICK_S = original
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        ticked.status = "done"
        return float(ticked.percent)

    ticked_percent = asyncio.run(_drive_ticker())
    assert ticked_percent > jobs_mod.STAGE_BANDS["generating_scad"][1], (
        f"推进循环跑了 0.35 秒，进度却还停在区间起点：{ticked_percent}"
    )
    return "阶段内按时间推进、单调不减、不越界、等待确认不涨、只有 done 是 100%、推进循环真实运行"


def t_network_errors() -> str:
    """网络失败必须「说人话」且「重试得安全」。

    这条来自一次真实的端到端翻车：系统代理抖动时抛出的 httpx 异常 `str()` 是空字符串，
    界面上于是显示「网络请求失败：」—— 冒号后面什么都没有，用户既不知道是不是自己
    断网了，也没法向客服描述。

    更要紧的是重试边界：只有「连接根本没建立」（ConnectError / ConnectTimeout）才能
    重试 —— 那时请求没发出去，重试不会重复计费；而 `ReadError` /
    `RemoteProtocolError` 是「请求已发出、响应断了」，盲目重放可能让用户为同一句话
    付两次钱，也会让界面出现重复内容。所以这条测试**同时**钉住这两个方向。
    """
    import asyncio

    import httpx

    from aiforge import llm

    real_client = httpx.AsyncClient
    real_delay = llm.CONNECT_RETRY_DELAY_S
    llm.CONNECT_RETRY_DELAY_S = 0.01

    def raise_on_call(exc: Exception):  # type: ignore[no-untyped-def]
        def _raise(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise exc

        return _raise

    class FakeClient:
        """替身：只统计被创建/被调用了几次，不真的联网。"""

        calls = 0
        next_exc: Exception | None = None

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *_exc):  # type: ignore[no-untyped-def]
            return False

        async def post(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            FakeClient.calls += 1
            assert FakeClient.next_exc is not None
            raise FakeClient.next_exc

    class FakeStreamResponse:
        status_code = 200

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *_exc):  # type: ignore[no-untyped-def]
            return False

        async def aread(self) -> bytes:
            return b""

        async def aiter_lines(self):  # type: ignore[no-untyped-def]
            yield 'data: {"choices":[{"delta":{"content":"你好"}}]}'
            raise httpx.ReadError("")

    class FakeStreamClient(FakeClient):
        streams = 0

        def stream(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            FakeStreamClient.streams += 1
            return FakeStreamResponse()

    def run_chat(client):  # type: ignore[no-untyped-def]
        return asyncio.run(client.chat([{"role": "user", "content": "hi"}]))

    httpx.AsyncClient = FakeClient  # type: ignore[misc]
    try:
        client = llm.DeepSeekClient("sk-selftest", base_url="https://api.example.invalid")

        # 1) 连接阶段失败：重试一次（共 2 次尝试），报错里必须带上异常类型
        FakeClient.calls = 0
        FakeClient.next_exc = httpx.ConnectError("")
        try:
            run_chat(client)
            raise AssertionError("连不上服务器时必须抛 LlmError")
        except llm.LlmError as exc:
            assert exc.code == "API_UNREACHABLE", exc.code
            assert "ConnectError" in exc.message, f"报错必须说清失败类型：{exc.message!r}"
            assert "api.example.invalid" in exc.message, f"报错必须带目标地址：{exc.message!r}"
        assert FakeClient.calls == 2, f"连接阶段应重试一次（共 2 次尝试），实际 {FakeClient.calls}"

        # 2) 请求已发出后断流：**绝不重试**（防止重复计费 / 重复内容）
        FakeClient.calls = 0
        FakeClient.next_exc = httpx.RemoteProtocolError("")
        try:
            run_chat(client)
            raise AssertionError("断流时必须抛 LlmError")
        except llm.LlmError as exc:
            assert exc.code == "API_UNREACHABLE", exc.code
            assert "RemoteProtocolError" in exc.message, exc.message
        assert FakeClient.calls == 1, f"已发出的请求不许重放，实际发了 {FakeClient.calls} 次"

        # 3) 流式：已经开始吐字之后再断，同样不许从头重放
        httpx.AsyncClient = FakeStreamClient  # type: ignore[misc]
        FakeStreamClient.streams = 0
        stream_client = llm.DeepSeekClient("sk-selftest", base_url="https://api.example.invalid")

        async def consume() -> list[str]:
            chunks: list[str] = []
            async for piece in stream_client.chat_stream([{"role": "user", "content": "hi"}]):
                chunks.append(piece)
            return chunks

        try:
            asyncio.run(consume())
            raise AssertionError("流中断时必须抛 LlmError")
        except llm.LlmError as exc:
            assert exc.code == "API_UNREACHABLE", exc.code
            assert "ReadError" in exc.message, exc.message
        assert FakeStreamClient.streams == 1, (
            f"已经收到内容之后绝不能再发一次请求，实际发了 {FakeStreamClient.streams} 次"
        )
    finally:
        httpx.AsyncClient = real_client  # type: ignore[misc]
        llm.CONNECT_RETRY_DELAY_S = real_delay

    # 空 `str()` 的异常也必须给出可读原因（用异常类型兜底）
    assert llm._describe_network_error(httpx.ReadError("")).strip()  # noqa: SLF001
    assert "ReadError" in llm._describe_network_error(httpx.ReadError(""))  # noqa: SLF001
    assert "超时" in llm._describe_network_error(httpx.ConnectTimeout("超时了"))  # noqa: SLF001
    return "连接阶段重试 1 次、断流不重放、报错带异常类型与目标地址"


def t_child_guard_wiring() -> str:
    """**接线**测试：应用启动时到底有没有把子进程守护装上。

    为什么单列一条：原来只有 `t_child_process_guard` 验证 procguard 模块本身
    （直接调 `install()` 当然过），没人验证"应用启动路径"。结果
    `server.start_server()` 里写的是 `files.local_dir()` —— `AppPaths` 上根本没有
    这个字段，每次启动都抛 AttributeError，被 except 吞成一行 warning，
    **守护从未真正生效**。用户反馈的"关掉软件 OpenSCAD 还在跑"因此只修了一半。

    这条测试用真实入口跑一遍，并断言 start_server 仍然会调它（防止以后被重构掉）。
    """
    import inspect

    from aiforge import procguard, server as srv

    procguard.reset_for_tests()
    active = srv.install_child_guard()
    if os.name == "nt":
        assert active, "Windows 上必须能装上作业对象守护（否则退出会残留子进程）"
        assert procguard.job_object_active(), "守护函数返回成功但作业对象没生效"
    else:  # pragma: no cover - CI 可能跑在 Linux 上
        assert active is False, "非 Windows 上应当明确返回 False，而不是假装成功"

    source = inspect.getsource(srv.start_server)
    assert "install_child_guard()" in source, "start_server 必须调用 install_child_guard()"
    # 命令行入口（--check / --install-openscad 等）也要装：不然「系统诊断」里那条
    # 「后台进程守护」会显示成"当前环境不支持"（明明只是我们自己没装）—— 实测踩过。
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "install_child_guard()" in main_src, "main.py 的所有入口都必须先装好子进程守护"

    # 守护状态要能在「系统诊断」里看见 —— 静默降级等于没有
    from aiforge import envcheck

    item = envcheck.check_child_guard()
    assert item.id == "child_guard", item.id
    assert item.status in ("ok", "warn"), item.status
    assert "child_guard" in envcheck.CHECK_IDS, "诊断报告里必须有这一项"
    return f"启动即装好守护（active={active}），且能在系统诊断里看到"


def t_install_swap_rollback() -> str:
    """换安装目录必须「可回滚」，改名必须容忍短暂占用。

    这条来自一次真实的端到端失败：安装流程先在旁路目录 `.new` 里跑一次
    `openscad.exe` 冒烟测试，紧接着就把该目录改名到正式位置 —— Windows 在
    进程退出后仍会短暂持有刚运行过的 exe 映像，`rename` 于是报
    `[WinError 5] 拒绝访问`。旧实现是「先 rmtree(正式目录) 再 rename」，
    这一次失败就把**用户手里那份能用的 OpenSCAD 删掉了**，
    而本文件上面刚写过"一次失败的重新安装把可用环境砸掉，是最不该发生的退化"。
    """
    import tempfile
    from pathlib import Path

    from aiforge import openscad

    def make_install(root: Path, marker: str) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        target = root / "openscad"
        (target / "openscad-2021.01").mkdir(parents=True)
        (target / "openscad-2021.01" / "openscad.exe").write_text(marker, encoding="utf-8")
        return target

    with tempfile.TemporaryDirectory(prefix="swap_") as tmp:
        root = Path(tmp)

        # 1) 正常替换：新目录就位、旁路目录消失、备份不残留
        case1 = root / "case1"
        target = make_install(case1, "OLD")
        staging = case1 / "openscad.new"
        (staging / "openscad-2021.01").mkdir(parents=True)
        (staging / "openscad-2021.01" / "openscad.exe").write_text("NEW", encoding="utf-8")
        openscad._swap_install_dir(staging, target)  # noqa: SLF001
        assert (target / "openscad-2021.01" / "openscad.exe").read_text(encoding="utf-8") == "NEW"
        assert not staging.exists(), "旁路目录应已被换走"
        assert not (case1 / "openscad.old").exists(), "备份目录不该残留"

        # 2) 换入失败：**原来的目录必须还在**（回滚），一个文件都不能少
        case2 = root / "case2"
        target = make_install(case2, "OLD2")
        staging = case2 / "openscad.new"
        (staging / "openscad-2021.01").mkdir(parents=True)
        (staging / "openscad-2021.01" / "openscad.exe").write_text("NEW2", encoding="utf-8")

        real_rename = Path.rename
        calls: list[tuple[str, str]] = []

        def flaky_rename(self, dst):  # type: ignore[no-untyped-def]
            calls.append((self.name, Path(dst).name))
            # 只拦「旁路 → 正式」这一步，且永久失败（模拟占用一直不释放）
            if self.name == "openscad.new" and Path(dst).name == "openscad":
                raise PermissionError(5, "拒绝访问")
            return real_rename(self, dst)

        Path.rename = flaky_rename  # type: ignore[method-assign]
        try:
            try:
                openscad._swap_install_dir(staging, target)  # noqa: SLF001
                raise AssertionError("换入失败时必须抛错，不能假装成功")
            except OSError:
                pass
        finally:
            Path.rename = real_rename  # type: ignore[method-assign]

        assert (target / "openscad-2021.01" / "openscad.exe").read_text(encoding="utf-8") == "OLD2", (
            "换入失败之后，用户原来那份能用的 OpenSCAD 必须还在"
        )
        attempts = [c for c in calls if c[0] == "openscad.new"]
        assert len(attempts) >= 2, f"改名必须重试（实测会撞上短暂占用），实际尝试 {len(attempts)} 次"

        # 3) 短暂占用（前两次失败、第三次成功）必须最终成功 —— 这正是真机上的形态
        case3 = root / "case3"
        target = make_install(case3, "OLD3")
        staging = case3 / "openscad.new"
        (staging / "openscad-2021.01").mkdir(parents=True)
        (staging / "openscad-2021.01" / "openscad.exe").write_text("NEW3", encoding="utf-8")
        seen = {"n": 0}

        def transient_rename(self, dst):  # type: ignore[no-untyped-def]
            if self.name == "openscad.new" and Path(dst).name == "openscad":
                seen["n"] += 1
                if seen["n"] <= 2:
                    raise PermissionError(5, "拒绝访问")
            return real_rename(self, dst)

        Path.rename = transient_rename  # type: ignore[method-assign]
        try:
            openscad._swap_install_dir(staging, target)  # noqa: SLF001
        finally:
            Path.rename = real_rename  # type: ignore[method-assign]
        assert seen["n"] == 3, f"重试次数不对：{seen['n']}"
        assert (target / "openscad-2021.01" / "openscad.exe").read_text(encoding="utf-8") == "NEW3"

    # 这条守卫本身也要防退化：安装流程里不能再出现「先删正式目录再改名」的写法
    import inspect

    source = inspect.getsource(openscad.install_portable)
    assert "shutil.rmtree(target_dir, ignore_errors=True)\n        staging.rename" not in source, (
        "不许先删正式目录再改名：改名失败会让用户的 OpenSCAD 彻底消失"
    )
    assert "_swap_install_dir(staging, target_dir)" in source, "安装流程必须走可回滚的换目录"
    return "换目录可回滚（失败时原环境完好）、改名退避重试、正常替换无残留"


def t_parts_library() -> str:
    """固定零件库：每个零件都要真实编译并实测（封闭 / 单一实体 / 尺寸对）。

    为什么这条必须存在：零件库里放的是**手写几何**（螺旋螺纹），
    这类几何一旦在 OpenSCAD 里镶嵌失败，用户拿到的是残缺或拧不上的模型，
    而 OpenSCAD 的退出码仍然是 0 —— 不实测就发现不了。
    参考 earthtojake/text-to-cad 的做法：标准件不现画，而是从**验证过的零件库**里选。
    """
    import subprocess as _subprocess

    from aiforge.parts import catalog as parts_lib

    # 库文件必须真实存在，且目录可被 OPENSCADPATH 引用
    library = parts_lib.library_path()
    assert library.is_file(), f"固定零件库缺失：{library}"
    text = library.read_text(encoding="utf-8")
    for spec in parts_lib.PARTS:
        assert f"module {spec.module}(" in text, f"零件库缺少模块 {spec.module}"

    # 安全例外必须**只**对该文件名生效（不能变成"放开 use"）
    from aiforge import scad as scad_mod

    assert scad_mod.validate(f"use <{parts_lib.LIBRARY_NAME}>\n{parts_lib.PARTS[0].usage.splitlines()[-1]}\n").ok, (
        "自带零件库的 use 应当被允许"
    )
    for blocked in (
        "use <threads.scad>\ncube(1);",
        "use <../aiforge_parts.scad>\ncube(1);",
        "use <C:/x/aiforge_parts.scad>\ncube(1);",
        "include <aiforge_parts.scad>\ncube(1);",
    ):
        assert not scad_mod.validate(blocked).ok, f"这个引用必须被拦截：{blocked!r}"

    # 需求匹配：用户问滤镜转接环时必须命中 filter_adapter
    matched = parts_lib.match("设计一个 72mm 转 67mm 的滤镜转接环，M72×0.75 外螺纹转 M67×0.75 内螺纹")
    assert matched and matched[0].id == "filter_adapter", [p.id for p in matched]
    block = parts_lib.prompt_block(matched, "72mm 转 67mm 滤镜转接环")
    assert "use <aiforge_parts.scad>" in block and "filter_adapter" in block, block[:200]
    # 无关需求不该被塞零件块（否则每次生成都要多花 token）
    assert not parts_lib.match("设计一个桌面手机支架，高度 100mm"), "无关需求不应命中零件库"

    # 单字关键词的阴性对照（实测踩到）：「掩盖瑕疵」「覆盖所有表面」这种散文用法
    # 以前会因为命中「盖」这一个字而被当成要设计螺纹盖，把零件块塞进毫不相关的提示词。
    # 中文单字必须写成复合词（瓶盖/镜头盖/盖子…）才能当关键词，否则就是在骗人：
    # llm._parts_block_for 自己的注释写的是「prompt 只在真正相关时才变长」。
    assert not parts_lib.match("帮我掩盖一下表面瑕疵"), "「掩盖」的「盖」不是盖子"
    assert not parts_lib.match("这个设计要覆盖所有表面，不留缝隙"), "「覆盖」的「盖」不是盖子"
    # 复合词必须仍能命中，防止「一刀切删关键词」把召回率也删没了
    assert any(p.id == "threaded_cap" for p in parts_lib.match("设计一个饮料瓶盖，内径 28mm")), "瓶盖必须命中螺纹盖"
    assert any(p.id == "threaded_cap" for p in parts_lib.match("做一个相机镜头盖")), "镜头盖必须命中螺纹盖"

    proc = _subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_parts.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, "固定零件库实测未通过：\n" + output.strip()[-900:]
    checked = output.strip().splitlines()[-1] if output.strip() else ""
    return checked or f"{len(parts_lib.PARTS)} 个零件全部通过实测"


def t_deterministic_part() -> str:
    """标准零件确定性直出：需求明确时程序直接生成已验证几何，不靠 AI 填参。

    为什么必须有：用户反馈「67 转 77 滤镜环始终做不对」——AI 填参/手写几何
    都可能错（口径填反、手写螺纹不封闭）。直出把口径映射与几何都交给程序，
    这里钉住三件事：映射正确、语序含糊时回退 AI、直出源码真编译且几何正确。
    """
    import math as _math
    import tempfile as _tempfile
    from pathlib import Path as _Path

    from aiforge import compiler as _compiler, mesh as _mesh, scad as _scad
    from aiforge.parts import catalog as parts_lib

    # 1) 明确转换 → 命中 filter_adapter 且口径映射正确（male=67, female=77）
    call = parts_lib.extract_call("建模一个67mm转77mm的滤镜转接环")
    assert call is not None, "明确的 67 转 77 应当能直出"
    part, values = call
    assert part.id == "filter_adapter", part.id
    assert values["male_d"] == 67 and values["female_d"] == 77, values

    # 2) 语序含糊（两个口径之间夹别的词）→ 必须回退 AI，不能猜
    assert parts_lib.extract_call("77mm滤镜用在67mm镜头上") is None, "语序含糊必须回退 AI"
    # 3) 无关需求 → 不直出
    assert parts_lib.extract_call("设计一个桌面手机支架，高度 100mm") is None
    # 3b) 用户显式给的总高必须被尊重（否则直出会 silently 用默认高）
    with_h = parts_lib.extract_call("建模一个67mm转77mm的滤镜转接环，高10mm")
    assert with_h is not None and with_h[1]["height"] == 10, with_h
    # 3c) 非标准螺距 → 回退 AI；标准螺距 → 仍可直出
    assert parts_lib.extract_call("67转77 螺距1.0") is None, "非标准螺距必须回退 AI"
    assert parts_lib.extract_call("M67×0.75转M77×0.75 转接环") is not None, "标准螺距应可直出"

    # 4) 直出源码：过安全校验 + 真编译 + 几何正确（水密/单件/尺寸/镂空）
    source = parts_lib.render_source(part, values, name="67转77滤镜转接环", summary=part.summary)
    validation = _scad.validate(source)
    assert validation.ok, validation.issues
    if not _openscad_ready():
        # 上面那些（映射/回退/安全校验）都不依赖环境，已经跑完了；
        # 真编译这一段在没装 OpenSCAD 的机器上验不了，跳过并说原因，
        # 不拿「本机没装引擎」当产品缺陷报（CI 与新 clone 都会被这条逼红）。
        raise SkipSelfcheck("几何与尺寸那一段需要 OpenSCAD 真编译（本机未安装；映射/回退/校验已验完）")
    with _tempfile.TemporaryDirectory(prefix="det_") as tmp:
        res = _compiler.compile_to_stl(source, _Path(tmp), basename="model")
        assert res.ok and res.stl_path, res.message or res.fatal
        m = _mesh.analyze(_Path(res.stl_path))
        assert m.watertight and m.solid_parts == 1, (m.watertight, m.solid_parts)
        assert abs(m.bbox[0] - 81.7) <= 0.7 and abs(m.bbox[2] - 8.4) <= 0.7, m.bbox
        verts, _ = _mesh.read_triangles(_Path(res.stl_path))
        radii = [_math.hypot(p[0], p[1]) for p in verts]
        assert min(radii) / max(radii) > 0.3, "中心必须镂空"
        # 螺纹度量必须朝向鲁棒：翻转后的升口径环也应量出真实牙深(~0.46)，
        # 而非固定单带跨到挡肩过渡区量出的假值(~2.46)。
        depth = _mesh.measure_thread_depth(verts)
        assert depth is not None and 0.3 <= depth <= 1.0, f"牙深应≈0.46，实测 {depth}"
    return "明确转换直出已验证几何；语序含糊/无关需求正确回退 AI"


def t_glm_provider() -> str:
    """提供商感知：GLM/智谱 与 DeepSeek 的视觉模型、输出预算、错误码翻译各自正确。

    为什么必须有：图片复核固定用一个视觉模型，而智谱 GLM-4V-Flash 的 max_tokens
    硬上限 1024（超出报 1210）、模型名也不同；把 DeepSeek 那套 8192 直接发过去必然报错。
    实测 glm-4.7-flash 高峰期返 429/1305（瞬态过载），错误码必须翻译成人话。
    """
    from aiforge import llm as llm_mod

    assert llm_mod.detect_provider("https://api.deepseek.com") == "deepseek"
    assert llm_mod.detect_provider("https://open.bigmodel.cn/api/paas/v4") == "glm"
    assert llm_mod.detect_provider("https://example.com/v1") == "openai"
    glm_url = "https://open.bigmodel.cn/api/paas/v4"
    # 智谱国际站（api.z.ai）必须认出来是 glm：以前 `config.provider_of_base` 与
    # `llm.GLM_BASE_HINTS` 各养一张域名表、两张都漏了这个，国际站用户被当成
    # 自定义 openai 服务，拿到 DeepSeek 的视觉模型名与 token 预算，全线报错。
    # 现在两张表合并到一处（detect_provider 只是 config.provider_of_base 的薄包装），
    # 加一家只需改一处；这条断言防的就是「漂回去变成两张表各自演化」。
    assert llm_mod.detect_provider("https://api.z.ai/api/paas/v4") == "glm", "智谱国际站必须被认成 glm"
    # 提供商名字/注册地址也不能写死：自定义网关用户收到 401 时，界面以前会说
    # 「DeepSeek API Key 无效…可在 platform.deepseek.com 创建」（指引方向完全错）。
    custom_url = "https://ai.corp.example/v1"
    e = llm_mod._friendly_http_error(401, "", custom_url)
    assert "DeepSeek" not in e.message and "deepseek" not in (e.hint or ""), (e.message, e.hint)
    assert "AI 服务" in e.message, e.message
    # 404 对自定义网关也不该把用户赶回 DeepSeek，而该提示他核对路径与模型名
    e = llm_mod._friendly_http_error(404, "", custom_url)
    assert "api.deepseek.com" not in (e.hint or ""), e.hint
    assert "自定义" in (e.hint or ""), e.hint
    # 1000（UNKNOWN，智谱服务端内部错误）不能被当成「API Key 无效」：
    # 以前跟 1001/1002/1004 一起归进鉴权名单，智谱侧偶发内部故障会把用户
    # 引去删 Key/重建/重新粘贴，做什么都是白折腾。
    e = llm_mod._friendly_http_error(400, '{"error":{"code":"1000"}}', glm_url)
    assert e.code == "API_UNREACHABLE", (e.message, e.code)
    assert "Key 无效" not in e.message, e.message
    # 1001/1002/1004 才是真的鉴权类，仍必须报成 Key 无效（不能因上一条把方向修反）
    e = llm_mod._friendly_http_error(400, '{"error":{"code":"1001"}}', glm_url)
    assert e.code == "API_KEY_INVALID", e.code
    # 视觉模型随提供商切换
    assert llm_mod.vision_model_for("https://open.bigmodel.cn/api/paas/v4") == "glm-4v-flash"
    assert llm_mod.vision_model_for("https://api.deepseek.com") == "deepseek-flash"
    # 输出预算：智谱视觉硬上限 1024；DeepSeek 保持 8192/16384
    assert llm_mod.vision_max_tokens_for("https://open.bigmodel.cn/x") == (1024, 1024)
    assert llm_mod.vision_max_tokens_for("https://api.deepseek.com") == (8192, 16384)
    e = llm_mod._friendly_http_error(429, '{"error":{"code":"1305"}}', glm_url)
    assert "访问量过大" in e.message and e.code == "API_UNREACHABLE", e.message
    e = llm_mod._friendly_http_error(400, '{"error":{"code":"1113"}}', glm_url)
    assert "余额不足" in e.message, e.message
    e = llm_mod._friendly_http_error(401, '{"error":{"code":"1001"}}', glm_url)
    assert e.code == "API_KEY_INVALID", e.code
    # 429 限流文案随提供商切换（不能对 GLM 用户说「被 DeepSeek 限流」）
    e = llm_mod._friendly_http_error(429, '{"error":{"code":"1130"}}', glm_url)
    assert "智谱 GLM" in e.message and "限流" in e.message, e.message
    e = llm_mod._friendly_http_error(429, "", "https://api.deepseek.com")
    assert "DeepSeek" in e.message, e.message
    # 5xx / 404 文案也随提供商切换
    e = llm_mod._friendly_http_error(500, "", glm_url)
    assert "智谱 GLM" in e.message, e.message
    e = llm_mod._friendly_http_error(404, "", glm_url)
    assert "open.bigmodel.cn" in (e.hint or ""), e.hint
    e = llm_mod._friendly_http_error(500, "", "https://api.deepseek.com")
    assert "DeepSeek" in e.message, e.message
    # 思考模型思维链开关：glm 关闭（烧预算/拖慢），DeepSeek 不动
    g = llm_mod.DeepSeekClient("k", glm_url, "glm-4.7-flash")
    d = llm_mod.DeepSeekClient("k", "https://api.deepseek.com", "deepseek-chat")
    assert g._thinking_control() == {"thinking": {"type": "disabled"}}
    assert d._thinking_control() == {}
    # glm 读超时放大（思考模型首字慢），DeepSeek 保持原值
    assert g.timeout.read > d.timeout.read
    # 视觉复核二级判据：不守首行契约但描述了强问题 → 算报告；纯复述指标 → 无结论
    # （不是「没问题」！这两以前都返回 False，调用方区分不出来；现在必须返回不同常量）
    assert llm_mod.parse_snapshot_verdict("模型整体不错\n但有一根筋悬空在腔体里") == llm_mod.VERDICT_PROBLEM
    assert llm_mod.parse_snapshot_verdict(
        "好的，包围盒 81.7 x 81.7 x 9.0 mm，水密单一实体，可用于 3D 打印。"
    ) == llm_mod.VERDICT_UNKNOWN, "纯复述指标必须是「无结论」，不能等同于「明确没问题」"
    assert llm_mod.parse_snapshot_verdict("结论：无明显问题") == llm_mod.VERDICT_CLEAR
    assert llm_mod.parse_snapshot_verdict("结论：有问题\n底座悬空") == llm_mod.VERDICT_PROBLEM
    assert llm_mod.VERDICT_UNKNOWN != llm_mod.VERDICT_CLEAR, "三态必须互不相同，否则又退回 bool"
    # 同提供商降级：仅瞬态过载/不可用且未降级过时切换，并改写 payload；其余不切
    fb = llm_mod.DeepSeekClient("k", glm_url, "glm-4.7-flash", fallback_model="glm-4-flash")
    payload = {"model": fb.model}
    assert fb._try_fallback(429, payload) is True, "429 应触发降级"  # noqa: SLF001
    assert fb.model == "glm-4-flash" and payload["model"] == "glm-4-flash"
    assert fb._try_fallback(429, payload) is False, "已降级过不应再切"  # noqa: SLF001
    nb = llm_mod.DeepSeekClient("k", glm_url, "glm-4.7-flash")
    assert nb._try_fallback(429, {"model": nb.model}) is False, "没配备用模型不降级"  # noqa: SLF001
    assert nb._try_fallback(404, {"model": nb.model}) is False, "404 不降级"  # noqa: SLF001

    # ---- 「瞬态」判据必须读业务码，不能只看状态码（本轮修的真缺陷）----
    # 以前 `_post_with_connect_retry` 与 `_try_fallback` 各自写了一份「429/5xx 就算瞬态」，
    # 两处都不读 body：智谱 1113（余额/资源包不足）或 OpenAI 兼容网关常见的 insufficient_quota
    # 都会被当成「稍后再试就好」，白等几轮退避/白试一次备用模型，最后才报出真正原因。
    # 判据合并到一处后（`_is_transient_status`），两个调用点共用，不能再漂。
    assert llm_mod._is_transient_status(429, '{"error":{"code":"1305"}}') is True, "1305 过载是真瞬态"
    assert llm_mod._is_transient_status(429, '{"error":{"code":"1130"}}') is True, "1130 频控也是真瞬态"
    assert llm_mod._is_transient_status(429, '{"error":{"code":"1113"}}') is False, (
        "1113 是余额/资源包不足，等一下也没用，不能重试"
    )
    assert llm_mod._is_transient_status(500, '{"error":{"code":"1220"}}') is False, (
        "1220 无权访问该模型，跟服务不可用没关系，换模型重发也白搭"
    )
    assert llm_mod._is_transient_status(429, '{"error":{"type":"insufficient_quota"}}') is False, (
        "OpenAI 兼容网关常用 429 搭配 insufficient_quota 表达配额耗尽，不是「请求太频繁」"
    )
    assert llm_mod._is_transient_status(503, "") is True, "没带业务码的 5xx 仍按瞬态处理（兼容旧行为）"
    assert llm_mod._is_transient_status(400, "anything") is False, "400 本来就不在可重试范围"
    # 反向：同一个 429 状态码，带 1305 与带 1113 的结果必须相反（这就是“读业务码”与
    # “只看状态码”的分水岭；若两处判据重新分家，这条断言不会自动抓，但上面逐条断言会先红）
    assert llm_mod._is_transient_status(429, '{"error":{"code":"1305"}}') != llm_mod._is_transient_status(
        429, '{"error":{"code":"1113"}}'
    ), "同为 429，1305 与 1113 的分流必须相反"

    # DeepSeek 分支不受影响
    e = llm_mod._friendly_http_error(402, "", "https://api.deepseek.com")
    assert "余额不足" in e.message and e.code == "API_KEY_INVALID"
    return "提供商识别/视觉模型/输出预算/错误码翻译 均按提供商正确分流"


def t_hole_measure() -> str:
    """孔径射线实测：有孔量出直径、实心返回 None、**偏心孔也要量对**（HANDOVER P0）。

    阴性对照：实心块必须返回 None —— 否则无法区分「真的量到孔」和「判据放松了」。

    偏心孔这一条是本轮补的：旧实现直接从 `(0,0)` 出发走射线，只对**同心**的
    竖直孔成立。实测两个都是 Ø10 的孔（`cube(center=true)` 减 `cylinder(d=10)`），
    唯一的差别是孔偏心 12mm：
        孔在中心   → 9.8  ✅
        孔偏心 12mm → None ❌  （(0,0) 落在实体里，直接判「这个高度没有孔」）
    后果很重：判据据此报「孔径没改到」，而 AI 其实改对了 —— 用户的正确修改被判失败。
    偏心孔在真实模型里很常见（面板安装孔、阵列孔、`translate([x,y,0]) cylinder`）。

    --- 但光修偏心孔不够：旧守卫自己也是**照不到问题的守卫** ---
    上面那三个用例全部用 `cube(center=true)` + `$fn=64`，面数只有 **272**，
    而当时代码里有个 `_HOLE_AXIS_MAX_FACES = 1200` 的代价上限 —— **上限永远咬不到**，
    所以这条守卫一直是绿的，而用户机器上 14 份真实项目 STL 的面数是 3,041~122,771，
    **14/14 全部超线**：每一份真实模型的孔都被那个过期常数直接判成「量不到」。
    那个 1200 是探测步长还是 0.1mm 那一版量出来的，步长优化到 0.6mm 后没人重取。
    所以本轮加的用例**必须把面数顶到 1500 以上**，否则又是绿着不工作。

    另一半盲区：**模型不以原点为中心**时，`(0,0)` 是个顶角，走不出材料对 ——
    就算孔完全同心也返回 None（AI 生成件经常不写 `center=true`）。

    --- 同一个函数里还验了「实心底」那条新判据 ---
    贴底/贴顶扫线（`measure_end_coverage`）的读数直接决定产品对用户说“底封住了”，
    所以它必须在**真编译的网格**上有参考值：笔筒 95.8/8.5、开口管 8.5/8.5、
    倒杯 8.5/100（本机实测）。这三组数字分得开，才能说“分档”不是空话；
    预算与耗时也用值验，因为这条在作业前台跑（119s 那一版卡死过界面）。
    """
    import tempfile as _tempfile
    import time as _time
    from pathlib import Path as _Path

    from aiforge import compiler as _compiler, mesh as _mesh

    hole_scad = "difference(){ cube([40,40,10], center=true); cylinder(d=10, h=12, center=true, $fn=64); }\n"
    solid_scad = "cube([40,40,10], center=true);\n"
    # 同一个 Ø10 的孔，但偏心 12mm —— 旧实现在这里返回 None
    offcenter_scad = (
        "difference(){ cube([40,40,10], center=true);"
        " translate([12,0,0]) cylinder(d=10, h=12, center=true, $fn=64); }\n"
    )
    # **非居中模型 + 同心孔**，而且面数要超过旧的 1200 上限（实测 3,616 面）：
    # `$fn=900` 把孔壁敲密；模型不写 center=true，所以 (0,0) 是顶角。
    noncenter_scad = (
        "$fn = 900;\n"
        "difference(){ cube([60,40,8]); translate([30,20,-1]) cylinder(d=10, h=10); }\n"
    )
    noncenter_solid_scad = "$fn = 900;\n cube([60,40,8]);\n"
    with _tempfile.TemporaryDirectory(prefix="hole_") as tmp:
        d = _Path(tmp)
        r1 = _compiler.compile_to_stl(hole_scad, d, "hole")
        assert r1.ok and r1.stl_path, r1.message or r1.fatal
        m1 = _mesh.analyze(_Path(r1.stl_path))
        assert m1.hole_diameter_mm is not None and abs(m1.hole_diameter_mm - 10.0) <= 0.3, (
            f"Ø10 的孔应量出≈10，实测 {m1.hole_diameter_mm}"
        )
        r2 = _compiler.compile_to_stl(solid_scad, d, "solid")
        assert r2.ok and r2.stl_path, r2.message or r2.fatal
        m2 = _mesh.analyze(_Path(r2.stl_path))
        assert m2.hole_diameter_mm is None, f"实心块不应量出孔，实测 {m2.hole_diameter_mm}"

        r3 = _compiler.compile_to_stl(offcenter_scad, d, "offc")
        assert r3.ok and r3.stl_path, r3.message or r3.fatal
        m3 = _mesh.analyze(_Path(r3.stl_path))
        assert m3.hole_diameter_mm is not None, (
            "偏心孔量不到：孔轴不能假设在 (0,0)（这会让正确的孔径改动被判成「没改到」）"
        )
        assert abs(m3.hole_diameter_mm - 10.0) <= 0.3, (
            f"偏心 Ø10 的孔应量出≈10，实测 {m3.hole_diameter_mm}"
        )
        # 剖面（6 个高度）也必须能测到偏心孔，否则改动判据拿到的全是 None
        assert any(v is not None for v in m3.hole_profile), (
            f"偏心孔的 hole_profile 全是 None：{m3.hole_profile}"
        )

        # --- 本轮补的覆盖：非居中模型 + 同心孔 + 面数超旧的 1200 上限 ---
        r4 = _compiler.compile_to_stl(noncenter_scad, d, "nonc")
        assert r4.ok and r4.stl_path, r4.message or r4.fatal
        _t0 = _time.perf_counter()
        m4 = _mesh.analyze(_Path(r4.stl_path))
        _analyze_cost = _time.perf_counter() - _t0
        # 这条断言是整项检查的**存在理由**：面数一旦掉回 1200 以下，
        # 这个用例就退化成「守卫绿着但不工作」（旧守卫就是这么漏掉 14/14 真实模型的）。
        assert m4.triangles > 1500, (
            f"用例只有 {m4.triangles} 面，落在旧的面数上限内 —— "
            "守不住「代价上限过期」这一类退化，请把 $fn 调大"
        )
        assert m4.hole_diameter_mm is not None, (
            "同心孔 + 非居中模型量不到：孔轴锚点不能只给 (0,0)（(0,0) 在这里是个顶角）"
        )
        # 读数必须≈真值：逐步走（step=0.1）时返回的是「最后一个还空的采样点」
        # 而不是墙面，直径被系统性低估 2×step —— 实测 Ø10 报 9.8。
        # 公差 0.1 能咬住那 0.2 的偏差，又容纳 OpenSCAD 多边形化（Ø10/64 段 ≈ 9.99）。
        assert abs(m4.hole_diameter_mm - 10.0) <= 0.1, (
            f"Ø10 量成 {m4.hole_diameter_mm}：系统性低估就是「改对了却报不达标」"
        )
        _hits = sum(1 for v in m4.hole_profile if v is not None)
        assert _hits == 6, (
            f"6 个高度只量到 {_hits} 个：预算把**本来量得到**的剖面砍掉了，"
            "等于换一个门槛继续误判"
        )
        # 时间必须有余量：这个扫描是在作业前台跑的（119s 那一版直接把界面卡死）
        assert _analyze_cost <= 15.0, f"{m4.triangles} 面 analyze 花了 {_analyze_cost:.1f}s"

        # 阴性对照：非居中的实心块也必须 None（加了锚点以后判据没被放松）
        r5 = _compiler.compile_to_stl(noncenter_solid_scad, d, "nonc_solid")
        assert r5.ok and r5.stl_path, r5.message or r5.fatal
        m5 = _mesh.analyze(_Path(r5.stl_path))
        assert m5.hole_diameter_mm is None, (
            f"非居中实心块不应量出孔，实测 {m5.hole_diameter_mm}"
        )

        # --- 假孔：多边形内腔顶上那条「月牙」不能当成孔 ---
        # 本机实测的翻车形状：Ø80 外壳、内腔 Ø74 从 z=5 起、底座 5mm、
        # 底座正中一个 Ø10 排水孔。这个模型**总高 100、孔只在 0~5**：
        #   * 剖面只按比例取（最低 15%）时，六个高度全量不到那个真孔；
        #   * 而搜索会把贴着腔壁的那条月牙当成孔，在 z=15 这种**根本没有孔**
        #     的高度报出 **11.36**。
        # 后果：【问题 8】对一个做对了的 Ø10 孔说「要求 10mm，实测 11.36」——
        # 正确的修改被判失败，还会驱动修复循环去改一个本来没坏的东西。
        penholder_scad = (
            "difference(){ cylinder(h=100, r=40, $fn=64);"
            " translate([0,0,5]) cylinder(h=96, r=37, $fn=64);"
            " cylinder(h=5.001, r=5, $fn=48); }\n"
        )
        r6 = _compiler.compile_to_stl(penholder_scad, d, "penholder")
        assert r6.ok and r6.stl_path, r6.message or r6.fatal
        m6 = _mesh.analyze(_Path(r6.stl_path))
        assert m6.hole_diameter_mm is None, (
            f"笔筒中部只有腔体，没有孔，却量出 {m6.hole_diameter_mm}："
            "腔壁顶上那条月牙形空档被当成孔了（旧的假读数就是 11.36）"
        )
        _ph = sorted({round(v, 2) for v in m6.hole_profile if v is not None})
        assert _ph, (
            f"底座上的 Ø10 排水孔一个高度都没量到（剖面 {m6.hole_profile}）："
            "探针必须包含贴底的绝对高度，否则薄底座里的孔对判据永久隐形"
        )
        assert all(abs(v - 10.0) <= 0.3 for v in _ph), (
            f"笔筒剖面量到 {_ph}，而这个模型里唯一的孔是底座正中 Ø10："
            "多出来的读数就是腔体/月牙被当成孔（11.36 那一类假孔）"
        )

        # --- 实心底 / 开口方向：必须钉在**真实网格**上 -------------------------
        # 合成 metrics 那份守卫（「实心底与开口方向判定」）只能验判定分支，验不了
        # 「贴底 0.3mm 这根探针到底落在底板上还是落进腔里」——而那才是这条判据
        # 真正的风险所在：`_END_PROBE_MM` 从 0.3 改成 3.0，合成用例一点感觉都没有，
        # 用户那边却会把 2mm 的底当成开口。
        _bc, _tc = m6.bottom_coverage, m6.top_coverage
        assert _bc is not None and _tc is not None, (
            f"笔筒的贴底/贴顶占比没量出来（{_bc}/{_tc}）：reqtests 整条退化成 invalid，"
            "用户明令过的「实心底」变成「没测出来」"
        )
        assert _bc >= 0.9, (
            f"5mm 底板 + 底座正中 Ø10 排水孔被量成 {round(_bc * 100, 1)}%（本机实测 95.8）："
            "探针高度或奇偶口径漂了，做对了的设计会被判成「底被挖穿」"
        )
        assert _tc <= 0.15, (
            f"顶部敞开的笔筒被量出贴顶 {round(_tc * 100, 1)}%（本机实测 8.5）："
            "贴顶探针没落在开口里，「方向反了」那一档就永远判不出来"
        )
        # 前端与落盘都按这两个键名取数（改名要同步 API-CONTRACT），先钉住
        _pd = m6.to_dict()
        assert _pd.get("bottom_coverage_pct") is not None and _pd.get("top_coverage_pct") is not None, (
            f"to_dict 没给出 bottom/top_coverage_pct：{sorted(_pd)}"
        )

        # 另外两个真模型形状：开口管（内腔从 z=0 挖穿）与倒过来建的杯。
        # 31 份历史版本里底开口的 18 份就是这两类（14 两端开 / 4 上下反），
        # 拿它们做夹具是因为**分档靠这两个数就能分开**，不需要再读需求文字。
        tube_scad = (
            "difference(){ cylinder(h=100, r=40, $fn=64);"
            " cylinder(h=100.001, r=37, $fn=64); }\n"
        )
        inverted_scad = (
            "difference(){ cylinder(h=100, r=40, $fn=64);"
            " translate([0,0,-0.005]) cylinder(h=95, r=37, $fn=64); }\n"
        )
        r7 = _compiler.compile_to_stl(tube_scad, d, "tube")
        assert r7.ok and r7.stl_path, r7.message or r7.fatal
        m7 = _mesh.analyze(_Path(r7.stl_path))
        assert m7.bottom_coverage is not None and m7.top_coverage is not None, (
            f"开口管两端没量出来（{m7.bottom_coverage}/{m7.top_coverage}）"
        )
        assert m7.bottom_coverage <= 0.15 and m7.top_coverage <= 0.15, (
            f"两端挖穿的开口管量到 底{round(m7.bottom_coverage * 100, 1)}% / "
            f"顶{round(m7.top_coverage * 100, 1)}%（本机实测都是 8.5）："
            "这种件会被判成「有底」，然后装着笔漏一桌面"
        )
        r8 = _compiler.compile_to_stl(inverted_scad, d, "inverted")
        assert r8.ok and r8.stl_path, r8.message or r8.fatal
        m8 = _mesh.analyze(_Path(r8.stl_path))
        assert m8.bottom_coverage is not None and m8.top_coverage is not None, (
            f"倒杯两端没量出来（{m8.bottom_coverage}/{m8.top_coverage}）"
        )
        assert m8.bottom_coverage <= 0.15 and m8.top_coverage >= 0.9, (
            f"腔体从底面开挖、顶上封死的杯量成 底{round(m8.bottom_coverage * 100, 1)}% / "
            f"顶{round(m8.top_coverage * 100, 1)}%（本机实测 8.5 / 100）："
            "上下反了这一档就分不出来 —— 27cc8c 那种件会直接交付出去"
        )

        # 薄底容器（2mm 底板）：这一例专门看**探针高度**本身。上面三个夹具的壁都够厚，
        # `_END_PROBE_MM` 从 0.3 改成 3.0 它们全都量不出来（本机实测：笔筒仍 95.8）；
        # 而 2mm 的底在 3.0mm 探针下就读成 8.5%（本机实测），做对了的薄底件会被判成开口。
        # 用户真实需求里“壁厚 2mm”很常见，所以这一例不是凑数，是把上一个夹具留的盲区补上。
        thin_bottom_scad = (
            "difference(){ cylinder(h=100, r=40, $fn=64);"
            " translate([0,0,2]) cylinder(h=99, r=37, $fn=64); }\n"
        )
        r9 = _compiler.compile_to_stl(thin_bottom_scad, d, "thin_bottom")
        assert r9.ok and r9.stl_path, r9.message or r9.fatal
        m9 = _mesh.analyze(_Path(r9.stl_path))
        assert m9.bottom_coverage is not None and m9.bottom_coverage >= 0.9, (
            f"2mm 实心底被量成 {m9.bottom_coverage}：贴边探针比底还厚，"
            "探到腔里去了（本机实测 0.3mm 探针下是 100%）"
        )
        assert m9.top_coverage is not None and m9.top_coverage <= 0.15, (
            f"2mm 底、顶部敲开的筒量出贴顶 {m9.top_coverage}：顶部探针没在腔里"
        )

        # 预算是否**真的生效**：用值验，不用时钟验（这一段必须在临时目录里跑完，
        # 跳出去文件就被删了）。
        # 同一个模型、同一次整段搜索（不传 center，(0,0) 又不在模型里 → 只能走搜索）：
        #   给一个已经过期的 deadline → 必须 None；预算宽裕 → 必须量到 ≈10。
        # 哪天 deadline 检查被删掉，前一条会返回 9.99 而不是 None —— 当场变红，
        # 而且不受机器快慢影响（拿计时断言验预算会在慢机器上假失败）。
        _pts4, _ = _mesh.read_triangles(_Path(r4.stl_path))
        _fc4 = len(_pts4) // 3
        _mid4 = (m4.bbox_min[2] + m4.bbox_max[2]) / 2.0
        _grid4 = _mesh._FaceGrid(_pts4, _fc4, 1.0)  # noqa: SLF001
        expired = _mesh.measure_hole_diameter(
            _pts4, _fc4, _mid4, grid=_grid4,
            deadline=_time.monotonic(),
        )
        assert expired is None, (
            f"deadline 已经过期却仍量出 {expired}：预算检查没生效（会把作业卡住）"
        )
        roomy = _mesh.measure_hole_diameter(
            _pts4, _fc4, _mid4, grid=_grid4,
            deadline=_time.monotonic() + 60.0,
        )
        assert roomy is not None and abs(roomy - 10.0) <= 0.1, (
            f"整段搜索在预算内量不到孔（实测 {roomy}）：偏心孔搜索这条路断了"
        )

        # 贴边扫线的**时间预算用值验**（和上面同一个做法，不用时钟）：
        # 那 0.35s/端 是实测定的，一旦哪天有人把它改成 35s，作业前台就会像
        # 119s 那一版一样把界面卡死 —— 这里要当场变红，而不是靠用户反馈。
        _expired_end = _mesh.measure_end_coverage(
            _pts4, _fc4, m4, grid=_grid4, deadline=_time.monotonic(),
        )
        assert _expired_end is None, (
            f"deadline 过期仍量出 {_expired_end}：每端 0.35s 的预算根本没生效"
        )
        # 两端各走一遍的**真实耗时**（这条在作业前台跑，是用户等得到的时间）：
        # 本机实测 3,616 面约 0.01s，给 1s 上限是百倍余量 —— 足够拖住「改成
        # 逐层扫描」这类把 3 条线写成 300 条的退化，又不会在慢机器上假失败。
        _t_end = _time.perf_counter()
        _mesh.measure_end_coverage(_pts4, _fc4, m4, grid=_grid4)
        _mesh.measure_end_coverage(_pts4, _fc4, m4, at_top=True, grid=_grid4)
        _cost_end = _time.perf_counter() - _t_end
        assert _cost_end <= 1.0, (
            f"{m4.triangles} 面两端量了 {_cost_end:.2f}s：贴边扫线变成逐层扫了"
        )
        assert 0 < _mesh._END_BUDGET_S <= 1.0, (  # noqa: SLF001
            f"每端预算 {_mesh._END_BUDGET_S}s 不合理：要能跑完 3 条扫线，又不能卡住界面"
        )

        # --- hole_through 推导必须在**真编译的网格**上验 --------------------------------------
        # `t_hole_criterion` 与 `t_requirement_tests` 里那些用例都是直接 `{hole_through: ...}`
        # 塑好成品给下游看，它们验的是「下游怎么用」，验不到 `analyze()` 那一行推导本身对不对
        # ——拿假数据推导与拿真数据推导对不上，就是个照不到问题的守卫（同一文件里已写过一次
        # 「旧守卫自己也是照不到问题的守卫」，同一个坑不能靠下游用例默不作声地撑过去）。
        sealed_cavity_scad = (
            "$fn = 64;\n"
            "difference(){ cube([60,60,40]); translate([22,22,10]) cylinder(d=16, h=20); }\n"
        )
        through_hole_scad = (
            "$fn = 64;\n"
            "difference(){ cube([60,60,40]); translate([22,22,-1]) cylinder(d=16, h=42); }\n"
        )
        blind_hole_scad = (
            "$fn = 64;\n"
            "difference(){ cube([60,60,40]); translate([22,22,-1]) cylinder(d=16, h=10); }\n"
        )
        for _name, _scad, _want in (
            ("sealed", sealed_cavity_scad, False),
            ("through", through_hole_scad, True),
            ("blind", blind_hole_scad, None),
        ):
            _r = _compiler.compile_to_stl(_scad, d, _name)
            assert _r.ok and _r.stl_path, (_name, _r.message or _r.fatal)
            _m = _mesh.analyze(_Path(_r.stl_path))
            assert _m.hole_through is _want, (
                f"{_name} 模型的 hole_through 应判 {_want}，实际 {_m.hole_through}"
                f"（剖面 {_m.hole_profile}）"
            )
            if _want is False:
                # 阴面的关键断言：这个模型确实能被量出「孔径」（中间高度有 16mm 的空档），
                # 不是「完全量不到」——如果 hole_diameter_mm 本身已经是 None，那 hole_through=False
                # 就只是在重复一个不需要重复的结论，本用例的存在意义（拿「任一高度命中」当通过）
                # 就没被验证到了。
                assert _m.hole_diameter_mm is not None, (
                    f"{_name} 模型本应能被量到孔径（这样 hole_through=False 才有意义），"
                    f"实际 hole_diameter_mm 已是 {_m.hole_diameter_mm}：用例没验证到它想验的东西"
                )

    # --- 代价必须封顶，但封顶要用**时间**而不是面数 ---
    assert not hasattr(_mesh, "_HOLE_AXIS_MAX_FACES"), (
        "面数上限又回来了：按面数封顶会把「本来 0.2 秒能量出来的模型」判成没量到"
        "（旧值 1200 就是这样让用户 14/14 份真实模型全盲的）"
    )
    assert 0 < _mesh._HOLE_SEARCH_BUDGET_S <= 2.0, (  # noqa: SLF001
        f"单次找孔轴预算 {_mesh._HOLE_SEARCH_BUDGET_S} 不合理（要能跑完一次搜索，又要不卡界面）"  # noqa: E501
    )
    assert _mesh._HOLE_SEARCH_BUDGET_S <= _mesh._HOLE_PHASE_BUDGET_S <= 20.0, (  # noqa: SLF001
        f"总预算 {_mesh._HOLE_PHASE_BUDGET_S} 必须容得下一次完整搜索"
        f"（{_mesh._HOLE_SEARCH_BUDGET_S}）又要小到不把界面卡住"
    )

    return (
        f"同心/偏心/非居中同心（{m4.triangles} 面，超旧上限）均 ≈10；"
        "实心 None（阴性对照）；剖面 6/6 不被预算砍掉；deadline 用值验证生效；"
        "笔筒（Ø74 腔 + 底座 Ø10 孔）中部不再报假孔 11.36、贴底探针量到真孔；"
        f"贴底/贴顶实测 笔筒 {round(m6.bottom_coverage * 100, 1)}/{round(m6.top_coverage * 100, 1)}%、"
        f"开口管 {round(m7.bottom_coverage * 100, 1)}/{round(m7.top_coverage * 100, 1)}%、"
        f"倒杯 {round(m8.bottom_coverage * 100, 1)}/{round(m8.top_coverage * 100, 1)}%、"
        f"2mm 薄底 {round(m9.bottom_coverage * 100, 1)}/{round(m9.top_coverage * 100, 1)}%"
        f"（两端共 {_cost_end * 1000:.0f}ms）"
    )



def t_hole_criterion() -> str:
    """孔径改动判据：声明 vs 实测剖面的四种情形（匹配/不匹配/量不到/未点名）。"""
    from aiforge import jobs as jobs_mod
    from aiforge.design import DesignSpec, SpecItem
    from aiforge.mesh import MeshMetrics

    def spec_with_hole(value, user_specified=True):
        s = DesignSpec()
        s.specs = [SpecItem(name="hole_diameter", label="孔径", value=value, user_specified=user_specified)]
        return s

    # 1) 剖面任一高度匹配声明值 → 不报
    m = MeshMetrics(); m.hole_profile = [None, 10.05, None, 9.9, None, None]
    assert jobs_mod._hole_problems(spec_with_hole(10.0), m) == []
    # 2) 剖面与声明不符 → 报「问题 8」
    m = MeshMetrics(); m.hole_profile = [8.0, 8.0, 8.0, 8.0, 8.0, 8.0]
    probs = jobs_mod._hole_problems(spec_with_hole(10.0), m)
    assert len(probs) == 1 and "问题 8" in probs[0], probs
    # 3) 剖面全 None（没有居中孔）→ 不下结论
    m = MeshMetrics(); m.hole_profile = [None] * 6
    assert jobs_mod._hole_problems(spec_with_hole(10.0), m) == []
    # 4) 非用户点名 → 不报
    m = MeshMetrics(); m.hole_profile = [8.0] * 6
    assert jobs_mod._hole_problems(spec_with_hole(10.0, user_specified=False), m) == []
    # 5) 剖面「任一高度命中」但 mesh 已判定这不是开口的孔（`hole_through is False`，
    #    两端都没开口）—— 不能当成「孔径做到」静默放过，必须报出来。
    m = MeshMetrics(); m.hole_profile = [None, None, 10.0, 10.0, 10.0, None]
    m.hole_through = False
    probs = jobs_mod._hole_problems(spec_with_hole(10.0), m)
    assert len(probs) == 1 and "封闭" in probs[0], probs
    # 反向：同样的剖面数字，`hole_through is None`（只开一面的盲孔，或没测完）不能
    # 被误报成封闭腔（否则会把真实合法的凹槽/盲孔设计当成缺陷去修，与本次要防的
    # 「没结论被当成结论」是同一种错的另一个方向）。
    m = MeshMetrics(); m.hole_profile = [None, None, 10.0, 10.0, 10.0, None]
    m.hole_through = None
    assert jobs_mod._hole_problems(spec_with_hole(10.0), m) == []
    return "匹配不报/不符报问题8/量不到不报/未点名不报/封闭腔报问题 五种情形均正确"


def t_thread_detection() -> str:
    """螺纹识别：真螺旋 vs 同心环（后者看着有纹路，实际拧不上）。

    用**合成点云**测，不需要 OpenSCAD，所以离线也能跑：
      * 真螺旋：牙顶只出现在一个薄片高度里的一小段角度上；
      * 同心环（rotate_extrude 的产物）：同一个高度上四周到处都有牙顶。
    用户反馈的「72 转 67 滤镜转接环做不出来」正是后者 —— 模型封闭、尺寸也对，
    只有专门量才能发现拧不上。
    """
    import math as _math
    import time as _time

    from aiforge import mesh

    def helix_points(turns: int = 6, pitch: float = 0.75, r_root: float = 35.5,
                     depth: float = 0.46, per_turn: int = 120) -> list[tuple[float, float, float]]:
        points = []
        total = turns * per_turn
        for i in range(total):
            theta = 2 * _math.pi * i / per_turn
            z = pitch * i / per_turn
            # 牙型：相位 = 角度/2π − z/螺距，取三角波
            phase = (i / per_turn) % 1.0
            tooth = 1 - abs(2 * phase - 1)
            r = r_root + depth * tooth
            for offset in (0.0, 0.02):  # 表面附近两个半径，模拟网格厚度
                points.append((r * _math.cos(theta), r * _math.sin(theta), z + offset))
        return points

    def ring_points(z_levels: int = 40, per_level: int = 60, pitch: float = 0.75,
                    r_root: float = 35.5, depth: float = 0.46) -> list[tuple[float, float, float]]:
        points = []
        for level in range(z_levels):
            z = pitch * level / 8.0
            phase = (level / 8.0) % 1.0
            tooth = 1 - abs(2 * phase - 1)
            r = r_root + depth * tooth
            for j in range(per_level):  # 同一高度上**整圈**都是这个半径
                theta = 2 * _math.pi * j / per_level
                points.append((r * _math.cos(theta), r * _math.sin(theta), z))
        return points

    helix = helix_points()
    rings = ring_points()
    assert mesh.detect_helix_thread(helix) is True, "真螺旋必须被判为螺旋"
    # 同心环必须**不能**被判成螺旋。返回 None 表示"没找到螺旋"（保守结论），
    # 与 False 同样是正确的判定 —— 关键是绝不能说它是螺旋。
    assert mesh.detect_helix_thread(rings) is not True, "同心环绝不能被判成螺旋"
    assert mesh.measure_thread_depth(helix) is not None, "真螺旋应当量出牙深"

    # 代价守卫：这个函数在每个生成作业里都**无条件**跑一遍（手机支架、
    # 水杯架这些根本没有螺纹的件也要付钱），所以它必须是线性的。
    # 实测踩到的形状：把 `max(radii)/min(radii)` 写在列表推导的条件里，
    # 条件对每个元素重算一遍全表 —— 24,016 面的模型上单这个函数就吃
    # **41.4s**，占整段 analyze 47.8s 的 86%（cProfile：`max` 32.4 万次 + `min` 34.8 万次）。
    # 用合成点云测，不需要 OpenSCAD，所以离线也能跑住这条。
    big = helix_points(turns=120, per_turn=250)  # 30,000 点 / 跨 90mm 高度
    assert len(big) >= 20_000, len(big)
    _t0 = _time.perf_counter()
    big_depth = mesh.measure_thread_depth(big)
    _cost = _time.perf_counter() - _t0
    assert big_depth is not None, "大点云也必须量出牙深（不能拿「算不动」当结论）"
    assert _cost <= 0.5, (
        f"measure_thread_depth 在 {len(big):,} 点上花了 {_cost:.2f}s："
        "这个扫描是无条件跑的，代价必须随点数线性增长 —— "
        "查有没有在循环/推导式里重算全表的 max/min/sorted/sum"
    )

    # 需求里要螺纹、模型却没有真螺旋时，必须产出可执行的修复证据
    from aiforge import jobs as jobs_mod

    class _FakeMetrics:
        thread_depth_mm = None
        thread_is_helix = None

    problems = jobs_mod._thread_problems(_FakeMetrics(), "设计一个 72mm 转 67mm 的滤镜转接环")  # noqa: SLF001
    assert problems and "aiforge_parts.scad" in problems[0], problems
    assert not jobs_mod._thread_problems(_FakeMetrics(), "设计一个桌面手机支架"), (  # noqa: SLF001
        "无关需求不该报螺纹问题"
    )
    spiral = _FakeMetrics()
    spiral.thread_depth_mm = 0.46
    spiral.thread_is_helix = False
    problems = jobs_mod._thread_problems(spiral, "螺纹转接环")  # noqa: SLF001
    assert problems and "同心环" in problems[0], problems
    return "真螺旋/同心环可区分（合成点云），无螺纹时给出「改用零件库」的修复证据"


def t_repair_wiring() -> str:
    """几何修复链路的**接线**测试（不需要 OpenSCAD、不需要 AI）。

    为什么单列：加「需求里要螺纹但模型没有真螺旋」这条判定时，调用点写了
    `self._requirement_text()`，而方法本身没加进类里 —— 运行到这条分支直接
    AttributeError，作业变成「生成过程中出现了意外问题」。
    自检当时全绿，因为这条分支离线不跑。所以这里用桩对象把链路走一遍：
    断言需求文本确实传到了判定函数里，且判定结果被拼进给 AI 的修复证据。
    """
    from aiforge import jobs as jobs_mod
    from aiforge import mesh as mesh_mod

    runner = jobs_mod.GenerationRunner.__new__(jobs_mod.GenerationRunner)
    assert hasattr(runner, "_requirement_text"), "GenerationRunner 必须有 _requirement_text()"
    assert runner._requirement_text() == ""  # noqa: SLF001 - 未开始作业时应为空串

    runner._requirement = "设计一个 72mm 转 67mm 的滤镜转接环，带螺纹"  # noqa: SLF001
    assert "滤镜" in runner._requirement_text()  # noqa: SLF001

    metrics = mesh_mod.MeshMetrics(triangles=12, bbox=(72.0, 72.0, 8.4), watertight=True, solid_parts=1)
    problems = jobs_mod._geometry_integrity_problems(  # noqa: SLF001
        metrics, None, "", None, runner._requirement_text()  # noqa: SLF001
    )
    assert problems, "需求要螺纹、模型没有螺纹时应当给出修复证据"
    assert "aiforge_parts.scad" in problems[0], problems[0][:200]

    # 反向：同一份 metrics，但需求没提螺纹 → 不该报螺纹问题
    assert not jobs_mod._geometry_integrity_problems(  # noqa: SLF001
        metrics, None, "", None, "设计一个桌面手机支架"
    ), "无关需求不该报螺纹问题"

    # 修复调用点必须把需求文本传进去（防止以后重构时又漏）
    import inspect

    source = inspect.getsource(jobs_mod.GenerationRunner._repair_geometry_integrity)  # noqa: SLF001
    assert source.count("_requirement_text()") >= 3, (
        f"几何修复里应当有 3 处传递需求文本，实际 {source.count('_requirement_text()')} 处"
    )
    # 散件两轮修不好时的换路径兜底必须接上；早退比较的元数必须和 rank 一致
    # （写少一位会导致永不早退，写多一位会永远早退）
    assert "_INTEGRITY_REGEN_EXTRA" in source, "散件兜底换路径重生成未接线"
    assert "(0, 0, 0, 0, 0, 0, 0)" in source, "完整性早退比较应为 7 元组（与 _integrity_rank 同数）"
    # AI 复核里用过 job.say 就必须先绑定 job（实测漏绑导致 modify 作业 NameError 崩溃）
    second = inspect.getsource(jobs_mod.GenerationRunner._ai_second_opinion)  # noqa: SLF001
    assert "job = self.job" in second, "_ai_second_opinion 必须先绑定 job"
    return "需求文本贯通几何修复链路，螺纹缺失/散件兜底均有可执行证据与换路径重生成"


def t_axis_dimension_fix() -> str:
    """点名尺寸实测偏差的确定性逐轴缩放兜底（离线、不调 AI）。

    阴性对照：偏差 ≤2% 必须返回 None（不修），否则会把测量噪声当问题乱缩。
    """
    from aiforge import jobs as jobs_mod
    from aiforge.design import DesignSpec, SpecItem
    from aiforge.mesh import MeshMetrics

    runner = jobs_mod.GenerationRunner.__new__(jobs_mod.GenerationRunner)
    spec = DesignSpec()
    spec.specs = [SpecItem(name="height", label="总高", value=100.0, user_specified=True)]
    source = "height = 100;\ncube([80, 80, height]);\n"

    # 实测 z=110（偏 10%）→ 只缩 z，倍率 100/110
    m = MeshMetrics(triangles=12, bbox=(80.0, 80.0, 110.0), watertight=True, solid_parts=1)
    fixed = runner._axis_dimension_fix(source, m, spec)  # noqa: SLF001
    assert fixed and "scale([1.000000, 1.000000, 0.909091])" in fixed, fixed
    # 声明留在块外、几何被缩进包进 scale 块
    assert "height = 100;\nscale(" in fixed and "\n  cube([80, 80, height]);" in fixed, fixed

    # 实测 z=100（偏差 0）→ 不修
    m_ok = MeshMetrics(triangles=12, bbox=(80.0, 80.0, 100.0), watertight=True, solid_parts=1)
    assert runner._axis_dimension_fix(source, m_ok, spec) is None  # noqa: SLF001

    # 偏差离谱（z=30，倍率 3.3 超出放大上界 2.0）→ 不修，宁可报问题
    m_bad = MeshMetrics(triangles=12, bbox=(80.0, 80.0, 30.0), watertight=True, solid_parts=1)
    assert runner._axis_dimension_fix(source, m_bad, spec) is None  # noqa: SLF001
    return "逐轴缩放兜底：偏 10% 只缩 z、偏差≤2% 不修、倍率离谱不修"


def t_sliver_detection() -> str:
    """≈0 厚薄膜（挖空刀具没伸出表面）必须报为完整性问题；正常壁厚不报。

    实测 glm 的笔筒底留下一层 0.01mm 膜：水密/单件/尺寸全对，只有射线壁厚能量出。
    """
    from aiforge import jobs as jobs_mod
    from aiforge.mesh import MeshMetrics, WallThickness

    thin = WallThickness(min_mm=0.01, p05_mm=0.01, median_mm=2.5, samples=400,
                         thinnest=[((0.0, 0.0, 0.0), 0.01)])
    m = MeshMetrics(triangles=12, bbox=(80.0, 80.0, 100.0), watertight=True, solid_parts=1, wall=thin)
    probs = jobs_mod._geometry_integrity_problems(m, None, "", None, "设计一个笔筒")  # noqa: SLF001
    assert any("问题 9" in p for p in probs), probs
    ok_wall = WallThickness(min_mm=2.0, p05_mm=2.5, median_mm=2.5, samples=400, thinnest=[])
    m_ok = MeshMetrics(triangles=12, bbox=(80.0, 80.0, 100.0), watertight=True, solid_parts=1, wall=ok_wall)
    assert not jobs_mod._sliver_problems(m_ok), "正常壁厚不该报薄膜"  # noqa: SLF001
    return "0.01mm 薄膜报问题9（含位置与改法）、正常壁厚不报"


def t_modify_prompt_clarifications() -> str:
    """修改提示词必须带上用户此前的补充说明（长对话口头约束不丢，HANDOVER P2）。

    阴性对照：没有 clarifications 时不该凭空多出这一段。
    """
    from aiforge import changes as changes_mod
    from aiforge.design import DesignSpec, SpecItem

    spec = DesignSpec(
        kind="笔筒",
        specs=[SpecItem(name="height", label="总高", value=100.0, user_specified=True)],
    )
    spec.clarifications = ["底部要留排水孔", "不要镂空侧壁"]
    contract = changes_mod.build_change_contract("把高度改成 120mm", spec, {"height": 100.0})
    prompt = changes_mod.modify_prompt("height = 100;\ncube([80, 80, height]);\n", contract, spec)
    assert "底部要留排水孔" in prompt and "不要镂空侧壁" in prompt, prompt[:400]
    assert "用户此前补充说明过的要求" in prompt

    plain = DesignSpec(kind="笔筒", specs=[SpecItem(name="height", label="总高", value=100.0, user_specified=True)])
    contract2 = changes_mod.build_change_contract("把高度改成 120mm", plain, {"height": 100.0})
    prompt2 = changes_mod.modify_prompt("height = 100;\ncube([80, 80, height]);\n", contract2, plain)
    assert "用户此前补充说明过的要求" not in prompt2, "没有补充说明时不该凭空多出这一段"
    return "修改提示词携带 clarifications（口头约束不丢），无补充时不凭空多段"


def t_provider_keys_and_economy() -> str:
    """两把 Key 按提供商分槽共存 + 混合模式经济档接线（离线、不碰真实凭据）。

    混合模式需要 DeepSeek 付费生成 + 智谱免费复核同时可用，
    所以 Key 必须分槽存、按 base_url 取；清一把不能带走另一把。
    """
    import inspect
    import tempfile as _tempfile
    from pathlib import Path as _Path

    from aiforge import config as config_mod
    from aiforge import jobs as jobs_mod

    assert config_mod.provider_of_base("https://api.deepseek.com") == "deepseek"
    assert config_mod.provider_of_base("https://open.bigmodel.cn/api/paas/v4") == "glm"
    assert config_mod.ApiSettings().economy_model == ""
    assert config_mod.ApiSettings().economy_base_url == ""

    with _tempfile.TemporaryDirectory(prefix="keys_") as tmp:
        store = config_mod.SecretStore(_Path(tmp) / "credentials.json")
        # 强制文件后端：真实机器凭据管理器里可能已有用户真 Key，不能拿它做断言
        store._keyring = lambda: None  # noqa: SLF001
        store.set("sk-deepseek-1", "deepseek")
        store.set("glm-key-1", "glm")
        assert store.get("deepseek") == "sk-deepseek-1"
        assert store.get("glm") == "glm-key-1"
        store.set("", "glm")  # 清智谱不应带走 DeepSeek
        assert store.get("glm") is None
        assert store.get("deepseek") == "sk-deepseek-1"

        # --- 后端归属必须按提供商分开报 ---
        # 设置页（settings_routes）与诊断页（envcheck.check_api）都拿 `backend_for(prov)`
        # 告诉用户「Key 存在哪儿」。两把 Key 允许存在不同后端（混合模式的设计前提），
        # 所以共用一个字段时「上一次读写的是哪把」会决定「这一把存在哪儿」的回答。
        # 本机实测过的形状：deepseek 进凭据管理器、glm 落文件之后，deepseek 那把
        # 被报成 config_file —— 用户按这个说法去删/换 Key 根本找不到它。
        class _FakeVault:
            """只实现产品用到的三个方法，不碰真机凭据管理器。"""

            def __init__(self) -> None:
                self.data: dict[tuple[str, str], str] = {}

            def get_password(self, service, user):
                return self.data.get((service, user))

            def set_password(self, service, user, value):
                self.data[(service, user)] = value

            def delete_password(self, service, user):
                self.data.pop((service, user), None)

        vault = _FakeVault()
        mixed = config_mod.SecretStore(_Path(tmp) / "mixed-credentials.json")
        mixed._keyring = lambda: vault  # noqa: SLF001
        assert mixed.set("sk-deepseek-2") == "credential_manager"
        mixed._keyring = lambda: None  # noqa: SLF001 - 凭据管理器随后不可用
        assert mixed.set("glm-key-2", "glm") == "config_file"
        assert mixed.backend_for() == "credential_manager", (
            f"DeepSeek 那把明明在凭据管理器里，却被报成 {mixed.backend_for()}"
        )
        assert mixed.backend_for("glm") == "config_file", mixed.backend_for("glm")
        # get() 必须照旧返回 **Key 本身**（改这一段时最容易把返回值写成后端名）
        mixed._keyring = lambda: vault  # noqa: SLF001
        assert mixed.get() == "sk-deepseek-2", f"get() 返回的不是 Key：{mixed.get()!r}"
        assert mixed.get("glm") == "glm-key-2", f"get('glm') 返回的不是 Key：{mixed.get('glm')!r}"
        assert mixed.backend_for() == "credential_manager", "读完之后后端归属又被另一把洗掉了"
        assert mixed.backend_for("glm") == "config_file"

        # --- 真 HTTP 驱动：设置页里那句「Key 存在哪儿」必须是对的 ---
        # 上面那两条只是库层断言，而这个缺陷的用户后果在接口上：
        # `settings_routes._serialize()` 把 `backend_for(prov)` 当作 `api_key_store` 直接回给界面。
        # 本进程的 AIFORGE_DATA_DIR 已在开头指向 build/work 沙箱（见文件顶部），
        # 不会碰到真机凭据管理器与真实密钥文件；进出仍按字节快照/还原。
        import json as _json

        from fastapi.testclient import TestClient

        from aiforge.app import create_app

        cfg_store = config_mod.store(refresh=True)
        secret_file = cfg_store.secrets._file  # noqa: SLF001
        saved_bytes = secret_file.read_bytes() if secret_file.is_file() else None
        saved_kr = cfg_store.secrets._keyring  # noqa: SLF001
        saved_backends = dict(cfg_store.secrets._backend_by_provider)  # noqa: SLF001
        saved_backend = cfg_store.secrets._backend  # noqa: SLF001
        saved_base = cfg_store.config.api.base_url
        try:
            vault_ep = _FakeVault()
            cfg_store.secrets._backend_by_provider.clear()  # noqa: SLF001
            cfg_store.secrets._backend = None  # noqa: SLF001
            cfg_store.secrets._keyring = lambda: vault_ep  # noqa: SLF001
            cfg_store.secrets.set("sk-endpoint-probe")  # deepseek → 凭据管理器
            cfg_store.secrets._keyring = lambda: None  # noqa: SLF001
            cfg_store.secrets.set("glm-endpoint-probe", "glm")  # 智谱 → 本地文件
            # 请求期间把凭据管理器接回去：这才是真机最常见的形态 ——
            # 两把 Key 一在 vault 一在文件，两者都取得到，只是**存放位置不同**。
            # （上一版没接回去，于是第一次请求 `api_key_set` 就是 False，
            # 验成了「拿不到 Key」而不是「报错了存放位置」。）
            cfg_store.secrets._keyring = lambda: vault_ep  # noqa: SLF001
            client_ep = TestClient(
                create_app(), base_url="http://127.0.0.1:8760", raise_server_exceptions=False
            )
            body = client_ep.get("/api/settings")
            assert body.status_code == 200, body.text
            ds = body.json()
            assert ds["api_key_set"] is True, ds
            assert ds["api_key_store"] == "credential_manager", (
                f"DeepSeek 那把在凭据管理器里，设置页却说存成「{ds['api_key_store']}」"
            )
            assert "sk-endpoint-probe" not in _json.dumps(ds), "接口把完整 Key 吐给了界面"
            cfg_store.config.api.base_url = "https://open.bigmodel.cn/api/paas/v4"
            glm = client_ep.get("/api/settings").json()
            assert glm["api_key_set"] is True, glm
            assert glm["api_key_store"] == "config_file", (
                f"智谱那把落在本地文件，设置页却说存成「{glm['api_key_store']}」"
            )
            assert glm["api_key_store"] != ds["api_key_store"], (
                "两把 Key 存在不同后端，接口却回同一个答案 —— 分桶没生效"
            )
        finally:
            cfg_store.secrets._keyring = saved_kr  # noqa: SLF001
            cfg_store.secrets._backend_by_provider.clear()  # noqa: SLF001
            cfg_store.secrets._backend_by_provider.update(saved_backends)  # noqa: SLF001
            cfg_store.secrets._backend = saved_backend  # noqa: SLF001
            cfg_store.config.api.base_url = saved_base
            if saved_bytes is not None:
                secret_file.write_bytes(saved_bytes)

    # 经济档接线：解析/评审/复核三处必须走 _client_economy
    src_pipeline = inspect.getsource(jobs_mod.GenerationRunner._pipeline_generate)  # noqa: SLF001
    src_second = inspect.getsource(jobs_mod.GenerationRunner._ai_second_opinion)  # noqa: SLF001
    assert "_client_economy()" in src_pipeline, "规格解析应走经济档"
    assert "_client_economy()" in src_second, "AI 复核应走经济档"
    src_review = inspect.getsource(jobs_mod.GenerationRunner._finish)  # noqa: SLF001
    assert "_client_economy()" in src_review, "打印性评审应走经济档"
    return "两把 Key 分槽共存/互不带走；后端归属按提供商分开报（库层 + 真 HTTP）；解析/评审/复核接经济档"


def t_3mf_export() -> str:
    """3MF 导出（对标 Meshy/SolidWorks 的可打印交付格式）。离线，不跑 OpenSCAD。

    为什么必须验这三件事：
      * **单位**：STL 不带单位，用户实测踩过“导入切片软件差 10/100 倍”；
        3MF 的核心价值就是 unit="millimeter"，写错就等于白做。
      * **面数一致**：3MF 是从已交付的 STL 转的，不一致就是用户拿到两个不同的模型。
      * **顶点去重**：STL 每个角重复存坐标，不去重文件白胖一半。
    """
    import tempfile as _tempfile
    from pathlib import Path as _Path

    from aiforge import threemf

    # 10×20×30 的长方体：8 个角、12 个三角面（STL 里是 36 个重复顶点）
    c = {
        (0, 0, 0): (0.0, 0.0, 0.0), (1, 0, 0): (10.0, 0.0, 0.0),
        (0, 1, 0): (0.0, 20.0, 0.0), (1, 1, 0): (10.0, 20.0, 0.0),
        (0, 0, 1): (0.0, 0.0, 30.0), (1, 0, 1): (10.0, 0.0, 30.0),
        (0, 1, 1): (0.0, 20.0, 30.0), (1, 1, 1): (10.0, 20.0, 30.0),
    }
    faces = [
        ((0, 0, 0), (1, 0, 0), (1, 1, 0)), ((0, 0, 0), (1, 1, 0), (0, 1, 0)),
        ((0, 0, 1), (1, 1, 1), (1, 0, 1)), ((0, 0, 1), (0, 1, 1), (1, 1, 1)),
        ((0, 0, 0), (0, 1, 0), (0, 1, 1)), ((0, 0, 0), (0, 1, 1), (0, 0, 1)),
        ((1, 0, 0), (1, 1, 1), (1, 1, 0)), ((1, 0, 0), (1, 0, 1), (1, 1, 1)),
        ((0, 0, 0), (0, 0, 1), (1, 0, 1)), ((0, 0, 0), (1, 0, 1), (1, 0, 0)),
        ((0, 1, 0), (1, 1, 0), (1, 1, 1)), ((0, 1, 0), (1, 1, 1), (0, 1, 1)),
    ]
    ascii_stl = "solid box\n" + "".join(
        "  facet normal 0 0 0\n    outer loop\n"
        + "".join(f"      vertex {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}\n" for v in (c[a], c[b], c[d]))
        + "    endloop\n  endfacet\n"
        for a, b, d in faces
    ) + "endsolid box\n"

    with _tempfile.TemporaryDirectory(prefix="3mf_") as tmp:
        stl = _Path(tmp) / "model.stl"
        out = _Path(tmp) / "model.3mf"
        stl.write_text(ascii_stl, encoding="ascii")
        stats = threemf.write_3mf_from_stl(
            stl,
            out,
            title="长方体测试件",
            metadata={"Title": "长方体测试件", "Material": "PLA", "Printer": "X1E", "DesignSizeMM": "10.0x20.0x30.0"},
        )
        assert stats["triangles"] == 12, stats
        assert stats["vertices"] == 8, f"36 个重复顶点应去重成 8 个，实际 {stats['vertices']}"

        summary = threemf.read_3mf_summary(out)
        assert summary["unit"] == "millimeter", summary  # 写错单位比不带单位更坑
        assert summary["triangles"] == 12 and summary["vertices"] == 8, summary
        assert summary["has_build"], "缺 <build> 切片软件不知道把哪个 object 放进去"
        assert summary["metadata"]["Material"] == "PLA", summary
        assert summary["metadata"]["Printer"] == "X1E", summary
        assert summary["object_name"] == "长方体测试件", summary

    # 输入不是 3 的倍数时必须拒绝，不能静默多出半个面
    try:
        threemf.build_3mf_bytes([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)])
        raise AssertionError("顶点数不是 3 的倍数时必须报错")
    except ValueError:
        pass

    # --- <model> 子元素顺序与 metadata 形状必须合规范 ---
    # 本模块踩过两次，都只发生在「用户能打开吗」验不到的地方：
    #   1. 顺序写成 resources → metadata → build；
    #   2. 把所有 <metadata name=…> 包进一层**没有 name 的 <metadata>**（规范里根本没
    #      这个包裹层，name 是必需属性，metadata 也不允许再套元素）。
    # 判据必须走 XML 解析：旧的 `findall(r"<(metadata|resources|build)>")` 只认不带属性的
    # 标签，于是它恰好把那个多余的包裹层当成了合法顺序的证据 —— 尺子跟着被测代码一起错。
    import io as _io
    import xml.etree.ElementTree as _ET
    import zipfile as _zipfile

    ns = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"

    def _model_shape(text: str) -> list[str]:
        """返回 <model> 的直接子元素名序列，不合规就抛 AssertionError。"""
        root = _ET.fromstring(text)
        assert root.tag == f"{ns}model", f"根元素不是 3MF 核心命名空间下的 model：{root.tag}"
        tags = [child.tag.removeprefix(ns) for child in root]
        assert set(tags) <= {"metadata", "resources", "build"}, f"<model> 下有规范外的子元素：{tags}"
        assert tags.count("resources") == 1 and tags.count("build") == 1, f"resources/build 必须各一份：{tags}"
        assert tags == ["metadata"] * tags.count("metadata") + ["resources", "build"], (
            f"<model> 子元素顺序违反 3MF 核心规范：{tags}（应为 metadata* → resources → build）"
        )
        for meta in root.findall(f"{ns}metadata"):
            assert (meta.get("name") or "").strip(), (
                f"<metadata> 缺必需属性 name（规范外包裹层）：{_ET.tostring(meta)[:120]!r}"
            )
            assert len(meta) == 0, f"<metadata> 里不许再套元素：{_ET.tostring(meta)[:120]!r}"
        return tags

    data, _stats = threemf.build_3mf_bytes(
        [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 0), (0, 1, 0), (0, 0, 1)],
        title="顺序",
        metadata={"Material": "PLA"},
    )
    xml = _zipfile.ZipFile(_io.BytesIO(data)).read("3D/3dmodel.model").decode("utf-8")
    tags = _model_shape(xml)
    assert tags.count("metadata") >= 2, f"Title 与 Material 应各占一条 metadata：{tags}"

    # 阴性对照：两个真实踩过的形状都必须被判不合格（否则这条判据就是个常亮的摆设）。
    for label, sample in (
        ("顺序写反", f'<model xmlns="{threemf.CORE_NS}"><resources/><metadata name="X">1</metadata><build/></model>'),
        ("多一层无 name 的包裹", f'<model xmlns="{threemf.CORE_NS}"><metadata><metadata name="Title">t</metadata></metadata><resources/><build/></model>'),
    ):
        try:
            _model_shape(sample)
        except AssertionError:
            pass
        except _ET.ParseError as exc:  # 解析不了也算不合格（但不能静默当成通过）
            raise AssertionError(f"{label} 的样本根本不是合法 XML：{exc}") from exc
        else:
            raise AssertionError(f"{label} 的投毒样本没被识破：形状判据失效了")

    # ZIP 条目必须用正斜杠：ZIP 规范要求 / ，反斜杠会让不少解包器直接拒绝。
    with _zipfile.ZipFile(_io.BytesIO(data)) as _z:
        names = _z.namelist()
    assert all("\\" not in n for n in names), f"ZIP 条目用了反斜杠：{names}"
    assert names[0] == "[Content_Types].xml", f"[Content_Types].xml 必须是第一个条目：{names}"

    return (
        "3MF：mm 单位/面数与 STL 一致/顶点 36→8 去重/元数据与 build 可读回/坏输入拒绝/"
        "子元素顺序与 metadata 形状合规范（含两个阴性对照）/正斜杠条目"
    )


def t_slicer_detect() -> str:
    """切片软件检测的判定逻辑：识别要宽、不猜、同一软件只能出现一次、送出的文件不能被自己删掉。

    为什么全靠夹具而不跑真机：这台机器装了 Bambu Studio 与 OrcaSlicer，换一台
    只装 Cura、或者把软件放在 `D:\\绿色软件\\` 下的机器，检测结果就全变了 ——
    靠“在我机器上能看到”永远测不到那些形态。这里把解析与合并规则钉死（喂注册表
    原文、造假安装目录、替掉候选来源）；真机那一半由 `launch/send` 的往返脚本另测。
    """
    import tempfile
    from types import SimpleNamespace

    from aiforge import slicers

    # 1) exe 名 → 种类：NTFS 不区分大小写，所以比较前必须 casefold
    kind = slicers.classify_exe(Path("D:/Bambu Studio/BAMBU-STUDIO.exe"))
    assert kind is not None and kind.id == "bambu_studio", f"大写名没认出来：{kind}"
    assert slicers.classify_exe(Path("C:/Windows/System32/notepad.exe")) is None, "不该随便认陌生程序"
    # 表可以宽（多认几个是好事），但表序就是界面文案的顺序：前几个必须是最主流的
    assert len(slicers.SLICER_KINDS) >= 8, f"只认 {len(slicers.SLICER_KINDS)} 种，少了"
    head = [k.id for k in slicers.SLICER_KINDS[:4]]
    assert head == ["bambu_studio", "orcaslicer", "prusa_slicer", "cura"], f"界面只报前 4 个名字，它们必须最主流：{head}"
    flags = [k.verified for k in slicers.SLICER_KINDS]
    assert flags == [True] * sum(flags) + [False] * (len(flags) - sum(flags)), (
        "本机真发过一次的必须排在表最前面（没实测过的不能排在前面给用户当首选）"
    )

    # 2) 注册表原文解析：全是实测或官方安装包里的形态
    got = slicers.parse_open_command('"D:\\Bambu Studio\\bambu-studio.exe" "%1"', require_exists=False)
    assert got == Path("D:\\Bambu Studio\\bambu-studio.exe"), got
    got = slicers.parse_open_command(
        '"C:\\Program Files\\SoftFever\\orca-slicer.exe" --load "%1"', require_exists=False
    )
    assert got == Path("C:\\Program Files\\SoftFever\\orca-slicer.exe"), got
    assert slicers.parse_open_command('"%1"', require_exists=False) is None, "只有占位符时不能返路径"
    assert slicers.parse_open_command(
        'rundll32.exe "C:\\x\\a.dll",Open "%1"', require_exists=False
    ) is None, "rundll32 只是壳，拿着启动等于什么也没打开"
    assert slicers.parse_open_command(
        '"C:\\Users\\me\\AppData\\Local\\Packages\\App_8wekyb3d8bbwe\\WindowsApps\\bambu.exe" "%1"',
        require_exists=False,
    ) is None, "UWP 不能这样直接启动（本机 .3mf 的 UserChoice 正是这种）"
    with tempfile.TemporaryDirectory() as td:
        spaced = Path(td) / "Program Files" / "Bambu Studio"
        spaced.mkdir(parents=True)
        real_exe = spaced / "bambu-studio.exe"
        real_exe.write_bytes(b"MZ")
        # 路径含空格又没引号：靠磁盘一段段试出来（不能硬猜成 `D:\\Bambu`）
        got = slicers.parse_open_command(f"{real_exe} \"%1\"", require_exists=True)
        assert got == real_exe, f"没引号的含空格路径应当靠磁盘拼全：{got}"
        assert slicers.parse_open_command(f"{real_exe} \"%1\"", require_exists=False) is None, (
            "不查磁盘时不能猜路径在哪结束，宁可不报"
        )
    assert slicers.parse_display_icon('"C:\\x\\a.exe",0') == "C:\\x\\a.exe"
    assert slicers.parse_display_icon("C:\\Program Files\\App, Inc\\a.exe,1") == "C:\\Program Files\\App, Inc\\a.exe", (
        "只有逗号后面是纯数字才当图标序号，带逗号的目录名不能切错"
    )

    # 3) 常见安装目录：通配命中、大小写无关、不能乱深挖
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        nested = root / "UltiMaker Cura 5.9.0" / "UltiMaker Cura 5.9.0"  # Cura 会再套一层版本号
        nested.mkdir(parents=True)
        (nested / "cura.exe").write_bytes(b"MZ")
        matched = slicers.match_known_dir(root, "ultimaker cura*")
        assert matched is not None and matched.name.startswith("UltiMaker Cura"), matched
        cura_kind = slicers.classify_exe(Path("x/cura.exe"))
        assert cura_kind is not None
        found = slicers.exe_in_dir(matched, cura_kind)
        assert found is not None and found.name.casefold() == "cura.exe", found
        assert slicers.match_known_dir(root, "prusaslicer") is None, "目录对不上就是不能命中"
        deep = root / "deep" / "a" / "b"
        deep.mkdir(parents=True)
        (deep / "bambu-studio.exe").write_bytes(b"MZ")
        bambu_kind = slicers.classify_exe(Path("x/bambu-studio.exe"))
        assert bambu_kind is not None
        assert slicers.exe_in_dir(root / "deep", bambu_kind) is None, (
            "只往下看一层；愿意扫整盘就能把任何目录当成安装目录"
        )

    # 4) 合并与去重：替掉候选来源，只看规则
    real_pairs = slicers._candidate_pairs  # noqa: SLF001 - 自检就为验这个合并规则
    slicers.invalidate_cache()
    slicers._candidate_pairs = lambda: [  # noqa: SLF001
        ("known_dir", Path("D:/Bambu Studio/bambu-studio.exe"), None),
        ("uninstall", Path("D:/Bambu Studio/bambu-studio.exe"), "02.08.02.61"),
        ("association", Path("D:/orcaslicer/orca-slicer.exe"), None),
        ("uninstall", Path("C:/Windows/System32/notepad.exe"), None),
        ("custom", Path("D:/绿色软件/my-slicer.exe"), None),
    ]
    import aiforge.config as config_mod

    real_store = config_mod.store
    fake_cfg = SimpleNamespace(slicers=SimpleNamespace(default_id="ghost", custom_paths=[]))
    config_mod.store = lambda: SimpleNamespace(config=fake_cfg)  # type: ignore[assignment]
    try:
        items = slicers.scan(force=True)
        ids = [item.id for item in items]
        assert ids == ["bambu_studio", "orcaslicer", "ext:my-slicer"], f"去重/丢弃/排序不对：{ids}"
        assert items[0].version == "02.08.02.61", "同一软件：版本从有版本的来源补齐，但不能因此多出一条"
        assert items[0].source == "known_dir", "先命中的来源保留说明（同一软件不抢名字）"
        shape = slicers.describe()
        assert shape["count"] == len(shape["slicers"]) == 3, shape
        assert shape["default_id"] is None, "记过的软件被卸载了要回到「没有默认」，不能把无效 id 发给界面"
        assert len(shape["recognized_names"]) >= 8, "没检测到时界面要能告诉用户装哪些就行"
        assert set(shape["slicers"][0]) == {"id", "name", "exe", "version", "source_label", "running"}, (
            f"字段就是界面用得上的那几个，别偷偷加：{sorted(shape['slicers'][0])}"
        )
        try:
            slicers.resolve("ghost")
            raise AssertionError("未知 id 必须 LookupError（接口据此回 409，绝不照着请求里的路径启动）")
        except LookupError:
            pass
        assert slicers.resolve(None).id == "bambu_studio", "默认目标没了就回落到第一个"
        fake_cfg.slicers.default_id = "orcaslicer"
        assert slicers.resolve(None).id == "orcaslicer", "不点名时必须用默认目标"
    finally:
        config_mod.store = real_store  # type: ignore[assignment]

    # 5) 送出去的快照：只留最近几份，而且**绝不能把刚送出去那份删掉**
    with tempfile.TemporaryDirectory() as td:
        proj_dir = Path(td)
        source = proj_dir / "model.3mf"
        source.write_bytes(b"fake-3mf")
        (proj_dir / "model.stl").write_bytes(b"fake-stl")
        project = SimpleNamespace(
            dir=proj_dir,
            three_mf_path=source,
            stl_path=proj_dir / "model.stl",
            name="滤镜转接环",
            revision=7,
        )
        target = slicers.stage_file(project, "3mf")
        assert target.name == "滤镜转接环-r07.3mf", target.name
        assert target.parent.name == "slicer" and target != source, (
            "交出的是快照，不是 model.3mf 本体（切片软件会长期占用它）"
        )
        staged_stl = slicers.stage_file(project, "stl")
        for revision in range(1, 12):  # 反复送样：这些快照的 mtime 全都被复制成了源文件那个旧时间
            project.revision = revision
            newest = slicers.stage_file(project)
        left = sorted(p.name for p in (proj_dir / "slicer").glob("*.3mf"))
        assert newest.name == "滤镜转接环-r11.3mf" and newest.is_file(), (
            f"刚送出去的那份被自己删掉了（切片软件马上就会报文件不存在）：{left}"
        )
        assert left == [f"滤镜转接环-r{r:02d}.3mf" for r in (7, 8, 9, 10, 11)], (
            f"快照应该只留版本号最大的那几份（实际 {left}）"
        )
        assert len(left) == slicers.STAGE_KEEP, f"快照只该留 {slicers.STAGE_KEEP} 份，实际 {len(left)}：{left}"
        assert staged_stl.is_file(), "清 3mf 快照不能顺手删掉 stl 的"
    slicers._candidate_pairs = real_pairs  # noqa: SLF001
    slicers.invalidate_cache()
    return (
        f"{len(slicers.SLICER_KINDS)} 种识别（实测过的排前面）/注册表原文解析/壳与 UWP 不认/"
        f"同软件去重不重复/未知 id 拒绝/快照只留 {slicers.STAGE_KEEP} 份且不动刚送出那份"
    )


def t_social_footer_links() -> str:
    """底部作者账号栅：前端链接必须真能在原生窗口里打开（离线）。

    为什么需要这条：WebView2 拦截新窗口，前端唯一可靠的打开方式是后端
    /api/app/open-external，而后端有 https 域名白名单。白名单漏加时，
    用户看到的是「点了没反应」（catch 里的 window.open 同样被拦），
    所以前端清单与后端白名单必须一起校验，不允许各改各的。
    """
    import re as _re
    from urllib.parse import urlparse

    from aiforge.api.settings_routes import ALLOWED_EXTERNAL_HOSTS

    src = (ROOT / "webui" / "src" / "components" / "SocialFooter.tsx").read_text(encoding="utf-8")
    urls = _re.findall(r"url:\s*'(https?://[^']+)'", src)
    assert len(urls) >= 2, f"底部入口至少要有两个链接，实际 {urls}"
    for url in urls:
        parsed = urlparse(url)
        assert parsed.scheme == "https", url
        host = (parsed.hostname or "").lower()
        assert host in ALLOWED_EXTERNAL_HOSTS, f"{host} 不在后端白名单，原生窗口点了会没反应"
    # 文案：作者要求按「平台：@账号」展示，改丢了就是入口看不见
    assert "@不会拍的锡" in src and "@xiiiiii422" in src, "底部账号名被改动"
    # 全局可见：必须挂在外壳 App.tsx 里，而不是只做在某个页面上
    app = (ROOT / "webui" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "<SocialFooter />" in app, "底部栅没挂进外壳，只有某页能看到"
    # 兜底链：open-external 失败要退到 window.open，否则纯浏览器模式下点不动
    assert "window.open(" in src, "缺少 window.open 兜底"
    return f"{len(urls)} 个链接全部 https 且在白名单；账号名/外壳挂载/兜底齐全"



def t_requirement_tests() -> str:
    """需求可执行测试（对标 CADTests：PR/RS/IR + 失败日志回喂）。离线，不跑 OpenSCAD。

    为什么必须单独一条用例：这一层的全部价值就是「把需求变成能判定的东西」，
    如果它自己没有被判定，就等于又回到「看起来对就行」。
    这里锁四件事：
      1. 指标口径（全过才 PR=1；invalid 算失败并计入 IR；manual 不参与打分）；
      2. **契约目标优先**（用户授权改成 50 后，绝不能拿旧规格 100 判失败 ——
         这是实测踩过的最恶劣回归：判失败 → AI 把模型改回 100 → 用户看到"改了没反应"）；
      3. invalid 只摊给用户看，**不驱动修复**（给 AI 一句"量不到孔"它就去改没坏的东西）；
      4. 与 jobs 的判据**单向一致**：jobs 报出来的尺寸问题，清单里必须也是 fail。
    """
    from aiforge import jobs as jobs_mod
    from aiforge import mesh, reqtests
    from aiforge.design import DesignSpec, SpecItem

    wall_ok = mesh.WallThickness(min_mm=1.9, p05_mm=2.1, median_mm=2.4, samples=400)

    def mk(**kw):
        m = mesh.MeshMetrics(triangles=4000, vertices=2000)
        for k, v in kw.items():
            setattr(m, k, v)
        return m

    spec = DesignSpec(
        kind="笔筒",
        specs=[
            SpecItem(name="height", label="总高", value=100.0, user_specified=True),
            SpecItem(name="hole_d", label="线孔直径", value=8.0, user_specified=True),
        ],
        features=["背面有穿孔"],
    )
    dims = {2: ("总高", 100.0, "height")}
    _planar_probe = jobs_mod.spec_planar_dimensions(
        DesignSpec(
            kind="长方体",
            specs=[
                SpecItem(name="length", label="长度", value=60.0, user_specified=True),
                SpecItem(name="width", label="宽度", value=40.0, user_specified=True),
                SpecItem(name="wall", label="壁厚", value=3.0, user_specified=True),
                SpecItem(name="hole_w", label="孔宽", value=5.0, user_specified=True),
            ],
        )
    )
    base = dict(
        spec=spec,
        requirement="做一个笔筒，总高100mm",
        limits=None,
        declared_dimensions=dims,
        declared_holes=[],
        spec_items=list(spec.specs),
    )

    def dim_row(**kw):
        suite = kw.pop("suite_obj")
        return [t for t in suite.tests if t.group == "dim:height"][0]

    limits = mesh.AnalysisLimits()  # 工艺类判据要有机型，否则那几条不会生成

    # --- 1. 全部达成 -------------------------------------------------------
    good = mk(
        bbox=(60.0, 60.0, 100.0), watertight=True, boundary_edges=0,
        solid_parts=1, stray_parts=0, wall=wall_ok, sits_on_plate=True,
    )
    suite = reqtests.evaluate(metrics=good, **base)
    summary = suite.summary()
    assert summary["pass_rate"] == 1.0, summary
    assert summary["requirement_score"] == 1.0, summary
    assert summary["invalid_ratio"] == 0.0, summary
    assert suite.repair_lines() == [], suite.repair_lines()
    # 语义需求标 manual，不参与打分 —— 否则「外观对不对」会把 PR 永远压成 0
    assert any(t.status == reqtests.MANUAL for t in suite.tests), "语义需求没有标 manual"
    assert all(t.status != reqtests.MANUAL or not t.scored for t in suite.tests)

    # --- 1b. 螺纹三态：True=过 / False=不过 / None=没能判定（绝不粉饰成通过）---
    # mesh.detect_helix_thread 的 docstring 自己写着「样本不足时返回 None（不猜）」，
    # 而旧判据只拦 `is_helix is False` —— None 落进 else 被当成真螺旋，给用户的
    # 判定表上多一个根本没人做过的结论（而且它永不驱动修复）。
    for helix, want_status in (
        (True, reqtests.PASS),
        (False, reqtests.FAIL),
        (None, reqtests.INVALID),
    ):
        m_thread = mk(
            bbox=(60.0, 60.0, 100.0), watertight=True, boundary_edges=0,
            solid_parts=1, stray_parts=0, wall=wall_ok, sits_on_plate=True,
            thread_depth_mm=0.46, thread_is_helix=helix,
        )
        s_thread = reqtests.evaluate(
            metrics=m_thread, **dict(base, requirement="做一个能拧得上的螺纹转接环")
        )
        rows = [t for t in s_thread.tests if t.group == "thread:real"]
        assert rows, f"需求里点了螺纹却没出螺纹那一行（helix={helix}）"
        row = rows[0]
        assert row.status == want_status, f"helix={helix} 应该判 {want_status}，实际 {row.status}：{row.actual}"
        if helix is None:
            assert "真螺旋" not in row.actual, f"没能判定的项把结论写成了事实：{row.actual}"
            assert row not in s_thread.blocking_failures, "没能判定的项不许驱动修复循环"
            assert row in s_thread.failures, "invalid 必须算失败（论文口径：不许拿没测当通过）"
            assert s_thread.summary()["invalid_ratio"] > 0.0, s_thread.summary()
            assert s_thread.summary()["pass_rate"] == 0.0, "没能判定却整件 PR=1 = 假绿复活"

    # --- 1c. 贯通三态：两端都开口=True 过 / 两端都没开口=False 不过 / 只开一面或没测完=None 不报 ---
    # mesh 新加的 `hole_through` 把「剖面某些高度命中了但根本没开口」这种情况与
    # 「真通孔」「只开一面的合法盲孔」分开。以前只看「任一高度命中」，密封腔体会被
    # 当成孔径做到了（拿一个插不进东西、漏不出东西的内部空腔去匹配用户的孔需求）。
    hole_base = dict(base)
    hole_base["declared_holes"] = [("线孔直径", 8.0, "hole_d")]
    for through, want_status in (
        (True, None),        # 真通孔：不新增那行“必须是开口的”判定
        (False, reqtests.FAIL),  # 两端都没开口，中间命中：必须报
        (None, None),         # 只开一面或没测完：不强行报，也不假装它是真通孔
    ):
        m_hole = mk(
            bbox=(60.0, 60.0, 100.0), watertight=True, boundary_edges=0,
            solid_parts=1, stray_parts=0, wall=wall_ok, sits_on_plate=True,
            hole_profile=([8.0] * 6 if through is True else [None, None, 8.0, 8.0, 8.0, None]),
            hole_through=through,
        )
        s_hole = reqtests.evaluate(
            metrics=m_hole, **dict(hole_base, requirement="做一个笔筒，背面有 8mm 的穿线孔")
        )
        sealed_rows = [t for t in s_hole.tests if t.group == "hole:sealed-void"]
        if want_status is None:
            assert not sealed_rows, f"hole_through={through} 不该多出『必须是开口的』这一行"
        else:
            assert sealed_rows and sealed_rows[0].status == want_status, (
                f"hole_through={through} 应该判 {want_status}，实际 {[t.status for t in sealed_rows]}"
            )
            assert sealed_rows[0] in s_hole.blocking_failures, "确认是封闭腔体时必须驱动修复（与螺纹三态里 None 的处理相反）"

    # --- 2. 尺寸偏小：判定 + 回喂日志带数字与公差 ---------------------------
    short = mk(
        bbox=(60.0, 60.0, 92.4), watertight=True, solid_parts=1,
        wall=wall_ok, sits_on_plate=True,
    )
    suite2 = reqtests.evaluate(metrics=short, **base)
    row = dim_row(suite_obj=suite2)
    assert row.status == reqtests.FAIL, row.to_dict()
    assert abs(row.deviation_pct + 7.6) < 0.2, row.to_dict()
    assert row.tolerance_mm and row.tolerance_mm >= 2.0, row.to_dict()  # 2% 兜底公差
    log_text = "\n".join(suite2.repair_lines())
    assert "总高" in log_text and "92.4" in log_text and "±" in log_text, log_text
    assert suite2.summary()["pass_rate"] == 0.0, suite2.summary()

    # --- 2.5 水平尺寸整组择优配对（第一版质量基准抓到的假失败）-------------
    # 「长 60、宽 40」做成 60(X)×40(Y) 与做成 40(X)×60(Y) 是同一件东西转了 90°，
    # 按固定轴比对会把正确的模型判成两条未达成 —— 假失败的代价是用户不再信这张表。
    block_dims = {0: ("宽度", 40.0, "width"), 1: ("长度", 60.0, "length")}
    planar = [("宽度", 40.0, "width"), ("长度", 60.0, "length")]
    for bbox_xy, note in (((60.0, 40.0), "长边放在 X"), ((40.0, 60.0), "长边放在 Y")):
        rotated = mk(
            bbox=(bbox_xy[0], bbox_xy[1], 20.0), watertight=True, solid_parts=1,
            wall=wall_ok, sits_on_plate=True,
        )
        srows = reqtests.evaluate(
            spec=spec, metrics=rotated, requirement="长60宽40高20", limits=None,
            declared_dimensions=block_dims, planar_dimensions=planar,
            spec_items=list(spec.specs),
        )
        dim_tests = [t for t in srows.tests if t.group.startswith("dim:")]
        assert len(dim_tests) == 2, (note, [t.group for t in dim_tests])
        assert all(t.status == reqtests.PASS for t in dim_tests), (
            note, [t.to_dict() for t in dim_tests]
        )
    # 真的做错了必须判失败：宽 40 做成 50（两个方向都救不回来）
    wrong = mk(
        bbox=(60.0, 50.0, 20.0), watertight=True, solid_parts=1,
        wall=wall_ok, sits_on_plate=True,
    )
    wrows = [
        t for t in reqtests.evaluate(
            spec=spec, metrics=wrong, requirement="长60宽40高20", limits=None,
            declared_dimensions=block_dims, planar_dimensions=planar,
            spec_items=list(spec.specs),
        ).tests
        if t.group.startswith("dim:")
    ]
    assert any(t.status == reqtests.FAIL for t in wrows), [t.to_dict() for t in wrows]
    # 「长度」以前从来没被核对过（第一版质量基准抓到的覆盖洞）：
    # 现在由 spec_planar_dimensions 兜住，但**不许**进按轴记账的那份判据 ——
    # 那份要喂给逐轴缩放，方向猜错就会把模型放大一倍。
    _planar_names = {name for _label, _v, name in _planar_probe}
    assert {"length", "width"} <= _planar_names, _planar_probe
    assert not (_planar_names & {"wall", "hole_w"}), f"局部尺寸混进了整体尺寸判据：{_planar_probe}"
    _axis_dims = jobs_mod._spec_declared_dimensions(
        DesignSpec(
            kind="长方体",
            specs=[SpecItem(name="length", label="长度", value=60.0, user_specified=True)],
        )
    )
    assert 1 not in _axis_dims, f"长度不该进按轴缩放判据：{_axis_dims}"

    # --- 2.4 工艺建议不参与打分；壁厚判据不被棱边切向采样骗到 --------------
    # 实测第一轮基准：Ø50×5 的垫片射线 p05 只有 0.29mm（打到棱边上），中位 5mm。
    # 只拿 p05 判"壁厚不达标"会把完全合格的件报成失败 —— 假失败的代价是用户不信这张表。
    def _rim(p05: float, median: float):
        return reqtests.evaluate(
            metrics=mk(
                bbox=(50.0, 50.0, 5.0), watertight=True, solid_parts=1, sits_on_plate=True,
                wall=mesh.WallThickness(min_mm=0.2, p05_mm=p05, median_mm=median, samples=400),
            ),
            limits=limits, spec=spec, requirement="总高100mm",
            declared_dimensions={}, declared_holes=[], spec_items=list(spec.specs),
        )

    rim = _rim(0.29, 5.0)
    wall_row = [t for t in rim.tests if t.group == "print:min-wall"][0]
    assert wall_row.status == reqtests.PASS, wall_row.to_dict()
    assert "中位" in wall_row.actual, wall_row.to_dict()
    # advisory 不参与 PR/RS：用户要的全做到时，不该因为一条工艺建议把整件判 0
    assert all(not t.scored for t in rim.tests if t.advisory), "咨询项仍在参与打分"
    assert rim.summary()["pass_rate"] == 1.0, rim.summary()
    # 真做薄了必须判失败（中位也低）
    assert [t for t in _rim(0.4, 0.6).tests if t.group == "print:min-wall"][0].status \
        == reqtests.FAIL, "整体偏薄必须报"

    # --- 2.5 水平尺寸整组择优配对（第一轮基准抓到的假失败 + 假绿）----------
    # 「长 60、宽 40」做成 60(X)×40(Y) 与 40(X)×60(Y) 是同一件东西转了 90°：
    # 按固定轴比对会把正确模型判成两条未达成；而只核对宽和高又会把
    # 「做成了 40×40」放过去（第一轮实测真的发生了）。
    block_dims = {0: ("宽度", 40.0, "width"), 1: ("长度", 60.0, "length")}
    planar = [("宽度", 40.0, "width"), ("长度", 60.0, "length")]
    for bx, by, note in ((60.0, 40.0, "长边在 X"), (40.0, 60.0, "长边在 Y")):
        pair = reqtests.evaluate(
            metrics=mk(
                bbox=(bx, by, 20.0), watertight=True, solid_parts=1,
                wall=wall_ok, sits_on_plate=True,
            ),
            spec=spec, requirement="长60宽40高20", limits=None,
            declared_dimensions=block_dims, planar_dimensions=planar,
            spec_items=list(spec.specs),
        )
        dims_rows = [t for t in pair.tests if t.group.startswith("dim:")]
        assert len(dims_rows) == 2, (note, [t.group for t in dims_rows])
        assert all(t.status == reqtests.PASS for t in dims_rows), (
            note, [t.to_dict() for t in dims_rows]
        )
    # 配对后的方向要回传给逐轴校正：两层必须用同一套事实
    pair_targets = reqtests.evaluate(
        metrics=mk(
            bbox=(60.0, 40.0, 20.0), watertight=True, solid_parts=1,
            wall=wall_ok, sits_on_plate=True,
        ),
        spec=spec, requirement="长60宽40高20", limits=None,
        declared_dimensions=block_dims, planar_dimensions=planar,
        spec_items=list(spec.specs),
    ).dimension_targets()
    assert {a: (l, v) for a, (l, v, _n) in pair_targets.items()} == {
        0: ("宽度", 40.0), 1: ("长度", 60.0)
    } or {a: (l, v) for a, (l, v, _n) in pair_targets.items()} == {
        0: ("长度", 60.0), 1: ("宽度", 40.0)
    }, pair_targets
    assert {name for _l, _v, name in jobs_mod.spec_planar_dimensions(
        DesignSpec(
            kind="长方体",
            specs=[
                SpecItem(name="length", label="长度", value=60.0, user_specified=True),
                SpecItem(name="width", label="宽度", value=40.0, user_specified=True),
                SpecItem(name="wall", label="壁厚", value=3.0, user_specified=True),
                SpecItem(name="hole_w", label="孔宽", value=5.0, user_specified=True),
            ],
        )
    )} == {"length", "width"}, "长度没进核对判据，或局部尺寸混进了整体判据"
    # 真的做错仍要判失败：宽 40 做成 50，两个方向都救不回来
    wrong = [
        t for t in reqtests.evaluate(
            metrics=mk(
                bbox=(60.0, 50.0, 20.0), watertight=True, solid_parts=1,
                wall=wall_ok, sits_on_plate=True,
            ),
            spec=spec, requirement="长60宽40高20", limits=None,
            declared_dimensions=block_dims, planar_dimensions=planar,
            spec_items=list(spec.specs),
        ).tests
        if t.group.startswith("dim:")
    ]
    assert any(t.status == reqtests.FAIL for t in wrong), [t.to_dict() for t in wrong]

    # --- 3. 与 jobs 判据单向一致：jobs 报了，清单也必须判 fail --------------
    way_short = mk(
        bbox=(60.0, 60.0, 62.0), watertight=True, solid_parts=1,
        wall=wall_ok, sits_on_plate=True,
    )
    problems = "\n".join(
        jobs_mod._geometry_integrity_problems(way_short, spec, "", None, "总高100mm", None)
    )
    assert "总高" in problems, problems[:200]  # jobs 确实报了这条尺寸问题
    row3 = dim_row(suite_obj=reqtests.evaluate(metrics=way_short, **base))
    assert row3.status == reqtests.FAIL, row3.to_dict()

    # --- 4. 偏大：附加结构可解释（≤10%）不判失败，超出才判 ------------------
    ok_big = mk(
        bbox=(60.0, 60.0, 104.0), watertight=True, solid_parts=1,
        wall=wall_ok, sits_on_plate=True,
    )
    assert dim_row(suite_obj=reqtests.evaluate(metrics=ok_big, **base)).status == reqtests.PASS
    too_big = mk(
        bbox=(60.0, 60.0, 130.0), watertight=True, solid_parts=1,
        wall=wall_ok, sits_on_plate=True,
    )
    assert dim_row(suite_obj=reqtests.evaluate(metrics=too_big, **base)).status == reqtests.FAIL

    # --- 5. 契约目标优先（历史 Bug 的回归位）--------------------------------
    class _Contract:
        changed = ["height"]
        unchanged = []
        frozen = []
        target_values = {"height": 50.0}

    m50 = mk(
        bbox=(60.0, 60.0, 50.0), watertight=True, solid_parts=1,
        wall=wall_ok, sits_on_plate=True,
    )
    row50 = dim_row(suite_obj=reqtests.evaluate(metrics=m50, contract=_Contract(), **base))
    assert row50.status == reqtests.PASS, f"契约目标没盖过旧规格：{row50.to_dict()}"
    assert row50.expected == "50mm", row50.to_dict()

    # --- 6. 量不到 = invalid：计入 IR，但不进回喂日志 ------------------------
    holes = reqtests.evaluate(
        metrics=mk(
            bbox=(60.0, 60.0, 100.0), watertight=True, solid_parts=1,
            wall=wall_ok, sits_on_plate=True, hole_profile=[None] * 6,
        ),
        spec=spec,
        requirement="做一个笔筒，总高100mm",
        limits=None,
        declared_dimensions=dims,
        declared_holes=[("线孔直径", 8.0, "hole_d")],
        spec_items=list(spec.specs),
    )
    hole_rows = [t for t in holes.tests if t.group == "hole:hole_d"]
    assert hole_rows and hole_rows[0].status == reqtests.INVALID, hole_rows[0].to_dict()
    assert holes.summary()["invalid_ratio"] > 0, holes.summary()
    assert "线孔直径" not in "\n".join(holes.repair_lines()), "没测出来的项不该去驱动修复"
    assert holes.summary()["pass_rate"] == 0.0, "invalid 按论文口径算失败，不许当通过"

    # 剖面命中 → pass
    hole_hit = reqtests.evaluate(
        metrics=mk(
            bbox=(60.0, 60.0, 100.0), watertight=True, solid_parts=1,
            wall=wall_ok, sits_on_plate=True, hole_profile=[None, 8.0, None],
        ),
        spec=spec,
        requirement="总高100mm，线孔直径8mm",
        limits=None,
        declared_dimensions=dims,
        declared_holes=[("线孔直径", 8.0, "hole_d")],
        spec_items=list(spec.specs),
    )
    assert [t for t in hole_hit.tests if t.group == "hole:hole_d"][0].status == reqtests.PASS

    # --- 7. 薄膜/散件/要螺纹 的判定（都是实测翻车过的类） --------------------
    film = reqtests.evaluate(
        metrics=mk(
            bbox=(60.0, 60.0, 100.0), watertight=True, solid_parts=1,
            wall=mesh.WallThickness(min_mm=0.01, p05_mm=0.01, median_mm=2.4, samples=400),
            sits_on_plate=True,
        ),
        **base,
    )
    assert any(t.group == "integrity:no-film" and t.status == reqtests.FAIL for t in film.tests)

    parts = reqtests.evaluate(
        metrics=mk(
            bbox=(60.0, 60.0, 100.0), watertight=True, solid_parts=3, stray_parts=2,
            wall=wall_ok, sits_on_plate=True,
        ),
        **base,
    )
    assert any(t.group == "integrity:one-body" and t.status == reqtests.FAIL for t in parts.tests)

    threadless = reqtests.evaluate(
        metrics=mk(
            bbox=(60.0, 60.0, 100.0), watertight=True, solid_parts=1,
            wall=wall_ok, sits_on_plate=True,
        ),
        spec=spec,
        requirement="做一个 67 转 77 滤镜转接环，带螺纹",
        limits=None,
        declared_dimensions={},
        declared_holes=[],
        spec_items=list(spec.specs),
    )
    assert any(t.group == "thread:real" and t.status == reqtests.FAIL for t in threadless.tests), \
        "要螺纹却量不到时也必须进清单（jobs 那边已有问题 7）"
    # 单向一致：jobs 报了螺纹问题，清单里同样是 fail
    thread_problems = "\n".join(
        jobs_mod._geometry_integrity_problems(
            mk(bbox=(60.0, 60.0, 100.0), watertight=True, solid_parts=1, wall=wall_ok),
            None, "", None, "做一个 67 转 77 滤镜转接环，带螺纹", None,
        )
    )
    assert "螺纹" in thread_problems and thread_problems, thread_problems[:120]

    # --- 8. 网格根本没有 → 全表 invalid -----------------------------------
    empty = reqtests.evaluate(spec=spec, metrics=None, requirement="x")
    assert empty.summary()["invalid_ratio"] == 1.0, empty.summary()
    assert empty.summary()["pass_rate"] == 0.0, empty.summary()

    # --- 9. 接线：修复回喂与落盘都带上清单 ---------------------------------
    import inspect

    src = inspect.getsource(jobs_mod.GenerationRunner._repair_geometry_integrity)
    assert "_requirement_suite(" in src and "repair_lines()" in src, "修复循环没接需求核对表"
    src_finish = inspect.getsource(jobs_mod.GenerationRunner._finish)
    assert "requirement_suite" in src_finish, "结果里没有需求核对表"
    src_persist = inspect.getsource(jobs_mod.GenerationRunner._persist)
    assert "_metrics_payload(" in src_persist, "核对表没随项目指标落盘（重开项目就丢了）"
    # 方向要统一供给：问题段落、逐轴校正、采纳比较三处都用配对后的方向
    assert "dimension_targets()" in src, "逐轴校正仍按固定轴判，会把正确的模型越修越歪"
    assert "dim_targets" in inspect.getsource(jobs_mod._geometry_integrity_problems), \
        "尺寸问题段落没接配对方向"
    # 偏 2%~10% 这种「不报错但也没做到」的尺寸，必须仍然能走到确定性逐轴校正：
    # 第二轮基准实测到旧的 early-return 把它整段跳过（要 60×40 做出 56×36 照样交付）。
    repair_src = src  # 上方已取：GenerationRunner._repair_geometry_integrity
    assert "if not problems:\n            return source, compile_result, metrics" not in repair_src, \
        "提前 return 会让逐轴校正永远轮不到"
    assert "if problems:" in repair_src and "for attempt in (1, 2):" in repair_src, \
        "AI 修复轮没有被 problems 门住（要么白烧调用，要么无条件重做）"

    return (
        "PR/RS/IR 口径正确（工艺建议不打分、invalid 不驱动修复）；契约目标优先；"
        "水平尺寸整组择优配对并回传逐轴校正；与 jobs 判据单向一致；落盘已接线"
    )


def t_solid_bottom_verdict() -> str:
    """实心底与开口方向的判定分档（离线，只喂合成 metrics）。

    为什么不并进「需求可执行测试」那一项：那项已有 40 多个断言，而这里的
    **每一档都对应一类真实翻车**（31 份历史版本里底是开口的 18 份：14 两端开、
    4 上下反），单独立项能让失败信息直接说出「哪一档判错了」。

    锁六件事：
      1. 五档分得开：PASS / 两端开 FAIL / 方向反 FAIL / 盖子 MANUAL+advisory / INVALID；
      2. 只有 FAIL 驱动修复（盖子不许被修复循环封底，量不到不许驱动修复）；
      3. 需求里没有「底」字时这条**完全不出场**（套管/环形件天生两头通）；
      4. 大孔件按实测孔径放宽（同样是 20% 材料：有 Ø45 通孔=过，没孔=不过）；
      5. 门槛只有一把尺子：reqtests 不许自己另写一个占比常数，必须从 mesh 拿；
      6. 接线：需求原文要含「项目最初那句诉求」，否则改一句「打个通到底的孔」
         就把门控丢掉了（实测：那一步的指令里根本没得词表里的说法）。
    """
    import inspect
    import re as _re
    from types import SimpleNamespace

    from aiforge import jobs as jobs_mod
    from aiforge import mesh, reqtests

    # 需求文案直接用用户真实那句（history.jsonl 第一条），而不是编造一句：
    # 词表能不能命中全看原文，编的句子验不出真东西。
    REQ_BOTTOM_TOP = (
        "设计一个圆形的桌面笔筒：外径 80mm、总高 100mm、壁厚 2.5mm，"
        "底部是封闭的实心底，顶部敞开。"
    )
    REQ_BOTTOM_ONLY = "做个圆形收纳筒：外径 80mm、总高 100mm，底部是封闭的实心底。"
    REQ_NO_BOTTOM = "做个圆形套管：外径 80mm、内径 74mm、总高 100mm。"

    def mk(**kw):
        footprint = kw.pop("footprint", 80.0)
        m = mesh.MeshMetrics(
            triangles=4000, vertices=2000,
            bbox=(footprint, footprint, 100.0),
            bbox_min=(0.0, 0.0, 0.0), bbox_max=(footprint, footprint, 100.0),
            watertight=True, boundary_edges=0, solid_parts=1, stray_parts=0,
            sits_on_plate=True, volume_mm3=1000.0,
        )
        m.hole_profile = list(kw.pop("hole_profile", [None] * 6))
        for k, v in kw.items():
            setattr(m, k, v)
        return m

    def suite_of(metrics, requirement):
        return reqtests.evaluate(
            spec=None, metrics=metrics, requirement=requirement, limits=None,
            declared_dimensions={}, declared_holes=[], spec_items=[],
        )

    def row_of(metrics, requirement):
        rows = [t for t in suite_of(metrics, requirement).tests if t.group == "bottom:solid"]
        return rows[0] if rows else None

    # 三组读数就是 OpenSCAD 夹具的真实测值（见「孔径射线实测」）：0.958/0.085、
    # 0.085/0.085、0.085/1.0。两边差一个数量级，所以这些数字**不是凑的**。
    SOLID = mk(bottom_coverage=0.958, top_coverage=0.085)
    TUBE = mk(bottom_coverage=0.085, top_coverage=0.085)
    INVERTED = mk(bottom_coverage=0.085, top_coverage=1.0)

    # --- 1. 做对了就必须 PASS，而且两种需求文案都要能门控 -------------------
    for req in (REQ_BOTTOM_TOP, REQ_BOTTOM_ONLY):
        r = row_of(SOLID, req)
        assert r is not None and r.status == reqtests.PASS, (
            f"封了底还带 Ø10 排水孔的件被判成 {r and r.status}（{r and r.actual}）："
            "正确修改会被说成「没改到」"
        )
        assert not r.advisory and r.scored and "95.8" in r.actual and "8.5" in r.actual, r.to_dict()

    # --- 2. 两端都开：必须拦住，而且要驱动修复 ------------------------------
    r = row_of(TUBE, REQ_BOTTOM_TOP)
    assert r.status == reqtests.FAIL, f"开口管被判成 {r.status}：笔放进去就漏"
    assert "两端都开着" in r.actual and "z=0" in r.fix_hint, r.to_dict()
    s = suite_of(TUBE, REQ_BOTTOM_TOP)
    assert any(t.group == "bottom:solid" for t in s.blocking_failures), "底开了却没进拦住名单"
    assert "实心底" in "\n".join(s.repair_lines()), "底开了却没回喂 AI（修循环不知道要补底）"

    # --- 3. 上下反了：能拦，而且给的是翻转改法 ----------------------------
    r = row_of(INVERTED, REQ_BOTTOM_TOP)
    assert r.status == reqtests.FAIL, f"倒过来建的杯被判成 {r.status}（需求明写了顶部要开）"
    assert "方向反了" in r.actual, r.to_dict()
    assert "上下正好反了" in r.fix_hint, r.fix_hint

    # --- 4. 盖子：同一个读数却不能拦住修 ------------------------------------
    # 需求没说顶部要开 → 底开着顶封着就是「开口朝下」的盖子/罩壳。
    # 拿「补底」去要它，修复循环会把一个本来正确的盖子改成塞子。
    r = row_of(INVERTED, REQ_BOTTOM_ONLY)
    assert r.status == reqtests.MANUAL and r.advisory, (
        f"盖子被判成 {r.status}（advisory={r.advisory}）：要么误拦修复，要么默默放行"
    )
    assert not r.scored, "manual/advisory 还在参与 PR/RS 打分"
    s = suite_of(INVERTED, REQ_BOTTOM_ONLY)
    assert all(t.group != "bottom:solid" for t in s.blocking_failures), "盖子被修复循环拦去封底"
    assert "实心底" not in "\n".join(s.repair_lines()), "盖子进了回喂日志（会被改成塞子）"
    assert "bottom:solid" not in s.scored_groups, "manual 那一档不该占一个打分位"

    # --- 5. 阴性对照：需求里没「底」字，这条必须完全不出场 ----------------
    for name, metrics in (("封底", SOLID), ("开口管", TUBE), ("倒杯", INVERTED)):
        assert row_of(metrics, REQ_NO_BOTTOM) is None, (
            f"套管需求里没点过「底」，却给 {name} 强加了实心底判定"
        )

    # --- 6. 大孔放宽：拿产品自己的实测剖面放宽，不拿固定 50% 卡天下 ------
    funnel = mk(footprint=50.0, bottom_coverage=0.2, top_coverage=0.2,
                hole_profile=[45.0, None, None, None, None, 45.0])
    assert row_of(funnel, REQ_BOTTOM_ONLY).status == reqtests.PASS, (
        "Ø45 通孔只留下 20% 足迹的件被判「底被挖穿」："
        "固定阈值会驱动修复去封一个用户明令要通的孔"
    )
    nohole = mk(footprint=50.0, bottom_coverage=0.2, top_coverage=0.2)
    assert row_of(nohole, REQ_BOTTOM_ONLY).status == reqtests.FAIL, (
        "同样 20% 却没量到任何孔却判过了：放宽只能放宽在**用户点名的缺口**上"
    )

    # --- 7. 量不到 = invalid：进 IR、但不驱动修复 -------------------------
    unknown = mk(bottom_coverage=None, top_coverage=None)
    r = row_of(unknown, REQ_BOTTOM_TOP)
    assert r.status == reqtests.INVALID, f"没量出来却被判成 {r.status}"
    s = suite_of(unknown, REQ_BOTTOM_TOP)
    assert all(t.group != "bottom:solid" for t in s.blocking_failures), "没测出来的项去驱动修复了"
    assert "实心底" not in "\n".join(s.repair_lines()), "没测出来的项进了回喂日志"
    assert s.summary()["invalid_ratio"] > 0, "invalid 没进 IR（等于把「没测出来」当通过）"
    assert "bottom:solid" in s.scored_groups, "invalid 不能不参与打分"

    # --- 8. 只有一把尺子：门槛必须从测量层拿，不能在判定层重写 ----------
    src_req = inspect.getsource(reqtests)
    assert _re.search(r"from \.mesh import[^\n]*_END_MIN_COVERAGE", src_req), (
        "reqtests 没从 mesh 拿占比门槛：同一个软件里就会有两把尺子量同一件事"
    )
    assert not _re.search(r"coverage[^\n]{0,20}>=\s*0?\.\d", src_req), (
        "reqtests 里写死了占比字面常量（应该用 mesh._END_MIN_COVERAGE）"
    )
    expect_threshold = round((1.0 - mesh._END_MIN_COVERAGE) * 100)
    assert f"≥{expect_threshold}%" in row_of(SOLID, REQ_BOTTOM_ONLY).expected, (
        f"清单里给用户看的门槛与 mesh 常数对不上：{row_of(SOLID, REQ_BOTTOM_ONLY).expected}"
    )

    # --- 9. 接线：需求原文必须带上「项目最初那句诉求」 -------------------
    project = SimpleNamespace(history=[
        SimpleNamespace(prompt=REQ_BOTTOM_TOP, instruction=None),
        SimpleNamespace(prompt=None, instruction="再打个通到底部的排水孔"),
    ])
    base = jobs_mod._project_baseline_requirement(project)
    assert base == REQ_BOTTOM_TOP, f"最初诉求取错了：{base!r}"
    # 第一条空原文要跳过（实测：真实 history.jsonl 里会有只写 instruction 的回退条目）
    assert jobs_mod._project_baseline_requirement(SimpleNamespace(history=[
        SimpleNamespace(prompt="   ", instruction=""),
        SimpleNamespace(prompt=None, instruction="回退到第 2 版"),
        SimpleNamespace(prompt="不该被取到", instruction=None),
    ])) == "回退到第 2 版"
    assert jobs_mod._project_baseline_requirement(SimpleNamespace(history=[])) == ""
    assert jobs_mod._project_baseline_requirement(SimpleNamespace()) == ""
    # 超长要截断（它会被发给 AI，不能把整个历史拼上去）
    long_base = jobs_mod._project_baseline_requirement(SimpleNamespace(history=[
        SimpleNamespace(prompt="长" * 2000, instruction=None),
    ]))
    assert len(long_base) <= jobs_mod._REQUIREMENT_MAX_CHARS, len(long_base)

    runner = jobs_mod.GenerationRunner.__new__(jobs_mod.GenerationRunner)
    runner._requirement = "再打个通到底部的排水孔"  # noqa: SLF001
    runner._requirement_base = base  # noqa: SLF001
    text = runner._requirement_text()
    # 这一对断言就是本轮修复的**存在理由**：本次指令本身开不了门控，
    # 拼上最初诉求才关得上——旧代码只看本次指令，所以那一步的实心底判定根本不生成。
    assert reqtests._BOTTOM_TOKENS.search(runner._requirement) is None, (  # noqa: SLF001
        "本次指令里居然有词表里的说法，这条阴性对照失去意义了（换个措辞）"
    )
    assert reqtests._BOTTOM_TOKENS.search(text), (  # noqa: SLF001
        f"拼上最初诉求后门控仍未命中：{text!r}"
    )
    assert "排水孔" in text, text
    # 去重两个方向（第一次生成时 base 就是 current；修改时 current 常被写成 base 的子串）
    runner._requirement = base  # noqa: SLF001
    assert runner._requirement_text() == base, "最初诉求与本次指令相同时被拼了两遍"
    runner._requirement_base = ""  # noqa: SLF001
    assert runner._requirement_text() == base, "没有 base 时把内容弄丢了"
    runner._requirement_base = base  # noqa: SLF001
    runner._requirement = "长" * 2000  # noqa: SLF001
    assert len(runner._requirement_text()) <= jobs_mod._REQUIREMENT_MAX_CHARS, "拼接后没截断"

    src_run = inspect.getsource(jobs_mod.GenerationRunner.run)
    assert "_requirement_base = _project_baseline_requirement(" in src_run, (
        "run() 里没接最初诉求（那上面那段门控断言就只是自吹自擂）"
    )
    src_init = inspect.getsource(jobs_mod.GenerationRunner.__init__)
    assert 'self._requirement_base: str = ""' in src_init, (
        "__init__ 没给空默认值（注释里写了有，而先于 run() 调到就会 AttributeError）"
    )
    src_suite = inspect.getsource(jobs_mod.GenerationRunner._requirement_suite)
    assert "self._requirement_text()" in src_suite, "需求核对表没用累计诉求"
    # 判据不许绕过 `_requirement_text()` 直接读本次指令（少一处就漏判一类）
    assert src_run.count("self._requirement ") <= 1, "还有地方在直接拼本次指令"

    return (
        "五档分得开（封底 PASS / 两端开 FAIL / 方向反 FAIL / 盖子 MANUAL+advisory / 量不到 INVALID）；"
        "只有 FAIL 驱动修复；无「底」字不出场；大孔按实测剖面放宽；"
        "门槛只从 mesh 拿；需求原文已含最初诉求"
    )





_DESIGN_RUN: "tuple[int, str] | None" = None


def _design_run() -> "tuple[int, str]":
    """跑一次 `selfcheck_design.py`，结果缓存（它约 3 秒，有两个地方要用）。

    返回 `(退出码, 全部输出)`。缓存而不是各跑一遍：项数守卫要的是**真跑出来的** `[OK]`
    条数，不是文档里抄的数 —— 而设计机制守卫本来就要跑它。
    """
    global _DESIGN_RUN
    if _DESIGN_RUN is None:
        import subprocess
        import sys as _sys

        script = ROOT / "scripts" / "selfcheck_design.py"
        assert script.is_file(), f"缺少专项自检脚本：{script}"
        proc = subprocess.run(
            [_sys.executable, "-u", str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        _DESIGN_RUN = (proc.returncode, (proc.stdout or "") + (proc.stderr or ""))
    return _DESIGN_RUN


def t_selfcheck_count_documented() -> str:
    """文档里的自检项数必须等于真源，`OFFLINE_SKIPS` 不许有死键（离线）。

    为什么会漂：项数被抄在 5 篇文档里（README-开发者的发布门槛清单与基线表、
    CONTRIBUTING、docs/DEVELOPMENT、docs/PUBLISH-TO-GITHUB、docs/HANDOVER），
    每加一项检查就要同步 5 处。实测同时存在 **89 / 74 / 50 / 85 / 131** 五种写法，
    **没有一个对**；而这些都是「发布前必跑」的门槛描述 —— 照着「50 项」核对的人会
    以为自己少跑了 41 项，而根本没人核对过「全绿」前面那个数。

    真源 = 本文件 `main()` 里 `check("名称", fn)` 的注册集合，用 **AST** 取。
    （拿正则扫源码会把注释与 docstring 里举的 `check(...)` 例子也算成一项 —— 同一个坑踩过。）

    设计机制那 142 项**不能**从静态调用点数出来（条件分支里两处 `check(...)` 只跑一处，
    还有循环），所以拿 `selfcheck_design.py` 真跑一遍的 `[OK]` 条数当尺子。
    """
    import ast

    src = Path(__file__).resolve().read_text(encoding="utf-8")
    names: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "check"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.append(node.args[0].value)
    total = len(names)
    dup = sorted({n for n in names if names.count(n) > 1})
    assert not dup, f"注册了重名的检查：{dup}（两条同名时根本看不出少跑了一条）"
    # 取数方式坏了不会报「0 项」，它会报一个看起来也很合理的旧数字 —— 所以先钉住下限
    assert total >= 80, f"AST 只数到 {total} 项注册，这条守卫自己先失效了（取数方式要查）"

    orphans = sorted(set(OFFLINE_SKIPS) - set(names))
    assert not orphans, (
        f"OFFLINE_SKIPS 里有死键：{orphans} —— 对应的检查早就不存在了。"
        "它在 --offline 下永远匹配不到东西，却会把「跳了几项」这个数骗过所有人"
    )
    offline_skip = len(OFFLINE_SKIPS)
    offline_pass = total - offline_skip

    design_rc, design_out = _design_run()
    assert design_rc == 0, "selfcheck_design.py 没跑过（具体原因看「设计规格与改动机制」那一项）"
    design_total = design_out.count("[OK]")
    assert design_total >= 100, f"只数到 {design_total} 项设计机制子检查，多半是输出格式改了"

    truth = {"total": total, "offline_pass": offline_pass, "offline_skip": offline_skip,
             "design": design_total, "fail": 0}
    # 历史实录：**具体哪几处**旧数字算历史，写成一份清单（各自说清是哪一轮），
    # 而不是靠「这行里有个‘当时’就整行放行」—— 后者会放过「当时的项数，现在完整是 85 项」
    # 这种把历史跟假现在时写在一句里的情况。不在清单里的旧数字一律算漂移：
    # 要么改对，要么把它记进清单并写清楚它是哪一轮的取证。
    historical_notes = (
        ("docs/HANDOVER.md", "**63 通过 / 0 失败**", "§12 交接后增量（2026-09-17）基线"),
        ("README-开发者.md", "**50/50 通过**", "§67 首次克隆冒烟那一次"),
        ("README-开发者.md", "**50/50**（新增「进度可信度」", "§69 那一轮"),
        ("README-开发者.md", "**50/50**（新增「开源文件完整性」", "§70 那一轮"),
        ("README-开发者.md", "`selfcheck.py --offline` 32 通过 / 13 跳过", "§70 那一轮"),
        ("README-开发者.md", "**50/50**（新增「固定零件库可编译」", "§71 那一轮"),
        ("README-开发者.md", "`selfcheck --offline` 36 通过 / 14 跳过", "§71 那一轮"),
        ("README-开发者.md", "**50/50**（当时的项数", "基线表里 1.0.0 的克隆冒烟"),
        ("CHANGELOG.md", "**79 通过 / 0 失败**", "1.0.1 发布记录"),
        ("CHANGELOG.md", "**40 通过 / 14 跳过 / 0 失败**", "1.0.0 发布记录"),
        ("CHANGELOG.md", "`--offline` 64 通过 / 15 跳过", "1.0.1 发布记录"),
    )

    # 只扫随包文档：内部记录（TAKEOVER/COMPETITIVE-GAPS）是**带日期的快照**，
    # 它们写的是「那一轮测到的数」，钉它们等于要求历史跟着代码改。
    internal = _internal_doc_paths()
    docs = [p for p in _doc_files() if p.relative_to(ROOT).as_posix() not in internal]
    assert len(docs) >= 10, f"只扫到 {len(docs)} 篇随包文档，这条检查等于没跑"

    # 文档里确实就是这么写这几类数的，按写法抽（顺序重要：先吃长写法，剩下的才是泛项数）。
    # 最后一列 `own`：这种写法要不要本行自己带主题词。**加粗的 N/M 必须自己带**——
    # 「**13/13**」「**6/6**」这种在文档里隔几行就是一个（打包冒烟、零件库、verify-flow），
    # 让缩进续行继承主题会把它们当成自检项数报红（实测就这么红了）。而
    # 「N 通过 / M 跳过 / K 失败」「N 项中跳 M 项」这种写法本身就只可能是项数，续行也能接。
    idioms = (
        (re.compile(r"(\d+)\s*项中跳\s*(\d+)\s*项"), ("total", "offline_skip"), False),
        (re.compile(r"(\d+)\s*通过\s*/\s*(\d+)\s*跳过\s*/\s*(\d+)\s*失败"),
         ("offline_pass", "offline_skip", "fail"), False),
        (re.compile(r"(\d+)\s*通过\s*\+\s*(\d+)\s*跳过"), ("offline_pass", "offline_skip"), False),
        (re.compile(r"(\d+)\s*通过\s*/\s*(\d+)\s*失败"), ("total", "fail"), False),
        (re.compile(r"\*\*(\d+)\s*/\s*(\d+)\*\*"), ("total", "total"), True),
        (re.compile(r"(\d+)\s*(?:项|个)\s*(?:子检查|细判据|确定性检查)"), ("design",), False),
    )
    # 泛项数只接「N 项」后面是分隔符的（「91 项」/「**91 项**」/「共 91 项）」），
    # 不接「182 个文本文件」「扫 49 个点」「其中 5 项是…」这类同形不同义的数 ——
    # 不收「个」是不想报「文档里提到多少个文件」为项数漂移（实测那样会一次报出十几条假阳）。
    generic = re.compile(r"(\d+)\s*项(?=$|[\s，。、；：）)【】（(*—/|])")

    findings: list[str] = []
    claim_files: set[str] = set()
    hist_used: set[int] = set()
    bound = 0
    for doc in docs:
        rel = doc.relative_to(ROOT).as_posix()
        prev_kind: str | None = None
        for ln, line in enumerate(doc.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            # 历史实录是一份**逐字清单**，认它不需要主题词推断，所以必须排在下面的
            # 门控之前。实测教训：「用它自己的 `scripts/selfcheck.py` 跑 —— **50/50 通过**」
            # 和「`selfcheck --offline` 36 通过 / 14 跳过」这两句既没缩进、本行又没主题词，
            # 放在门控之后就被 `continue` 掉了 —— 清单里那两条永远命不中，守卫反过来
            # 把它们报成「文档里已经找不到」，跟真正的死条目长得一模一样。
            is_hist = False
            for hi, (hf, needle, _w) in enumerate(historical_notes):
                if hf == rel and needle in line:
                    is_hist = True
                    hist_used.add(hi)
            design_subject = any(
                k in line for k in ("selfcheck_design", "设计规格与改动机制")
            )
            # 命令清单里经常是「上一行注释写项数、下一行才是脚本路径」，
            # 所以主题词不能只认文件名：「后端自检（91 项）」这种也得算数。
            self_subject = (not design_subject) and any(
                k in line for k in ("selfcheck.py", "后端自检", "完整自检")
            )
            kind = "design" if design_subject else ("total" if self_subject else None)
            if not line.strip():
                prev_kind = None  # 空行结束一段，续行不能隔着空行认爸爸
            elif kind is not None:
                prev_kind = kind
            elif not (prev_kind and line.startswith("  ")):
                continue
            # markdown 软换行接上一行的主题（实测漏过 HANDOVER.md 基线表：
            # 「**89 通过 / 0 失败**」就写在缩进的续行上，只认本行主题词永远抓不到它）。
            own_subject = design_subject or self_subject
            if kind is None:
                kind = prev_kind
            consumed: list[range] = []
            claims: list[tuple[int, str]] = []
            for rx, kinds, needs_own in idioms:
                if needs_own and not own_subject:
                    continue
                for m in rx.finditer(line):
                    claims.extend((int(g), k) for g, k in zip(m.groups(), kinds))
                    consumed.append(range(m.start(), m.end()))
            for m in generic.finditer(line):
                if any(m.start() in c for c in consumed):
                    continue
                claims.append((int(m.group(1)), kind))
            # 本行自己点名了哪个脚本（`selfcheck.py` / `selfcheck_design.py`），那它写的
            # 任何「N 项」就是在说那个脚本的项数 —— 后面跟什么词都得算。只认严格分隔符时
            # 实测漏过一句：`selfcheck_design.py`：**全部通过**（142 项设计机制判据）
            # —— 「项」后面是「设计机制判据」，既不接分隔符也不在写法表里，142 就这么滑过了。
            # 不带主题词的续行不走这一支，否则「182 个文本文件」「其中 5 项是」又会涌进来。
            if own_subject:
                for m in re.finditer(r"(\d+)\s*项(?![\d.])", line):
                    if any(m.start() in c for c in consumed):
                        continue
                    claims.append((int(m.group(1)), kind))
                    consumed.append(range(m.start(), m.end()))
            if not claims:
                continue
            claim_files.add(rel)
            for num, k in claims:
                if num == truth[k]:
                    bound += 1
                    continue
                if is_hist:
                    continue
                findings.append(
                    f"{rel}:{ln} 写着「{num}」（按 {k} 解释），真源是 {truth[k]} —— "
                    f"{line.strip()[:60]}"
                )
    # 同一个数字常被严格分隔符和 own_subject 两支各抓一次， findings 里就会连着
    # 出现完全相同的两行（实测：21 行里只有 12 处真漂移）。逐条改文档时这种噪声
    # 会把人带偏，所以先去重再报错。
    findings = list(dict.fromkeys(findings))
    assert not findings, (
        "文档里的自检项数和真源不一致：\n" + "\n".join(findings[:30])
        + (f"\n……共 {len(findings)} 处" if len(findings) > 30 else "")
    )
    # 历史清单自己也不能长死条目：躺着一条文档里已经找不到的“历史”，和
    # OFFLINE_SKIPS 里的死键是同一类病（它们都不会报错，只会把下一个读者带偏）。
    unused_hist = [
        f"{hf} 「{needle}」（{why}）"
        for i, (hf, needle, why) in enumerate(historical_notes) if i not in hist_used
    ]
    assert not unused_hist, (
        "历史实录清单里有文档中已经找不到（或被改写掉）的条目：\n  " + "\n  ".join(unused_hist)
        + "\n—— 要么把那句话改回去了，要么它本来就不存在：从清单里删掉，或者改成能命中的那句"
    )
    # 数到 0 处声明就是「什么都没查却绿了」；而发布门槛那几篇必须各自至少有一处
    # （历史实录按**清单条目数**计，同一句里两个旧数字只算一处 —— 它证明的是
    # 「这条记录还被文档带着」，不是「核了两个数」）。
    historical = len(hist_used)
    assert bound + historical >= 12, f"只核到 {bound} 处现行声明 + {historical} 条历史实录，扫描范围不对"
    must_state = {"README-开发者.md", "CONTRIBUTING.md", "docs/DEVELOPMENT.md",
                   "docs/PUBLISH-TO-GITHUB.md", "docs/HANDOVER.md"}
    missing = sorted(must_state - claim_files)
    assert not missing, (
        f"这些发布相关文档里一处项数都没写：{missing} —— 把数字删掉不算把数字改对，"
        "跑哪个脚本、该得几项得写在文档里"
    )
    return (
        f"真源 {total} 项 / --offline {offline_pass} 通过 + {offline_skip} 跳过 / 设计机制 {design_total} 项；"
        f"{len(docs)} 篇随包文档里 {bound} 处项数声明全部对上（另有 {historical} 条已登记的历史实录）；"
        "检查名无重名、OFFLINE_SKIPS 无死键"
    )


def t_bench_prompts_script() -> str:
    """质量基准脚本的结构守卫（离线，不调 AI）。

    为什么要给"基准脚本"本身做自检：基准一旦题目退化，报出来的 PR/RS 就成了噪声，
    而噪声基线比没有基线更危险（会让人以为回归通过了）。这里锁三件事：
      1. 题量与分档（20 题 / 4 档，对齐 CADTestBench 的公开口径）；
      2. 每题都必须带**可实测的点名尺寸**（没有数字就没法判 RS，等于白跑一次生成）；
      3. 判据只认实测：脚本必须从 requirement_tests 取数，不许读 AI 的自我陈述。
    """
    import importlib.util
    import os
    import re
    from pathlib import Path as _Path

    script = _Path(ROOT) / "scripts" / "bench_prompts.py"
    assert script.is_file(), "基准脚本丢了"
    spec = importlib.util.spec_from_file_location("aiforge_bench_probe", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # 脚本在 import 时就设 AIFORGE_DATA_DIR/PROJECTS_DIR（它靠这个隔离跑题数据）：
    # 自检进程里必须原样还回去，否则后面的用例会写到别人的目录里去。
    saved = {k: os.environ.get(k) for k in ("AIFORGE_DATA_DIR", "AIFORGE_PROJECTS_DIR")}
    try:
        spec.loader.exec_module(mod)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    prompts = mod.PROMPTS
    assert len(prompts) == 20, f"题量必须固定才能横向比较，实际 {len(prompts)}"
    tiers = {q["tier"] for q in prompts}
    assert tiers == {1, 2, 3, 4}, tiers
    for q in prompts:
        assert re.search(r"\d+(?:\.\d+)?\s*mm", q["prompt"]), f"{q['name']} 没有可实测的尺寸"
    # 一致性指标要真的比对重复次数
    src = script.read_text(encoding="utf-8")
    assert "requirement_tests" in src, "基准没有取实测核对表，分数就是假的"
    assert "pass_rate" in src and "invalid_ratio" in src, "缺 PR/IR 口径"
    assert "AIFORGE_BENCH_REPEAT" in src, "没有一致性子模式"
    return f"{len(prompts)} 题 / 4 档，全部带可实测尺寸；判据取自 requirement_tests"


def t_name_bridge() -> str:
    """规格名 ↔ 源码参数名的「改名桥」必须接上（离线、不调 AI）。

    实测到的翻车（用户报的正是这个）：一句话生成时 AI 把参数写成 `total_h`，
    提炼规格时又写成 `height`。用户说「把高度改成50mm」，AI 也**真的**把
    `total_h` 从 100 改成了 50 —— 但程序：
      * 找不到 `height` → 判「你要的改动没生效」；
      * 又按名字比对，把 `total_h` 的改动判成「未授权改动」；
      两项加起来代价 8.0，高于「完全没改」的 4.0，于是取舍逻辑选中了
      **原封不动的那一版**，界面还报成功。用户看到的就是「改了没反应」。

    这里把整条桥固化成断言：标签解析 → 名字解析 → 契约 → 判定 → 兜底，
    并带一个**阴性对照**（把名字换成毫无线索的 `body_extent`），
    确保绿灯是桥带来的，不是判据被放松换来的。
    """
    from aiforge import jobs as jobs_mod
    from aiforge import scad as scad_src
    from aiforge.changes import (
        build_change_contract,
        follow_values,
        resolve_param_name,
        verify_change,
    )
    from aiforge.design import DesignSpec

    source = (
        "// @name 笔筒\n"
        "// @param total_h 100 总高 60 200\n"
        "// @param outer_d 80 外径 60 120\n"
        "// @param rib_z0 12 加强筋起始高度 0 60\n"
        "// @param rib_z1 88 加强筋结束高度 20 100\n"
        "// @param rib_h 1.2 加强筋凸出高度 0.6 4\n"
        "total_h = 100;\n"
        "outer_d = 80;\n"
        "rib_z0 = 12;\n"
        "rib_z1 = 88;\n"
        "rib_h = 1.2;\n"
        "cylinder(h = total_h, d = outer_d);\n"
    )
    spec = DesignSpec.from_dict(
        {
            "kind": "笔筒",
            "specs": [
                {"name": "height", "label": "总高", "value": 100.0, "unit": "mm",
                 "user_specified": True},
                {"name": "outer_diameter", "label": "外径", "value": 80.0, "unit": "mm",
                 "user_specified": True},
            ],
        }
    )
    params = scad_src.extract_top_level_numbers(source)
    labels = scad_src.extract_param_labels(source)
    assert labels.get("total_h") == "总高", labels
    assert resolve_param_name("height", params, labels) == "total_h", "标签桥没接上"
    # 第二道网：没有注释标签时，结尾的单字母缩写也要能认出来
    assert resolve_param_name("height", {"total_h": 100.0, "wall_t": 2.0}) == "total_h"
    # 阴性对照：既无标签也无缩写线索时不许硬猜
    assert resolve_param_name("height", {"body_extent": 100.0}) is None

    contract = build_change_contract("把高度改成50mm。", spec, params, labels)
    assert "total_h" in contract.changed, contract.changed
    assert contract.target_values.get("total_h") == 50.0, contract.target_values
    assert "total_h" not in contract.unchanged, contract.unchanged
    # 同维度家族、沿轴的位置项必须进入跟随名单，并从「必须不变」里拿掉
    assert "rib_z1" in contract.follow, contract.follow
    assert not (set(contract.follow) & set(contract.unchanged)), contract.unchanged
    assert "outer_d" in contract.unchanged, contract.unchanged

    candidate = source.replace("total_h = 100;", "total_h = 50;")
    verdict = verify_change(source, candidate, contract, spec)
    assert verdict.ok, verdict.describe()
    assert not verdict.unauthorized, verdict.unauthorized
    assert verdict.penalty() == 0.0, verdict.penalty()

    # 阴性对照：把参数换成毫无线索的名字，同一形状的候选必须被判失败
    renamed = source.replace("total_h", "body_extent")
    narrow = build_change_contract(
        "把高度改成50mm。", spec, scad_src.extract_top_level_numbers(renamed)
    )
    narrow_verdict = verify_change(
        renamed, renamed.replace("body_extent = 100;", "body_extent = 50;"), narrow, spec
    )
    assert not narrow_verdict.ok and narrow_verdict.unauthorized, narrow_verdict.describe()

    # 跟随项按比例缩放；但「特征自身尺寸」（凸出高度）不许跟着缩 ——
    # 把 1.2mm 的筋缩成 0.6mm 是改坏模型，不是改小模型。
    followed = follow_values(contract, params, labels)
    assert followed.get("rib_z1") == 44.0 and followed.get("rib_z0") == 6.0, followed
    assert "rib_h" not in followed, followed

    surgical = jobs_mod._apply_targets(source, contract)  # noqa: SLF001
    assert surgical is not None
    rewritten = scad_src.extract_top_level_numbers(surgical)
    assert rewritten["total_h"] == 50.0 and rewritten["rib_z1"] == 44.0, rewritten
    assert rewritten["outer_d"] == 80.0 and rewritten["rib_h"] == 1.2, rewritten
    assert verify_change(source, surgical, contract, spec).ok

    # 底线：目标在源码里是表达式赋值时，绝不能改别的参数充数
    expr = "design_h = 100;\nheight = design_h * 1.5;\ncube([10, 10, height]);\n"
    expr_contract = build_change_contract(
        "把高度改成150mm。", spec, scad_src.extract_top_level_numbers(expr)
    )
    assert jobs_mod._apply_targets(expr, expr_contract) is None, (  # noqa: SLF001
        "height 是表达式赋值时不能改写 design_h 充数"
    )

    # 方位词必须**自成词元**才算方位（旧写法是子串，误伤过真参数名）。
    # 为什么这一条值得钉在改名桥里：`_modifier_conflict` 一报“方位相反”就把那个
    # 参数从候选里整个删掉，删错了就不是“保守”，而是**解析不到 → 确定性兜底失效
    # → 用户明明改对了却被判“没改到”**。实测拿全仓 760 个真实参数名/标签对账：
    #   * `backlash`（齿轮侧间隙，零件库里的真参数）旧判定读出 `back`；
    #   * `laptop_bottom_h` 里 `top` 只是 `laptop` 的中间一段，旧判定读出 top+bottom，
    #     于是它与「底部厚度」被判成方位相反（实测：旧=冲突、新=不冲突）。
    # 也不能反过来只按 `[_-\s]` 切词：零件库里有 `NemaBackAxleLength` / `CenterCandleWidth`
    # 这种驼峰名，切不开就把**真**方位词一起丢了（实测这两个名字新旧判定一致，
    # 靠的就是驼峰边界而不是子串）。
    from aiforge.changes import _direction_words  # noqa: SLF001

    assert _direction_words("laptop_bottom_h") == {"bottom"}, _direction_words("laptop_bottom_h")
    assert _direction_words("backlash") == set(), _direction_words("backlash")
    assert _direction_words("NemaBackAxleLength") == {"back"}, "驼峰里的真方位词丢了"
    assert _direction_words("CenterCandleWidth") == {"center"}, "驼峰里的真方位词丢了"
    # 中文没有词边界，单字方位词仍按子串认（这条不能误伤到英文那一套里）
    assert _direction_words("把这个孔的内径改成8mm") == {"inner"}, "中文方位词丢了"
    # …并且真的能接到参数上（实测：旧判定下唯一候选被删 → 返回 None；新判定接得上）
    assert resolve_param_name("底部高度", {"laptop_height": 80.0}) == "laptop_height", (
        "laptop 里的 top 被当成方位词，把用户说的那个参数从候选里删了"
    )
    return (
        "改名桥：标签与缩写都能把 height 接到 total_h，跟随项按轴缩放、特征尺寸不动；"
        "方位词只认自成词元的（backlash/laptop 不误伤，驼峰 Back/Center 不丢）"
    )


def t_contract_intent() -> str:
    """改动契约的**语义**正确性：用户说什么，就只能授权改什么（离线）。

    这一组全部来自真机实测翻车（跑真实端到端矩阵时抓到的）：
      * 「在顶部增加一个直径10mm的圆孔」→ 契约里出现
        `target_values={'outer_d': 90}`：裸别名「直径」命中了规格里的**外径**，
        「增加」又被当成相对改动 → 程序**主动下令**把外径从 80 改成 90。
        用户只是要加个孔，外径却变了 10mm。
      * 「把这个孔的内径改成8mm」→ 解析到 `outer_d`（外径）：`inner_diameter`
        与 `外径` 是相反的两个尺寸，但一个写英文一个写中文，逐字比对看不出来。
      * 「高度不变，把外径改成70mm」→ `total_h` 仍被写进 changed：
        冻结的是规格名 `total_height`，而授权名单里补的是源码名 `total_h`。
      * 「把高度增加20mm」→ 目标值成了 20（而不是 70）：`total_height` 这种
        复合规格名解析不到源码里的 `total_h`。
    这些错误都比"AI 改错"更严重 —— 契约错了，AI 改得再听话也是错的。
    """
    from aiforge import scad as scad_src
    from aiforge.changes import build_change_contract
    from aiforge.design import DesignSpec

    source = (
        "// @param outer_d 80 外径 40 200\n"
        "// @param total_h 100 总高 30 300\n"
        "// @param wall_t 2.5 筒壁厚度 1 10\n"
        "// @param bottom_t 6 底部实心厚度 1 20\n"
        "// @param rib_z1 88 加强筋终止高度 10 300\n"
        "outer_d = 80;\n"
        "total_h = 100;\n"
        "wall_t = 2.5;\n"
        "bottom_t = 6;\n"
        "rib_z1 = 88;\n"
        "cylinder(h = total_h, d = outer_d);\n"
    )
    spec = DesignSpec.from_dict(
        {
            "kind": "圆形笔筒",
            "specs": [
                {"name": "outer_diameter", "label": "外径", "value": 80.0, "unit": "mm",
                 "user_specified": True},
                {"name": "total_height", "label": "总高", "value": 100.0, "unit": "mm",
                 "user_specified": True},
                {"name": "wall_thickness", "label": "壁厚", "value": 2.5, "unit": "mm",
                 "user_specified": True},
                # 底部厚度这类项常常是后加的（用户改动时才被提炼出来），
                # 句子里没有"厚度"两个字、只有动词「加厚」时也要能落到它上面
                {"name": "bottom_thickness", "label": "底部实心厚度", "value": None,
                 "unit": "mm", "user_specified": False},
                {"name": "inner_diameter", "label": "内径", "value": None, "unit": "mm",
                 "user_specified": False},
            ],
        }
    )
    params = scad_src.extract_top_level_numbers(source)
    labels = scad_src.extract_param_labels(source)

    def contract(text: str):
        return build_change_contract(text, spec, params, labels)

    def touches_outer(built) -> bool:
        return any(
            "outer" in name for name in (*built.changed, *built.target_values, *built.follow)
        )

    # ① 加孔：只能当成结构改动，绝不能授权改外径
    hole = contract("在顶部正中间增加一个直径10mm的圆孔，要完全贯通到底部。")
    assert not hole.target_values, hole.target_values
    assert not touches_outer(hole), (hole.changed, hole.target_values, hole.follow)
    # ② 内径：不许落到外径上
    inner = contract("把这个孔的内径改成8mm。")
    assert not touches_outer(inner), (inner.changed, inner.target_values, inner.follow)
    # ③ 明确冻结的项，解析出来的源码参数名也要一起冻结
    frozen = contract("高度不变，把外径改成70mm。")
    assert frozen.target_values.get("outer_d") == 70.0, frozen.target_values
    assert "total_h" not in frozen.changed, frozen.changed
    assert "total_h" in frozen.frozen, frozen.frozen
    # ④ 相对改动：以源码里的当前值为基准（100 + 20 = 120）
    grow = contract("把高度增加20mm。")
    assert grow.target_values.get("total_h") == 120.0, grow.target_values
    # ⑤ 整体缩放：比例被识别、且不存在"必须逐字不变"的项
    scale = contract("整体缩小到原来的80%。")
    assert abs(float(scale.scale_factor or 0) - 0.8) < 1e-9, scale.scale_factor
    assert scale.unchanged == [] and scale.follow == [], (scale.unchanged, scale.follow)
    # 缩放的授权名单必须落到**源码参数名**上：只写规格名时，参数核对会把
    # 缩放后的 outer_d / total_h 全判成"越权改动"，缩放永远不达标 → 被回退成
    # "什么都没改"（实测：整体缩小到 80% 时模型一毫米没动，作业却报成功）。
    for name in ("outer_d", "total_h", "wall_t", "bottom_t"):
        assert name in scale.changed, (name, scale.changed)
    # ⑥ 正对照：直接说外径，必须真的授权外径（别为了防误报把正事也挡了）
    outer = contract("把外径改成90mm。")
    assert outer.target_values.get("outer_d") == 90.0, outer.target_values
    # ⑦ 壁厚：复合规格名要能落到 wall_t
    wall = contract("把壁厚改成3mm。")
    assert wall.target_values.get("wall_t") == 3.0, wall.target_values
    # ⑧ 「底座加厚到6mm」：句子里没有"厚度"两个字，只有动词"加厚"。
    #    以前契约完全空转（changed/target_values 都空）→ AI 什么都不改 → 报成功。
    base = contract("把底座加厚到6mm。")
    assert base.target_values.get("bottom_t") == 6.0, base.target_values
    assert "wall_t" not in base.target_values, base.target_values

    # ⑨ 多个子句各自一个数字时，每个数字必须落在**自己那一句**的参数上。
    #   钉住的是「按句长推算子句区间」那套记账：分隔符不都是 1 个字符
    #   （然后/并且/同时/另外 是 2 个），而且「，然后」会被当成**两个**分隔符、
    #   中间空片段又被丢弃 —— 推算出的区间比真实区间短，句尾的个位数就滑到下一句。
    #   旧实现下这一句实测会算成 `outer_d=8、total_h=3、壁厚没目标值`：
    #   程序**主动下令**把总高改成 3mm（用户说的是 50）。这不是保守，是错。
    many = contract("把外径改成8，然后把壁厚改成3，同时把总高改成50")
    assert many.target_values == {"outer_d": 8.0, "wall_t": 3.0, "total_h": 50.0}, (
        many.target_values
    )
    return "契约语义正确：加孔不授权外径、内径不落到外径、冻结含源码名、缩放授权到参数名、加厚认得厚度、多子句数字各归各句"


def t_user_scale() -> str:
    """用户要求的整体缩放必须**真的缩小**，且不改变结构（离线，不调 AI）。

    实测两个坑：
      * 缩放没有"目标参数值"，所以「把目标值写回源码」那条确定性兜底对它是空转，
        AI 两次没做到时整条链只能保留原版 —— 用户看到"缩小到80%"却一毫米没变，
        作业还报成功；
      * 无差别按倍率改写会把 `rib_count` 从 16 缩成 12.8（渲染成 13 条筋），
        那是**改了结构**，不是等比缩小。
    """
    from aiforge import jobs as jobs_mod
    from aiforge import scad as scad_src
    from aiforge.changes import build_change_contract, verify_change
    from aiforge.design import DesignSpec

    source = (
        "// @param outer_d 80 外径 40 200\n"
        "// @param total_h 100 总高 30 300\n"
        "// @param wall_t 2.5 筒壁厚度 1 10\n"
        "// @param rib_count 16 竖向加强筋数量 0 36\n"
        "// @param rib_z1 88 加强筋终止高度 10 300\n"
        "outer_d = 80;\n"
        "total_h = 100;\n"
        "wall_t = 2.5;\n"
        "rib_count = 16;\n"
        "rib_z1 = 88;\n"
        "cylinder(h = total_h, d = outer_d);\n"
    )
    spec = DesignSpec.from_dict(
        {
            "kind": "笔筒",
            "specs": [
                {"name": "outer_diameter", "label": "外径", "value": 80.0, "unit": "mm",
                 "user_specified": True},
                {"name": "height", "label": "总高", "value": 100.0, "unit": "mm",
                 "user_specified": True},
            ],
        }
    )
    params = scad_src.extract_top_level_numbers(source)
    labels = scad_src.extract_param_labels(source)
    contract = build_change_contract("整体缩小到原来的80%。", spec, params, labels)
    assert abs(float(contract.scale_factor or 0) - 0.8) < 1e-9, contract.scale_factor
    assert not contract.unchanged, contract.unchanged

    scaled = jobs_mod.build_user_scaled_source(source, 0.8, "测试机")  # noqa: SLF001
    assert scaled and scaled != source, "必须能构造出缩放版"
    after = scad_src.extract_top_level_numbers(scaled)
    assert abs(after["outer_d"] - 64.0) < 1e-6, after
    assert abs(after["total_h"] - 80.0) < 1e-6, after
    assert abs(after["rib_z1"] - 70.4) < 1e-6, after
    assert abs(after["rib_count"] - 16.0) < 1e-6, f"筋数量不该跟着缩：{after}"
    verdict = verify_change(source, scaled, contract, spec)
    assert verdict.ok, verdict.describe()
    assert not verdict.unauthorized, verdict.unauthorized
    return "整体缩放：契约授权到参数名、确定性缩放真的缩小、数量类参数不变、核对达标"


def t_compile_advice() -> str:
    """编译失败要给出**针对该错误的可执行改法**（离线，不调 AI）。

    实测：模型生成时 OpenSCAD 报 `CGAL error in CGALUtils::applyUnion3D:
    assertion violation`，程序把这段原文直接丢给 AI 去修 —— 连修两轮还是同一个错，
    用户看到的是「自动修复后仍然失败」。原文对 AI 基本是噪音，
    必须翻成「共面重合 / 零厚度 / 非流形」这类具体成因与改法。
    """
    from aiforge import compiler
    from aiforge import jobs as jobs_mod
    import inspect

    cgal = (
        "CGAL error in CGALUtils::applyUnion3D: CGAL ERROR: assertion violation!\n"
        "Expr: e_bp == CGAL::EQUAL"
    )
    advice = compiler.explain_failure(cgal)
    assert advice and "0.01mm" in advice and "共面" in advice, advice
    manifold = compiler.explain_failure("Object may not be a valid 2-manifold and may need repair!")
    assert manifold and "2-流形" in manifold, manifold
    flat = compiler.explain_failure("Current top level object is not a 3D object.")
    assert flat and "extrude" in flat, flat
    # 不认识的报错原样交给 AI，不要编造改法
    assert compiler.explain_failure("something totally unknown") is None
    assert compiler.explain_failure("") is None
    # P0 扩充的 CGAL 变体：不封闭 / 自相交 / 空几何 / 内存 / NaN
    closed = compiler.explain_failure("WARNING: The given mesh is not closed!")
    assert closed and "零件库" in closed, closed
    selfint = compiler.explain_failure("CGAL: self-intersection detected")
    assert selfint and "自相交" in selfint, selfint
    empty = compiler.explain_failure("Current top level object is empty")
    assert empty and "module" in empty, empty
    mem = compiler.explain_failure("terminate called after throwing 'std::bad_alloc'")
    assert mem and "$fn" in mem, mem
    nan = compiler.explain_failure("ERROR: coordinate is nan")
    assert nan and "NaN" in nan, nan
    # 三个字母的特征片段不能做子串匹配：实测 `nano_gap` / `nanoseconds` 都会被
    # 旧的 `"nan" in lowered` 当成「坐标出现 NaN」，自动修复循环因此拿到错的病因。
    for text in (
        "ERROR: Unknown variable 'nano_gap' in file model.scad, line 12.",
        "WARNING: Ignoring unknown variable nanoseconds.",
    ):
        got = compiler.explain_failure(text)
        assert not (got and "NaN" in got), f"无关文本被判成了 NaN 报错：{text} → {got}"
        assert got and "未定义" in got, f"未定义变量该拿到逐字校对名字的改法：{text} → {got}"
    # 上面两条还不能证明「整词匹配」真的生效（它们先被 unknown variable 那条接住了）。
    # 这两条才是：文本里带 nano/nancy 但不属于任何已知失败 —— 按子串匹配会被
    # `"nan"` 接走并编出一份 NaN 改法，按整词匹配则必须老实返回 None（不编造）。
    for text in (
        "WARNING: Variable nanoseconds is deprecated.",
        "ERROR: Could not open input file C:\\Users\\nancy\\model.scad",
    ):
        got = compiler.explain_failure(text)
        assert got is None, f"无关文本被 `nan` 子串匹配拉走了：{text} → {(got or '')[:40]}"
    # 修复证据的拼装方式：改法在前、原文在后
    source = inspect.getsource(jobs_mod.GenerationRunner._ensure_compilable)  # noqa: SLF001
    assert "compile_result.advice" in source, "编译修复没有用上可执行改法"
    # 换路径重生成（P0）：修复两轮仍失败时带硬约束重新生成一次
    assert "_REGEN_EXTRA" in source, "编译修复两轮失败后没有换路径重生成"
    return "编译报错翻译成可执行改法（含不封闭/自相交/空/内存/NaN），未知不编造，两轮失败换路径"


def t_console_streams() -> str:
    """`--noconsole` 打包后，诊断入口在无控制台句柄时也必须能跑完（离线）。

    实测：打包版用快捷方式 / `Start-Process` 启动 `--check` 时
    `sys.stdout` 是 `None`，`print()` 抛 AttributeError，进程退出码 1、
    且什么都不显示 —— 用户看到的是「诊断跑完什么都没有，还失败了」。
    """
    import importlib.util

    import main as main_mod  # type: ignore[import-not-found]

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("aiforge_main_probe", root / "main.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "_ensure_console_streams"), "main.py 缺少无控制台兜底"

    saved = (sys.stdout, sys.stderr)
    try:
        sys.stdout = None  # type: ignore[assignment]
        sys.stderr = None  # type: ignore[assignment]
        mod._ensure_console_streams()
        assert sys.stdout is not None and sys.stderr is not None, "兜底之后仍有 None 流"
        sys.stdout.write("")  # 必须真的可写，不能只是非 None
    finally:
        sys.stdout, sys.stderr = saved

    # `--check` 必须同时留一份报告文件，避免"没控制台就什么都看不到"
    import inspect

    check_source = inspect.getsource(mod._run_check)
    assert "check-report.txt" in check_source, "--check 没有落地报告文件"
    assert inspect.getsource(mod.main).index("_ensure_console_streams") < 200, (
        "main() 必须在最前面就装好标准流兜底"
    )
    del main_mod
    return "无控制台句柄时标准流有兜底（不再崩、退出码正确），--check 另存报告文件"


def t_console_encoding() -> str:
    """GBK 控制台下，打印中文/符号不能把脚本自己弄崩。

    这条是实测出来的真问题（不是假想）：中文 Windows 默认代码页 936，而 Python
    到 3.13 为止不会默认启用 UTF-8 模式。`scripts/selfcheck_design.py` 里有一行
    明细带「−」（U+2212），在 GBK 控制台里跑就把整个自检带崩：不只这一项没结果，
    后面的项、汇总、退出码全部丢失 —— 发布门禁会“看起来跑过了”。
    这里分别验证：真子进程能跑通（含阴性对照）、降级路径不抛异常、
    入口脚本都接上了、以及实现只有一份。
    """
    import ast
    import io
    import subprocess

    root = Path(__file__).resolve().parents[1]
    scripts_dir = root / "scripts"
    from aiforge import stdio as stdio_mod

    tricky = "− 减号 （≥4.5:1）"  # 数学减号等不是 GBK 可表示字符，混在中文里
    env = {**os.environ, "PYTHONIOENCODING": "gbk", "PYTHONUTF8": "0", "PYTHONPATH": str(scripts_dir)}
    env.pop("PYTHONHOME", None)

    def run(code: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            env=env,
            cwd=str(root),
            timeout=120,
        )

    # 1) 接上兜底：子进程里 stdout 必须是 UTF-8，这些字符能完整写出来
    fixed = run(f"import console_utf8; print({tricky!r})")
    assert fixed.returncode == 0, (
        f"接上 console_utf8 仍崩：rc={fixed.returncode} "
        f"stderr={fixed.stderr.decode('utf-8', 'replace')[-300:]}"
    )
    out = fixed.stdout.decode("utf-8", "replace")
    assert tricky in out, f"兜底后输出不对（实际 {out!r}）"

    # 2) 阴性对照：同样的字符不兜底时必须崩，否则这条用例在验一个不存在的问题
    broken = run(f"print({tricky!r})")
    assert broken.returncode != 0, "GBK 下打印这些字符居然没崩 —— 用例前提变了，重新看这里"
    assert b"UnicodeEncodeError" in broken.stderr, broken.stderr[-300:]

    # 3) 降级路径：写不进去也不能抛异常，而是退化成 \uXXXX（信息不丢）
    buf = io.BytesIO()
    strict_ascii = io.TextIOWrapper(buf, encoding="ascii", errors="strict")
    assert stdio_mod.safe_print(tricky, file=strict_ascii) is True
    assert b"\\u2212" in buf.getvalue(), buf.getvalue()

    # 4) 入口脚本都接上了，而且比第一个 print 先执行（用 AST 看，不会被注释/文档骗到）
    missing, late, guarded = [], [], 0
    for path in sorted(scripts_dir.glob("*.py")):
        if path.name == "console_utf8.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        printed = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ]
        if not printed:
            continue
        wired = next(
            (
                node.lineno
                for node in tree.body
                if isinstance(node, ast.Import)
                and any(alias.name == "console_utf8" for alias in node.names)
            ),
            None,
        )
        if wired is None:
            missing.append(path.name)
        elif wired > min(printed):
            late.append(path.name)
        else:
            guarded += 1
    assert not missing, f"这些脚本会打印中文却没接兜底：{missing}"
    assert not late, f"这些脚本的 console_utf8 导入太晚（必须比第一个 print 先）：{late}"
    assert guarded >= 15, f"只量到 {guarded} 个脚本接了兜底，与预期不符（是不是 glob 不对？）"

    # 5) 实现只有一份：main.py 不能又写一份自己的（两处漂移就是这次的成因）
    main_src = (root / "main.py").read_text(encoding="utf-8")
    assert "from aiforge import stdio" in main_src, "main.py 不再共用 aiforge.stdio"
    assert "TextIOWrapper" not in main_src, "main.py 里又写了一份自己的标准流实现"
    return (
        f"GBK 子进程已修好（含阴性对照）、写不进时降级为 \\uXXXX、"
        f"{guarded} 个入口脚本已接入且导入时机正确"
    )


def t_dpi_workarea() -> str:
    """高 DPI 适配：窗口尺寸夹紧必须拿到**真实工作区**，而不是任意一个系统度量。

    这条来自一个用户天天碰得到、而自检一项都没覆盖的缺陷（`dpi.py`）：它把
    `GetSystemMetrics(48) / (49)` 当成 `SM_CXWORKAREA / SM_CYWORKAREA` 使用，
    而 **winuser.h 里没有这两个索引** —— 48 是 SM_CYMINSPACING（小图标纵向间距）、
    49 是 SM_CXMINTRACK（最小窗口跟踪宽度），本机实测读到 34 与 16。
    于是 `work_dip=(27,16)`，`window_geometry` 把 min 抬到 floor、又把 preferred
    夹到 `work-8`，**任何机器上恒返回 (720,520) 且 min_size == size**：用户永远
    拿不到设计尺寸 1360×880，窗口还被钉死不能放大；同时「高缩放小屏上窗口比
    屏幕还大」这个它真正要保护的场景一次也没被保护过。又因为读数不是 0，
    「拿不到就退回屏幕尺寸」的兜底也一起失效。

    为什么以前没被抓到：全仓没有一处对 `window_geometry` / `ensure_dpi_awareness`
    做断言，`verify-hdpi` 跑在浏览器 CDP 里量不到原生窗口。**基线全绿不等于没问题。**

    同一个模块里还钉了第二条（下面第 4 段）：`shcore.SetProcessDpiAwareness` 的
    返回值是 HRESULT，**失败时不抛异常**（本机实测：已经有感知时返回 0x80070005）。
    旧写法把「没抛异常」当「设成功了」，于是诊断页与日志一起谎报 `mode=per_monitor`，
    还顺手跳过下面那条 `SetProcessDPIAware()` 兜底 —— 用户报「界面发虚」时，
    拿到的第一条排查线索本身就是假的。

    尺子本身也是实测出来的（不能再省一步）：`shcore.GetProcessDpiAwareness(NULL)` 读的是
    Win8.1 那套进程 slot，用 `SetProcessDpiAwarenessContext` 设的感知它看不见（本机实测
    钉成 system 后它返回 0=unaware，而 GetThreadDpiAwarenessContext 返回 -1）。
    拿它当「真值」会把正确的产品代码判红 —— 第一版守卫就是这么红的，错在尺子不在代码。
    """
    import ctypes
    import ctypes.wintypes
    import subprocess as sp

    from aiforge import dpi as dpi_mod

    # 1) 纯几何：夹紧逻辑本身（不依赖本机屏幕，三档都要对）
    big = dpi_mod.DpiState(aware=True, mode="per_monitor_v2", dpi=120, scale=1.25,
                           screen_px=(2880, 1620), work_px=(2880, 1620))
    size, mins = dpi_mod.window_geometry((1360, 880), (1080, 700), state=big)
    assert size == (1360, 880) and mins == (1080, 700), (
        f"宽裕的工作区不该夹紧，却算成 {size}/{mins}"
    )

    small = dpi_mod.DpiState(aware=True, mode="per_monitor_v2", dpi=192, scale=2.0,
                             screen_px=(1920, 1080), work_px=(1920, 1000))
    size, mins = dpi_mod.window_geometry((1360, 880), (1080, 700), state=small)
    assert small.work_dip == (960, 500), small.work_dip
    assert size[0] <= small.work_dip[0] and mins[0] < 1080, (
        f"200%/1080p 上窗口或最小尺寸仍比屏幕宽：size={size} min={mins}"
    )

    blind = dpi_mod.DpiState()  # 什么屏幕信息都没拿到
    size, mins = dpi_mod.window_geometry((1360, 880), (1080, 700), state=blind)
    assert size == (1360, 880) and mins == (1080, 700), (
        f"拿不到屏幕信息时必须保持原样（给定的默认值不能用 0 尺寸替它），却算成 {size}/{mins}"
    )

    if os.name != "nt":
        return "窗口夹紧三档正确（宽屏原样 / 200% 小屏夹紧 / 无信息不返回 0）；非 Windows 不取屏幕度量"

    # 2) 本机真值对账：产品读的工作区必须 == 系统真实工作区
    state = dpi_mod.ensure_dpi_awareness()
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    rect = ctypes.wintypes.RECT()
    if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
        return "夹紧三档正确；本会话取不到工作区（无交互桌面），真值对账未跑"
    truth = (int(rect.right - rect.left), int(rect.bottom - rect.top))
    assert tuple(state.work_px) == truth, (
        f"dpi 读到的工作区 {tuple(state.work_px)} 不是系统真值 {truth} —— "
        "八成又拿某个 GetSystemMetrics 索引当真值用了"
    )
    assert min(truth) > 200, f"工作区读数 {truth} 小得不像屏幕"
    assert (
        state.work_px[0] <= state.screen_px[0] and state.work_px[1] <= state.screen_px[1]
    ), (f"工作区比整屏还大：work={state.work_px} screen={state.screen_px}")

    # 3) 负向锁定：旧写法读的是索引 48/49，直接拿那两个字面值对一遍
    wrong = (int(user32.GetSystemMetrics(48)), int(user32.GetSystemMetrics(49)))
    assert tuple(state.work_px) != wrong, f"工作区又读成了 GetSystemMetrics(48)/(49)={wrong}"

    size, _mins = dpi_mod.window_geometry((1360, 880), (1080, 700), state=state)
    assert size == (1360, 880), (
        f"这台机器上窗口被夹成 {size}（旧缺陷下恒为 (720,520) 且 min_size == size）"
    )

    # 4) 感知模式必须报真话。DPI 感知是**进程级且不可逆**的，所以必须在子进程里做：
    #    先把子进程钉成 system aware（模拟「清单/引导程序已经设过」的机器），
    #    这时 dpi.py 的三档 Context 与 shcore 一律失败 —— 旧写法恰恰在这里谎报成功。
    #    真值读法用 GetThreadDpiAwarenessContext：预设走的是 Set…Context，
    #    两者方向不同（一写一读），而旧代码根本不做任何读回（拿返回值造个模式）。
    child_src = (
        "import ctypes, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "u = ctypes.windll.user32\n"
        "u.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]\n"
        "u.SetProcessDpiAwarenessContext.restype = ctypes.c_bool\n"
        "preset = bool(u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-1)))\n"
        "truth = 'unknown'\n"
        "try:\n"
        "    u.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p\n"
        "    h = u.GetThreadDpiAwarenessContext()\n"
        "    eq = u.AreDpiAwarenessContextsEqual\n"
        "    eq.argtypes = [ctypes.c_void_p, ctypes.c_void_p]\n"
        "    eq.restype = ctypes.c_bool\n"
        "    for val, label in ((-4, 'per_monitor_v2'), (-3, 'per_monitor'), (-1, 'system')):\n"
        "        if h and eq(ctypes.c_void_p(h), ctypes.c_void_p(val)):\n"
        "            truth = label\n"
        "            break\n"
        "except Exception:\n"
        "    pass\n"
        "from aiforge import dpi\n"
        "st = dpi.ensure_dpi_awareness()\n"
        "print('PRESET=%s TRUTH=%s REPORTED=%s AWARE=%s DPI=%d'\n"
        "      % (preset, truth, st.mode, st.aware, st.dpi))\n"
    )
    sandbox = ROOT / "build" / "work" / f"dpi-mode-{os.getpid()}"
    sandbox.mkdir(parents=True, exist_ok=True)
    child_script = sandbox / "dpi_mode_child.py"
    child_script.write_text(child_src, encoding="utf-8", newline="\n")
    proc = sp.run(
        [sys.executable, "-X", "utf8", str(child_script), str(SRC)],
        capture_output=True,
        cwd=str(ROOT),
        timeout=180,
    )
    tail = [ln for ln in (proc.stdout or b"").decode("utf-8", "replace").splitlines() if "REPORTED=" in ln]
    fields = dict(tok.split("=", 1) for tok in (tail[-1].split() if tail else []))
    mode_note = "感知模式对账未跑（子进程读不到真值）"
    if proc.returncode != 0 and not fields:
        raise AssertionError(
            f"子进程跑挂了（rc={proc.returncode}）："
            f"{(proc.stderr or b'').decode('utf-8', 'replace')[-300:]}"
        )
    if fields.get("PRESET") == "True" and fields.get("TRUTH") not in (None, "unknown"):
        assert fields["REPORTED"] == fields["TRUTH"], (
            f"子进程的感知真值是 {fields['TRUTH']}，dpi 却报 {fields['REPORTED']} —— "
            "setter 的返回值是 HRESULT，非 0（失败）也不抛异常：拿「没抛」当「成功」"
            "既谎报模式，又跳过了 SetProcessDPIAware 兜底"
        )
        assert fields["AWARE"] == "True", (
            f"真值是 {fields['TRUTH']}（已感知），dpi 报 AWARE={fields['AWARE']} —— "
            "把已经感知的进程写成 unaware 是反方向谎报，用户会照着「未适配」白折腾"
        )
        mode_note = (
            f"感知已由别处设定时报真值 {fields['REPORTED']}（不是造出来的 per_monitor）"
        )

    return (
        f"工作区走 SPI_GETWORKAREA（实测 {truth[0]}×{truth[1]} 与系统一致）；"
        f"窗口回到设计尺寸，200% 小屏夹紧真能生效；{mode_note}"
    )


def t_restore_revision() -> str:
    """回退历史版本：**归档编号必须对得上**，恢复出来的必须是那一版。

    这条来自实测发现的功能缺口 + 一个真 Bug：
      * 用户说明书写着「随时可以回到之前的版本」，但后端**根本没有恢复接口** ——
        归档了却回不去；
      * 更糟的是归档本身错位：编译会把新的 model.scad / model.stl 直接写进项目目录，
        而归档发生在收尾阶段，于是 revisions/rNNN 里存的是**第 N+1 版**的内容。
        两个问题叠在一起，用户点"回退"只会拿到一个错误的模型。

    用**手写的最小 STL**（不依赖 OpenSCAD）造出两版真实文件，然后走真实接口验证。
    """
    import json
    import tempfile

    from fastapi.testclient import TestClient

    from aiforge import projects as projects_mod
    from aiforge.app import create_app

    def cube_stl(size: float) -> bytes:
        """一个 ASCII STL 立方体（尺寸可控，便于按体积区分版本）。"""
        s = size
        triangles = [
            ((0, 0, 0), (s, 0, 0), (s, s, 0)), ((0, 0, 0), (s, s, 0), (0, s, 0)),
            ((0, 0, s), (s, s, s), (s, 0, s)), ((0, 0, s), (0, s, s), (s, s, s)),
            ((0, 0, 0), (0, 0, s), (s, 0, s)), ((0, 0, 0), (s, 0, s), (s, 0, 0)),
            ((0, s, 0), (s, s, 0), (s, s, s)), ((0, s, 0), (s, s, s), (0, s, s)),
            ((0, 0, 0), (0, s, 0), (0, s, s)), ((0, 0, 0), (0, s, s), (0, 0, s)),
            ((s, 0, 0), (s, s, s), (s, s, 0)), ((s, 0, 0), (s, 0, s), (s, s, s)),
        ]
        lines = ["solid v"]
        for a, b, c in triangles:
            lines.append("facet normal 0 0 0")
            lines.append("  outer loop")
            for vertex in (a, b, c):
                lines.append(f"    vertex {vertex[0]} {vertex[1]} {vertex[2]}")
            lines.append("  endloop")
            lines.append("endfacet")
        lines.append("endsolid v")
        return ("\n".join(lines) + "\n").encode("utf-8")

    with tempfile.TemporaryDirectory(prefix="restore_") as tmp:
        project_dir = Path(tmp) / "AI-3D-Forge" / "Projects"
        os.environ["AIFORGE_PROJECTS_DIR"] = str(project_dir)
        projects_mod.reset_for_tests()
        store = projects_mod.store()
        project = store.create("回退测试")
        # 第 1 版：10mm 立方体
        project.scad_path.write_text("// @name 回退测试\ncube([10, 10, 10]);\n", encoding="utf-8")
        project.stl_path.write_bytes(cube_stl(10.0))
        project.revision = 1
        project.metrics = {"bbox": {"x": 10.0, "y": 10.0, "z": 10.0}}
        store.save(project)
        store.append_history(
            project,
            projects_mod.HistoryEntry(
                revision=1, created_at=projects_mod.now_iso(), prompt="做一个 10mm 立方体",
                summary="10mm 立方体", name=project.name, stl=True, metrics=project.metrics,
            ),
        )
        # 第 2 版：20mm 立方体（模拟用户改过尺寸）
        store.archive_revision(project, 1, keep_stl=True)          # 先归档第 1 版
        project.scad_path.write_text("// @name 回退测试\ncube([20, 20, 20]);\n", encoding="utf-8")
        project.stl_path.write_bytes(cube_stl(20.0))
        project.revision = 2
        project.metrics = {"bbox": {"x": 20.0, "y": 20.0, "z": 20.0}}
        store.save(project)
        store.append_history(
            project,
            projects_mod.HistoryEntry(
                revision=2, created_at=projects_mod.now_iso(), instruction="改成 20mm",
                summary="20mm 立方体", name=project.name, stl=True, metrics=project.metrics,
            ),
        )

        # 归档编号必须对应「那一版」的实际内容（这次实测踩到的错位）
        archived = (project.revision_dir(1) / "model.scad").read_text(encoding="utf-8")
        assert "cube([10, 10, 10])" in archived, f"r001 里应当是第 1 版的内容，实际：{archived[:80]!r}"
        assert (project.revision_dir(1) / "model.stl").is_file(), "第 1 版应当带 STL"

        client = TestClient(create_app(), base_url="http://127.0.0.1:8760", raise_server_exceptions=False)
        listing = client.get(f"/api/projects/{project.id}").json()
        assert len(listing["history"]) >= 2, listing["history"]

        restored = client.post(f"/api/projects/{project.id}/restore", json={"revision": 1})
        assert restored.status_code == 200, restored.text
        payload = restored.json()
        assert payload["restored_from"] == 1 and payload["revision"] == 3, payload

        now_scad = project.scad_path.read_text(encoding="utf-8")
        assert "cube([10, 10, 10])" in now_scad, f"回退后应当是第 1 版的源码：{now_scad[:80]!r}"
        from aiforge import mesh as mesh_mod

        metrics = mesh_mod.analyze(project.stl_path)
        assert abs(metrics.bbox[0] - 10.0) < 0.01, f"回退后的模型应当是 10mm 立方体，实际 {metrics.bbox}"
        # 回退会生成**新的一版**，历史只追加不重写
        detail = client.get(f"/api/projects/{project.id}").json()
        assert detail["revision"] == 3, detail["revision"]
        assert any("回退" in (entry.get("instruction") or "") for entry in detail["history"]), detail["history"]
        # 第 2 版仍在历史里（用户回退错了还能回来）
        assert (project.revision_dir(2) / "model.scad").is_file(), "回退不该抹掉被回退掉的那一版"

        # 不存在的版本必须给出明确错误，而不是静默成功
        missing = client.post(f"/api/projects/{project.id}/restore", json={"revision": 99})
        assert missing.status_code == 404, missing.text
        # 缺 STL 的历史版本也不能假装成功
        (project.revision_dir(1) / "model.stl").unlink()
        no_stl = client.post(f"/api/projects/{project.id}/restore", json={"revision": 1})
        assert no_stl.status_code == 409, no_stl.text

        shutil.rmtree(project_dir, ignore_errors=True)
    return "归档编号与内容一致、恢复的是那一版、历史只追加、缺版本/缺 STL 明确报错"


def t_mesh_quality_adoption() -> str:
    """修复候选的「网格质量不比原版差」判据（钉住实测踩过的那两个数）。

    为什么这条必须有：三个采纳点原本都写成
    `retry.watertight or not best.watertight`，原版不水密时恒为真 ——
    第三轮基准实测到逐轴校正把 28 条非流形边（流形边占比 0.9948）
    换成 287 条（占比 0.3376）还当成功采纳，交付出去切片就是废件。
    """
    from aiforge import jobs, mesh

    def mk(*, watertight, ratio, boundary=0, nonmanifold=0, degenerate=0):
        return mesh.MeshMetrics(
            watertight=watertight, watertight_ratio=ratio, boundary_edges=boundary,
            non_manifold_edges=nonmanifold, degenerate_triangles=degenerate,
        )

    before = mk(watertight=False, ratio=0.9948, nonmanifold=28)
    worse = mk(watertight=False, ratio=0.3376, nonmanifold=287)
    assert jobs._mesh_quality_not_worse(before, before), "同一版该判为不差"
    assert not jobs._mesh_quality_not_worse(before, worse), \
        "28 条非流形边换成 287 条必须判为更差（实测踩过的坑）"
    assert jobs._mesh_quality_not_worse(before, mk(watertight=True, ratio=1.0)), "修好了要允许采纳"
    assert not jobs._mesh_quality_not_worse(
        mk(watertight=True, ratio=1.0), mk(watertight=False, ratio=0.999, nonmanifold=1)
    ), "原版水密时不能用别的指标换封闭性"
    assert not jobs._mesh_quality_not_worse(
        mk(watertight=False, ratio=0.9, boundary=5),
        mk(watertight=False, ratio=0.95, boundary=9),
    ), "开放边变多就是变差，占比涨了也不行"
    # rank 的末项必须是连续量：28 与 287 条非流形边不能同分
    assert jobs._integrity_rank(before) < jobs._integrity_rank(worse), \
        "rank 分不出脏净，修复循环就会在两个破网格中随机挑"

    # 接线：三个采纳点都必须改用这个判据（不许再出现布尔写法）
    import inspect

    src = inspect.getsource(jobs.GenerationRunner._repair_geometry_integrity)
    assert "retry_metrics.watertight or not best_metrics.watertight" not in src, \
        "又用回了「两边都不水密就放行」的布尔判据"
    used = src.count("_mesh_quality_not_worse(best_metrics, retry_metrics)")
    assert used >= 3, f"三个采纳点应各用一次，实际 {used} 次"
    return "网格质量判据按实测数字生效（28→287 非流形边被拒），三个采纳点全部接线"


def t_geometry_repair() -> str:
    """尺寸/结构自动修复的判定逻辑（离线，不调用 AI）。

    为什么这条必须有：生成的模型「是不是一整件」「特征有没有穿进腔体」
    「用户点名的尺寸有没有做到」只靠提示词约束不住 ——
    6 个真实复杂实例实测，第一次生成有 5 个翻车。
    所以真正的保障是「实测 → 把证据交回给 AI → 只采纳更好的那一版」这条闭环。
    这里断言闭环的**决策部分**：
      * 只有真的存在缺陷时才触发修复（不能无缘无故多花一次 AI 调用）
      * 证据必须带上实测数字（泛泛的「请保证连通」没有用）
      * 采纳条件必须是「严格更好」—— 平手或更差一律保留原版
    """
    from aiforge import jobs, mesh
    from aiforge.design import DesignSpec, SpecItem

    # 合格模型：一整件、尺寸达标、网格封闭 → 不该触发任何修复
    clean = mesh.MeshMetrics(
        solid_parts=1, stray_parts=0, cavity_islands=[], watertight=True,
        boundary_edges=0, non_manifold_edges=0, watertight_ratio=1.0,
    )
    assert jobs._geometry_integrity_problems(clean) == [], "合格模型不该触发修复"
    assert jobs._integrity_rank(clean) == (0, 0, 0, 0, 0, 0, 0)

    # 散件：证据里必须出现实测的零件数
    stray = mesh.MeshMetrics(solid_parts=80, stray_parts=79, cavity_islands=[])
    problems = jobs._geometry_integrity_problems(stray)
    assert len(problems) == 1, problems
    assert "80 个互不相连的零件" in problems[0], problems[0]
    assert "79 个散件" in problems[0], problems[0]

    # 穿腔：证据里必须给出具体高度与处数
    island = mesh.MeshMetrics(
        solid_parts=1, stray_parts=0, cavity_islands=[(9.4, 4), (37.1, 4)]
    )
    problems = jobs._geometry_integrity_problems(island)
    assert len(problems) == 1, problems
    assert "9mm 处 4 处" in problems[0] and "37mm 处 4 处" in problems[0], problems[0]
    assert "穿进了腔体" in problems[0], problems[0]

    # 注意：下面凡是 watertight=True 的夹具都必须显式写 watertight_ratio=1.0。
    # rank 的末项是「1 - 流形边占比」的连续量，字段默认 0.0 意味着「面全穿插」，
    # 漏写会让一个自称封闭的夹具在末项上白白背负 1000 分，比较结果就不可信了。
    # 两类同时存在时都要报出来（AI 需要一次看全）
    both = mesh.MeshMetrics(
        solid_parts=3, stray_parts=2, cavity_islands=[(5.0, 1)],
        watertight=True, watertight_ratio=1.0,
    )
    assert len(jobs._geometry_integrity_problems(both)) == 2

    # ---- 用户点名的整体尺寸必须被核对（首次生成也要，不只是改动流程）----
    # 以前 acceptance 里的「总高等于 100mm」只写给 AI 看，从没有程序核对过：
    # 用户说 100mm、模型出来 62mm，是没有任何东西拦得住的。
    spec = DesignSpec(
        kind="桌面手机支架",
        specs=[
            SpecItem(name="height", label="支架高度", value=100, unit="mm", user_specified=True),
            SpecItem(name="width", label="支架宽度", value=80, unit="mm", user_specified=True),
            SpecItem(name="wall_thickness", label="壁厚", value=2.4, unit="mm", user_specified=True),
            SpecItem(name="depth", label="支架深度", value=None, unit="mm", user_specified=False),
        ],
    )
    declared = jobs._spec_declared_dimensions(spec)
    # 返回 (给人看的标签, 期望值, 规格名)。规格名必须带上 —— 判断"这一项本次是否
    # 被授权修改"要用它（契约里用的是规格名，而标签是中文）。
    assert declared[2] == ("支架高度", 100.0, "height"), declared
    assert declared[0] == ("支架宽度", 80.0, "width"), declared
    assert 1 not in declared, "用户没指定的方向不该编出一个期望值"
    assert all(label != "壁厚" for label, _value, _name in declared.values()), (
        "壁厚是零件内部尺寸，不能拿去和包围盒比"
    )

    # 实测远小于声明值 → 必须报出来，并带上双方的具体数字
    short = mesh.MeshMetrics(bbox=(80.0, 50.0, 62.0), watertight=True, watertight_ratio=1.0)
    dim_problems = jobs._spec_dimension_problems(spec, short)
    assert len(dim_problems) == 1, dim_problems
    assert "支架高度 要求 100mm" in dim_problems[0] and "只有 62.0mm" in dim_problems[0], dim_problems[0]
    # 证据还要带上「差多少」「占比」和最可能的成因。
    # 只写「要求 100、实测 62」时，实测发现 AI 会反复重写同一段公式
    # （它以为自己算对了），两轮修复都修不动 —— 问题 5（挤出写法）证明
    # 「把机制点破 + 给出正确写法」这类证据是有效的。
    assert "差 38.0mm" in dim_problems[0], dim_problems[0]
    assert "62%" in dim_problems[0], dim_problems[0]
    assert "倾斜件" in dim_problems[0] and "rotate" in dim_problems[0], dim_problems[0]
    assert "把参数值改大不算" in dim_problems[0], dim_problems[0]

    # 只有水平方向没做到时，不该塞进「倾斜件」那套建议（避免把 AI 带偏）
    width_short = mesh.MeshMetrics(bbox=(50.0, 50.0, 100.0), watertight=True)
    width_problems = jobs._spec_dimension_problems(spec, width_short)
    assert len(width_problems) == 1, width_problems
    assert "支架宽度 要求 80mm" in width_problems[0], width_problems[0]
    assert "倾斜件" not in width_problems[0], width_problems[0]

    # 唯一的缺陷就是「点名的尺寸没做到」（第 2 项），其余各项归零
    assert jobs._integrity_rank(short, spec) == (0, 0, 1, 0, 0, 0, 0)

    # 实测偏大是正常的（模型常多出底座/托边），绝不能误报
    taller = mesh.MeshMetrics(bbox=(80.0, 50.0, 140.0))
    assert jobs._spec_dimension_problems(spec, taller) == [], (
        "实测比要求高是正常的（含底座等结构），不能当成错误"
    )
    # 阈值：低于声明值 10% 以内算做到（模型有圆角/倒角时允许一点出入）
    borderline = mesh.MeshMetrics(bbox=(80.0, 50.0, 92.0))
    assert jobs._spec_dimension_problems(spec, borderline) == []

    # ---- 但「远远超出」必须报出来（圆件的主要整体尺寸以前根本没校验）----
    # 实测：用户要 Ø80 的笔筒，AI 把周向的筋画到了错误的平面，
    # 出来一个 92×145.8×100 的块料；程序全程沉默（直径被当成"内部尺寸"跳过），
    # 只给了个 warn。现在外径要按**水平最大尺寸**核对，且超出 50% 就报。
    round_spec = DesignSpec(
        kind="圆形笔筒",
        specs=[
            SpecItem(name="outer_diameter", label="外径", value=80, unit="mm", user_specified=True),
            SpecItem(name="height", label="总高", value=100, unit="mm", user_specified=True),
        ],
    )
    declared_round = jobs._spec_declared_dimensions(round_spec)
    assert declared_round[jobs.HORIZONTAL_ENVELOPE_AXIS][1] == 80.0, declared_round
    good_round = mesh.MeshMetrics(bbox=(82.4, 82.4, 100.0), watertight=True)
    assert jobs._spec_dimension_problems(round_spec, good_round) == [], "Ø82 的笔筒是达标的"
    blob = mesh.MeshMetrics(bbox=(92.28, 145.83, 100.0), watertight=True)
    blob_problems = jobs._spec_dimension_problems(round_spec, blob)
    assert len(blob_problems) == 1, blob_problems
    assert "外径 要求 80mm" in blob_problems[0], blob_problems[0]
    assert "145.8mm" in blob_problems[0] and "82%" in blob_problems[0], blob_problems[0]
    assert "错误的平面" in blob_problems[0], blob_problems[0]
    # 内部直径（孔径/内径/螺孔）仍然不能拿去跟外形比
    for label, name in (("孔径", "hole_diameter"), ("内径", "inner_diameter"),
                        ("螺纹外径", "thread_dia")):
        inner_spec = DesignSpec(
            kind="件",
            specs=[SpecItem(name=name, label=label, value=8, unit="mm", user_specified=True)],
        )
        assert not jobs._spec_declared_dimensions(inner_spec), f"{label} 是内部尺寸，不该拿去比外形"

    # 采纳条件：先比散件、再比点名尺寸、再比穿腔、最后比封闭性；平手必须判为「没有更好」
    assert jobs._integrity_rank(both) == (0, 2, 0, 1, 0, 0, 0)
    assert jobs._integrity_rank(
        mesh.MeshMetrics(
            stray_parts=2, cavity_islands=[], watertight=True, watertight_ratio=1.0
        )
    ) < jobs._integrity_rank(both)
    assert not (
        jobs._integrity_rank(
            mesh.MeshMetrics(
                stray_parts=2, cavity_islands=[(5.0, 1)], watertight=True, watertight_ratio=1.0
            )
        )
        < jobs._integrity_rank(both)
    ), "完全相同的结果不能算改善，否则会白白换掉用户已经在看的模型"
    # 优先级：用户说过的数字做不到（rank 更大=更差）比腔内有孤岛更严重，
    # 因为前者意味着「根本没听懂需求」，后者是内部结构瑕疵。
    # 两个模型都用同一个 spec 比较，保证是同一把尺子。
    island_only = mesh.MeshMetrics(
        bbox=(80.0, 50.0, 100.0), cavity_islands=[(5.0, 1)], watertight=True, watertight_ratio=1.0
    )
    assert jobs._spec_dimension_problems(spec, island_only) == [], "这个模型的尺寸是达标的"
    assert jobs._integrity_rank(short, spec) > jobs._integrity_rank(island_only, spec), (
        "「点名尺寸没做到」应当排在「腔内有孤岛」前面（更严重）"
    )

    # ---- 破网格（开放边）必须能触发修复，而且能作为「平手时的加分项」胜出 ----
    # 实测踩到：端到端送出去过一个 736 条开放边的 STL（切出来是废件），
    # 而修复循环**从来不会去修它** —— 破网格当时不在任何一类问题里；
    # 就算被触发，其它指标平手的候选也会被「严格更好」的门槛挡掉。
    # 程序只在打印性报告里如实写了「建议让 AI 重新生成」。
    holed = mesh.MeshMetrics(
        solid_parts=1, stray_parts=0, cavity_islands=[], watertight=False,
        boundary_edges=736, non_manifold_edges=103, watertight_ratio=0.2331,
    )
    sealed = mesh.MeshMetrics(
        solid_parts=1, stray_parts=0, cavity_islands=[], watertight=True,
        boundary_edges=0, non_manifold_edges=0, watertight_ratio=1.0,
    )
    hole_problems = jobs._geometry_integrity_problems(holed)
    assert len(hole_problems) == 1, hole_problems
    assert "736 条**开放边**" in hole_problems[0], hole_problems[0]
    assert "23.3%" in hole_problems[0], hole_problems[0]
    assert jobs._integrity_rank(sealed) < jobs._integrity_rank(holed), (
        "封闭性必须参与排序，否则「只把网格修好了」的候选会被判成平手而丢弃"
    )
    # 零星开放边（切片软件能自己补）**不该**花掉用户一次 AI 调用
    assert jobs._geometry_integrity_problems(
        mesh.MeshMetrics(solid_parts=1, boundary_edges=9, watertight=False)
    ) == [], "9 条开放边不值得触发一次 AI 修复；打印性报告里照样会如实说明"
    # 非流形边单独出现（开放边为 0）也不触发：CGAL 在两实体恰好相切时极常见，
    # 多数切片软件能处理，拿它驱动修复只会白花额度
    manifold_only = mesh.MeshMetrics(
        solid_parts=1, boundary_edges=0, non_manifold_edges=103, watertight=False
    )
    assert jobs._geometry_integrity_problems(manifold_only) == [], jobs._geometry_integrity_problems(
        manifold_only
    )
    # 但排序仍然偏好没有非流形边的那一版（其它指标持平时）
    assert jobs._integrity_rank(sealed) < jobs._integrity_rank(manifold_only)
    # 封闭性**绝不能**压过「是不是一整件」：多出一个散件换网格封闭不算改善
    assert jobs._integrity_rank(
        mesh.MeshMetrics(stray_parts=0, watertight=False, boundary_edges=736)
    ) < jobs._integrity_rank(
        mesh.MeshMetrics(stray_parts=1, watertight=True, watertight_ratio=1.0)
    ), (
        "散件（是不是一件东西）必须排在封闭性前面 —— 不能拿「多一件」去换「网格封闭」"
    )

    # ---- 「装不进打印机」必须是第一位，修复不能把能打印的模型换成装不下的 ----
    # 这是实测踩出来的回归：修复环节排在「成型范围重做」之后，
    # 而它原本只看散件/尺寸/穿腔，**不看还放不放得下** ——
    # 于是它把一个已经缩到 180mm、刚好能打印的模型，
    # 换成了一个「更连通但 234mm」的版本，越界问题又被打破（端到端实测）。
    oversized = mesh.MeshMetrics(
        solid_parts=1, stray_parts=0, cavity_islands=[], exceeds_build_volume=True
    )
    fitting_but_flawed = mesh.MeshMetrics(
        solid_parts=4, stray_parts=3, cavity_islands=[(5.0, 2)]
    )
    assert jobs._integrity_rank(oversized) > jobs._integrity_rank(fitting_but_flawed), (
        "装不下（哪怕其它方面都干净）必须比「放得下但有散件/穿腔」更差，"
        "否则修复会为了连通性牺牲可打印性"
    )
    # 反过来：放得下的一方永远更优，无论对方多干净
    assert jobs._integrity_rank(mesh.MeshMetrics(solid_parts=1)) < jobs._integrity_rank(
        mesh.MeshMetrics(solid_parts=1, exceeds_build_volume=True)
    )

    # 修复通道确实接在生成流程上，且失败时保留原结果
    import inspect

    runner = jobs.GenerationRunner
    assert hasattr(runner, "_repair_geometry_integrity"), "生成流程缺少几何修复环节"
    body = inspect.getsource(runner._repair_geometry_integrity)
    assert "return source, compile_result, metrics" in body, "修复失败必须保留原结果"
    # 每一处「修不动」的分支都必须 early-break 出循环，最后由统一的出口保留原版；
    # 采纳只发生在 `_integrity_rank(retry_metrics, ...) < best_rank` 这一个条件里。
    # （排序键现在多带一个契约参数 —— 授权改动不该被旧规格扣分，见 t_repair_wiring）
    # 采纳路径现在有两处，各自有独立门：
    #   1) 修复环：rank 严格更好 + 封闭性不变差；
    #   2) 逐轴校正兜底：实测尺寸偏差严格更小（rank 对「偏 8% 但没报问题」不敏感）。
    assert body.count("best_rank = _integrity_rank(retry_metrics, spec, self._change_contract)") == 2, (
        "采纳路径应为两处：修复环的 rank 比较 + 逐轴校正的偏差比较"
    )
    assert "dev_after < dev_before - 1e-3" in body, "逐轴校正采纳必须用实测偏差更小作门"
    assert "_mesh_quality_not_worse(best_metrics, retry_metrics) and _integrity_rank(retry_metrics, spec, self._change_contract) < best_rank" in body, (
        "只有网格质量不变差且严格更好的版本才允许采纳"
    )
    # 封门条件必须写成「网格质量不比原版差」，而不是「候选必须封闭」，
    # 也不是只比 watertight 布尔值（后者在原版已经封闭时才等价，
    # 原版不封闭时等于「修成多破都行」，实测就因此把 28 条非流形边换成 287 条）。
    # 完整判据与接线见 t_mesh_quality_adoption，这里只钉住修复环入口。
    assert "for attempt in (1, 2)" in body, "散件修复必须允许第二轮换策略"
    assert "spec" in inspect.signature(runner._repair_geometry_integrity).parameters, (
        "核对点名尺寸需要拿到规格，签名里必须有 spec"
    )
    finish = inspect.getsource(runner._finish)
    assert "_repair_geometry_integrity" in finish, "修复环节没有被 _finish 调用"

    # 给 AI 的证据链必须包含实测事实，而不是只让它「再检查一遍」
    summary = mesh.summarize_for_ai(island, "m.stl")
    assert "腔体孤岛检查" in summary, summary[-200:]
    # 散件明细必须带位置和尺寸：只说「有 80 个散件」时 AI 根本改不动
    detail_summary = mesh.summarize_for_ai(stray, "s.stl")
    assert "散件明细" in detail_summary, detail_summary[-300:]
    assert jobs._geometry_integrity_problems(
        mesh.MeshMetrics(
            solid_parts=3,
            stray_parts=2,
            stray_details=[
                {"center": [1.0, 2.0, 3.0], "size": [4.0, 5.0, 6.0], "volume_mm3": 120.0, "faces": 12}
            ],
        )
    )[0].count("中心(1.0,2.0,3.0)") == 1, "证据里必须带上散件位置"

    return "缺陷判定/证据构造/采纳门槛/流程接线 全部正确"


def t_module_aliases() -> str:
    """静态检查：用了某个兄弟模块，但没把它 import 进来。

    为什么专门查这个：实测踩过两次同类的坑 ——
    `_scale_to_fit` 里用了没定义的 `params`、`_review_snapshot` 里用了没导入的 `llm`。
    两次都是「运行到那一行才炸」，而它们藏在很深的流程里：
    前者只在缩放兜底时触发，后者只在真的渲染复核时才触发 ——
    单测和自检都照不到，只能靠一次完整的端到端运行才发现，代价很高。

    这里用 AST 做一件很窄但很准的事：找出 `xxx.attr` 这种用法中，
    `xxx` 是本项目的兄弟模块名、却不在该文件已导入的别名集合里。
    窄检查的好处是不会误报 —— 只认确定的模块名，不猜。
    """
    import ast

    source_dir = ROOT / "src" / "aiforge"
    module_names = {p.stem for p in source_dir.glob("*.py")} - {"__init__"}
    checked = 0
    problems: list[str] = []

    for path in sorted(source_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported.add(alias.asname or alias.name)
        # 函数参数与局部变量同名的情况也要放行：只有「像模块」的名字才检查
        bound: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound.add(node.name)
                for arg in list(node.args.args) + list(node.args.kwonlyargs):
                    bound.add(arg.arg)
            elif isinstance(node, ast.ClassDef):
                bound.add(node.name)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            value = node.value
            if not isinstance(value, ast.Name):
                continue
            name = value.id
            if name in imported or name in bound:
                continue
            if name in module_names:
                problems.append(
                    f"{path.name}:{node.lineno} 用了 `{name}.{node.attr}`，"
                    f"但 `{name}` 没有导入（也没有同名局部变量）"
                )
        checked += 1

    assert not problems, "发现未导入的模块引用：\n    " + "\n    ".join(problems)
    return f"{checked} 个模块的 `模块名.成员` 引用全部有对应导入"


def t_visual_review() -> str:
    """外观复核（渲染 + 多模态判读）的离线部分（不调用 AI）。

    为什么这条必须有：**所有确定性指标全绿，模型仍然可能是错的**。
    实测过的桌面垃圾桶，4 根外壁加强筋从外壁穿进了桶内腔，
    而 solid_parts==1、watertight==True、包围盒体积全部正常 ——
    这类「测不出来但一眼看得出」的问题只能靠渲染图复核。

    这里断言：
      * 渲染器能真的画出东西（不是一张空图）
      * 渲染是确定性的（同样输入给同样输出），否则没法复现问题
      * 判读方向必须是「只有明确说有问题才算有问题」，空回复不能算通过
    """
    from aiforge import compiler, llm, render

    import inspect

    work = ROOT / "build" / "work" / "rendertest"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    # 一个 40mm 立方体：从任何角度看都该占据画面中间一块明显区域
    cube = compiler.compile_to_stl("cube([40,40,40], center=true);", work, "cube")
    assert cube.ok, cube.message
    shots = render.render_views(cube.stl_path, work, prefix="cube", size=200)
    assert len(shots) == len(render.VIEWS) == 2, f"应渲染 2 个视角，实际 {len(shots)}"
    for shot in shots:
        assert shot.path.is_file(), shot.path
        raw = shot.path.read_bytes()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n", "渲染结果必须是合法 PNG"
        assert shot.width == 200 and shot.height == 200, shot
        assert not shot.sampled, "12 个面不该触发抽样"
        assert shot.triangles_used == 12, shot.triangles_used

    # 画面里必须真的有模型：统计非背景像素占比。
    # 用 zlib 解回像素，避免为了测试引入图像库。
    import struct as _struct
    import zlib as _zlib

    def non_background_ratio(path: Path) -> float:
        raw = path.read_bytes()
        pos = 8
        idat = b""
        width = height = 0
        while pos < len(raw):
            (length,) = _struct.unpack_from(">I", raw, pos)
            tag = raw[pos + 4 : pos + 8]
            data = raw[pos + 8 : pos + 8 + length]
            if tag == b"IHDR":
                width, height = _struct.unpack_from(">II", data, 0)
            elif tag == b"IDAT":
                idat += data
            pos += 12 + length
        pixels = _zlib.decompress(idat)
        stride = width * 3
        hits = 0
        total = width * height
        for row in range(height):
            start = row * (stride + 1) + 1
            line = pixels[start : start + stride]
            for col in range(0, stride, 3):
                if abs(line[col] - render.BACKGROUND[0]) > 4:
                    hits += 1
        return hits / total if total else 0.0

    for shot in shots:
        ratio = non_background_ratio(shot.path)
        assert ratio > 0.10, f"{shot.view} 视角几乎没有画出模型（非背景占比 {ratio:.1%}）"
        assert ratio < 0.98, f"{shot.view} 视角整屏都是模型，可能没有留边（{ratio:.1%}）"

    # 确定性：同样的输入必须给出同样的图，否则渲染根本没法用来复现问题
    again = render.render_views(cube.stl_path, work, prefix="cube2", size=200)
    assert again[0].path.read_bytes() == shots[0].path.read_bytes(), (
        "同样的模型两次渲染结果不一致，渲染过程引入了随机性"
    )

    # 抽样渲染时的取景必须按**全量顶点**算（本轮实测的坑）：
    # 原来中心与最长边是从 `triples`（抽样后拿去画的那批）里来的，
    # 最端的点没被抽到时就把最长边算小、scale 算大，模型超出画布被裁掉一角 ——
    # 而这张图是给 AI 判「有没有缺件」用的，自己先缺就是谎报。
    far = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 10.0, 0.0), (0.0, 10.0, 10.0),
           (200.0, 200.0, 200.0)]
    c_all, span_all = render._frame_all(far)  # noqa: SLF001 - 验的就是这个取值口径
    subset = far[:4]  # 模拟「最远那个点没被抽到」
    c_sub, span_sub = render._frame_all(subset)  # noqa: SLF001
    assert span_all > span_sub * 1.5, f"抽样前后的最长边没差别（{span_all} / {span_sub}），用例是空跑"
    assert c_all != c_sub, "抽样前后的中心没差别，这个夹具验不出错位"
    flat = [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0)]
    _, span_flat = render._frame_all(flat)  # noqa: SLF001
    assert span_flat > 0, "退化的零尺寸模型会把 scale 算成除零"
    # 上面的夹具只证明「取值口径区分得开」，还不能证明画图时真的用了它 ——
    # 这一条盯调用点：只能拿全量 points 算框，不准退回用抽样集算。
    src_views = inspect.getsource(render.render_views)
    assert "_frame_all(points)" in src_views, "render_views 不再用全量顶点算框（大模型会被裁角）"
    assert "for t in triples for p in t" not in src_views, (
        "又回到用抽样出来的 triples 算包围盒：最端点被抽掉时整张复核图会裁掉一角"
    )

    # 判读方向：只有明确说「有问题」才算有问题。
    # 把「没结论」当成「没问题」会静默放过缺陷，这是最危险的失败方向。
    # 三态之前被压成 bool 的坑：「没结论」和「真的没问题」共用一个 False，
    # 调用方（jobs._review_snapshot）只能把它们一起播成 success。
    assert llm.parse_snapshot_verdict("结论：有问题\n桶口有两个多余凸起") == llm.VERDICT_PROBLEM
    assert llm.parse_snapshot_verdict("结论：无明显问题") == llm.VERDICT_CLEAR
    assert llm.parse_snapshot_verdict("结论：**有问题**") == llm.VERDICT_PROBLEM
    assert llm.parse_snapshot_verdict("") == llm.VERDICT_UNKNOWN, "空回复必须算「没有结论」"
    assert llm.parse_snapshot_verdict("我看不太清楚，可能有吧") == llm.VERDICT_UNKNOWN, "含糊的答复不算数"
    assert llm.parse_snapshot_verdict("好的，包围盒正常，水密，可用于 3D 打印。") == llm.VERDICT_UNKNOWN, (
        "只复述指标不给结论也不算「没问题」——必须能被调用方区分于真的「无明显问题」"
    )
    # 上面几条都是拿常量本身去比函数返回值，即使常量被意外改回同一个值也自洽，
    # 抓不到「三态被重新坍缩成两态」（就是本次要修的坑本身）。这一条直接比常量本身。
    assert len({llm.VERDICT_PROBLEM, llm.VERDICT_CLEAR, llm.VERDICT_UNKNOWN}) == 3, (
        "VERDICT_PROBLEM/CLEAR/UNKNOWN 必须互不相同；一旦有两态合并，就退回了修好之前的 bool 老坑"
    )

    # 视觉结论只有「明确有问题」时才并进修复证据
    from aiforge import jobs, mesh

    clean = mesh.MeshMetrics(solid_parts=1, stray_parts=0, cavity_islands=[])
    assert jobs._geometry_integrity_problems(clean, None, "结论：无明显问题\n正常") == []
    flagged = jobs._geometry_integrity_problems(clean, None, "结论：有问题\n桶口有两个多余凸起")
    assert len(flagged) == 1 and "图上确实可见" in flagged[0], flagged

    # 接口事实：图片复核必须用会看图的模型，且 token 预算必须给足。
    # 这两条都是实测出来的坑，写死在断言里防止以后被「顺手改回去」：
    #   * deepseek-v4-pro 收到图会回答「我无法查看图片」，静默忽略图片
    #   * max_tokens=2048 时思考过程吃满额度，finish_reason=length、content 为空 ——
    #     表现成「模型没意见」，其实什么都没检查
    assert llm.VISION_MODEL == "deepseek-flash", llm.VISION_MODEL
    assert llm.VISION_MAX_TOKENS >= 8192, (
        f"图片复核额度 {llm.VISION_MAX_TOKENS} 太小：实测 2048 时思考过程会吃满额度并返回空正文"
    )
    assert llm.VISION_MAX_TOKENS_RETRY > llm.VISION_MAX_TOKENS
    review_body = inspect.getsource(llm.DeepSeekClient.review_snapshot)
    assert '"length"' in review_body, "必须识别「额度用尽导致的空正文」，不能当成没问题"
    # 本轮修的真缺陷：放大重试的终止判据以前写的是 `budget >= VISION_MAX_TOKENS_RETRY`，
    # 拿的是 **DeepSeek 的常量**当尺；而 `vision_max_tokens_for` 对智谱返回 `(1024, 1024)`
    # （上限就 1024，根本没有更大的档可试），`1024 >= 16384` 永远为假 —— 于是同一个 1024
    # 额度被原样重发一遍（双倍延迟/双倍计费面，却拿不到任何新信息）。
    # 不能用 `"VISION_MAX_TOKENS_RETRY" not in review_body` 这种裸字符串包含判断：
    # 下面修复时写的解释注释本身就提到了这个旧写法，会被误判成「还在用」（同一文件里
    # `_try_fallback` 那条已经踩过这个坑，改用 AST 而不是正则/裸字符串）。
    import ast as _ast_budget

    review_tree_budget = _ast_budget.parse(inspect.cleandoc(review_body))
    loaded_names_budget = {
        n.id
        for n in _ast_budget.walk(review_tree_budget)
        if isinstance(n, _ast_budget.Name) and isinstance(n.ctx, _ast_budget.Load)
    }
    called_names_budget = {
        n.func.id
        for n in _ast_budget.walk(review_tree_budget)
        if isinstance(n, _ast_budget.Call) and isinstance(n.func, _ast_budget.Name)
    }
    assert "VISION_MAX_TOKENS_RETRY" not in loaded_names_budget and "VISION_MAX_TOKENS" not in loaded_names_budget, (
        "review_snapshot 不能直接拿 DeepSeek 的 VISION_MAX_TOKENS/RETRY 常量当尺，"
        "必须走 vision_max_tokens_for() 按提供商分（否则智谱用户会被拿 16384 跟自己的 1024 上限做比较）"
    )
    assert "vision_max_tokens_for" in called_names_budget, (
        "review_snapshot 必须调用 vision_max_tokens_for() 拿到按提供商算好的额度档位"
    )
    # 本轮新查出的缺陷：`_try_fallback` 是「文本模型过载时降级」的工具函数，会改写
    # `payload["model"]`。以前 `review_snapshot` 在 >=400 时也调了它 —— 于是视觉请求
    # 的模型被换成一个**不会看图**的文本模型重发，那个模型会回一句「我无法查看图片」
    # 这种**非空**正文，看起来像复核成功、其实一个字也没看。上面「接口事实」那两条
    # 注释禁止的就是这件事，不能从降级工具函数那条路绕回去。
    # 不能用 `"_try_fallback" not in review_body` 这种裸字符串包含判断：下面修复后写的
    # 解释注释本身就提到了这个词，会被误判成「还在调用」（这个坑在本文件其他地方已经
    # 踩过一次，当时也是用 AST 而不是正则解决，见下方对 `_review_snapshot` 的处理）。
    import ast as _ast_try

    review_tree = _ast_try.parse(inspect.cleandoc(inspect.getsource(llm.DeepSeekClient.review_snapshot)))
    called_names = {
        n.func.attr
        for n in _ast_try.walk(review_tree)
        if isinstance(n, _ast_try.Call) and isinstance(n.func, _ast_try.Attribute)
    }
    assert "_try_fallback" not in called_names, (
        "视觉复核不能走 `_try_fallback`：它会把会看图的模型换成不会看图的文本模型"
    )
    # 本轮清查出的真缺陷：那份系统提示词写好了却从没发出去过，而用户提示里还写着
    # 「请按系统提示复核」—— parse_snapshot_verdict 的第一行契约判定因此始终落空，
    # 只能退到全文关键词兜底（看起来能跑，实际把最可靠的一层判据去掉了）。
    # 光靠「常量有没人引用」不够，必须钉住发出去的 messages 里有 system 这一条。
    assert '"role": "system"' in review_body, (
        "视觉复核必须把系统提示词发出去：第一行契约就写在那份提示词里"
    )
    assert "SNAPSHOT_REVIEW_SYSTEM_PROMPT" in review_body, (
        "视觉复核的 system 消息必须是 SNAPSHOT_REVIEW_SYSTEM_PROMPT，不能只塞个空壳"
    )
    assert "请按系统提示" in llm.snapshot_review_user_prompt("x", "y"), (
        "用户提示词里「请按系统提示复核」这句话与上面的断言是一对：删了这行就要同步改那行"
    )

    runner = jobs.GenerationRunner
    assert hasattr(runner, "_review_snapshot"), "生成流程缺少外观复核环节"
    finish_body = inspect.getsource(runner._finish)
    # 外观复核必须在几何修复之前：它的结论要作为修复证据的一部分
    assert finish_body.index("_review_snapshot") < finish_body.index("_repair_geometry_integrity"), (
        "外观复核必须排在几何修复之前，否则它发现的程序测不出来的问题进不了修复证据"
    )

    # --- 抽样渲染的告警必须真的发出去（本轮修的真缺陷）---
    # 原来写的是 `if sampled := any(shot.sampled for shot in shots):` 却从不读
    # `sampled` —— 告警语句永远不执行。用户在开着线框/大模型时以为看到的是
    # 完整渲染，实际是抽稀的，而界面一声不吭。这类"算出来却不用"编译器
    # 不报错、运行不报错，只能钉住"这句告警确实在可达路径上"。
    review_body = inspect.getsource(runner._review_snapshot)
    assert "抽样渲染图" in review_body, (
        "面数过多时的抽样渲染告警被删了 —— 用户看到的抽稀图没有任何提示"
    )
    # 判据必须由 render 真实上报的 sampled 驱动，不能写死。
    assert "shot.sampled" in review_body, (
        "抽样告警必须由 render 上报的 sampled 决定，不能凭空判断"
    )
    # 阴性对照：不能是"赋值了却不读"的形状。这里用 AST 而不是正则 ——
    # 正则会连注释里写的 `if x := ...` 示例一起匹配（实测踩到：修好代码后
    # 守卫因为我自己写的解释性注释而变红），而注释不是代码。
    # 判据：NamedExpr（海象）如果所在函数体里再没出现过同一个名字，就是算了不用。
    import ast as _ast

    tree = _ast.parse(inspect.cleandoc(inspect.getsource(runner._review_snapshot)))
    assigned_only: list[str] = []
    for fn in [n for n in _ast.walk(tree) if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))]:
        walruses = [
            n.target.id
            for n in _ast.walk(fn)
            if isinstance(n, _ast.NamedExpr) and isinstance(n.target, _ast.Name)
        ]
        loaded = {
            n.id
            for n in _ast.walk(fn)
            if isinstance(n, _ast.Name) and isinstance(n.ctx, _ast.Load)
        }
        assigned_only.extend(name for name in walruses if name not in loaded)
    assert not assigned_only, (
        f"_review_snapshot 里这些名字赋值后从未被读：{'、'.join(assigned_only)} —— "
        "那正是本缺陷的原始形状（判据算出来了却不生效）"
    )

    # 告警要在 AI 结论**之前**发出：否则用户先看到「未发现明显问题」，
    # 注意力已经被带走，抽稀这件事就再也看不见了。
    if "抽样渲染图" in review_body and "review_snapshot(images" in review_body:
        assert review_body.index("抽样渲染图") < review_body.index("review_snapshot(images"), (
            "抽样告警必须排在 AI 复核请求之前，否则用户先看到结论、后看到告警"
        )

    return (
        f"渲染 {len(shots)} 视角（非背景占比 "
        f"{min(non_background_ratio(s.path) for s in shots):.0%}）、确定性、"
        "判读门槛、模型与额度约束、抽样告警真的发得出去 全部正确"
    )


def t_declared_dependencies() -> str:
    """声明的依赖必须覆盖代码里真正 import 的第三方模块。

    为什么这条属于开源发布的必修项：**clone 之后跑不起来，最常见的原因就是
    requirements.txt / package.json 漏了一个 import**。这类问题在开发机上
    永远暴露不了（依赖早就装好了），只有新用户会踩到，而且报错信息通常
    只是一行 ModuleNotFoundError，很难定位到「谁漏了声明」。

    做法是静态解析，不猜：
      * Python：AST 取出所有 import/from-import 的顶层模块名，
        去掉标准库、本项目自己的模块、相对导入，剩下的第三方模块
        必须在 requirements*.txt 里出现；
      * 前端：正则取出 import ... from 'x'，去掉相对路径与 node: 内置，
        剩下的必须在 package.json 的 dependencies/devDependencies 里。

    import 名与发行包名不一致的（webview ← pywebview）用映射表处理。
    """
    import ast
    import json
    import re

    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    # import 名 → PyPI 发行包名
    alias_to_package = {
        "webview": "pywebview",
        "PyInstaller": "pyinstaller",
        "yaml": "pyyaml",
        "PIL": "pillow",
        "serial": "pyserial",
    }
    local_modules = {p.stem for p in (SRC / "aiforge").rglob("*.py")}
    local_modules |= {
        p.name for p in (SRC / "aiforge").iterdir() if p.is_dir()
    }
    # 本项目根目录下的顶层模块（main.py）与 scripts 目录里的脚本互导
    local_modules |= {"aiforge", "main", "scripts"}
    # 脚本之间按裸名互导（`python scripts\x.py` 时 sys.path[0] 就是 scripts/，
    # 所以是 `import console_utf8` 而不是 `import scripts.console_utf8`）。
    # 少了这一行，任何一个脚本 import 兄弟脚本都会被当成未声明的第三方依赖报出来。
    local_modules |= {p.stem for p in (ROOT / "scripts").glob("*.py")}

    declared: set[str] = set()
    declared_files = []
    for name in ("requirements.txt", "requirements-build.txt"):
        path = ROOT / name
        if not path.exists():
            continue
        declared_files.append(name)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if not line or line.startswith("-"):
                continue
            package = re.split(r"[<>=!\[;]", line)[0].strip().lower()
            if package:
                declared.add(package)
    assert declared, "没有找到任何依赖声明文件"

    targets = [p for p in (SRC.rglob("*.py")) if "__pycache__" not in p.parts]
    targets += [p for p in (ROOT / "scripts").glob("*.py")]
    if (ROOT / "main.py").exists():
        targets.append(ROOT / "main.py")

    used: dict[str, set[str]] = {}
    for path in targets:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            raise AssertionError(f"{path.name} 无法解析：{exc}") from exc
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # 相对导入 = 本项目
                    continue
                if node.module:
                    names = [node.module.split(".")[0]]
            for name in names:
                if name in stdlib or name in local_modules or name.startswith("_"):
                    continue
                used.setdefault(name, set()).add(path.name)

    undeclared = []
    for module in sorted(used):
        package = alias_to_package.get(module, module).lower()
        if package not in declared and module.lower() not in declared:
            undeclared.append(f"{module}（PyPI 名可能是 {package}）← {'、'.join(sorted(used[module]))}")
    assert not undeclared, "有 import 没被声明，clone 后会 ModuleNotFoundError：\n      " + "\n      ".join(undeclared)

    # 前端：import 的包必须在 package.json 里
    package_json = ROOT / "webui" / "package.json"
    assert package_json.exists(), "缺少 webui/package.json"
    meta = json.loads(package_json.read_text(encoding="utf-8"))
    front_declared = set(meta.get("dependencies", {})) | set(meta.get("devDependencies", {}))
    front_used: dict[str, set[str]] = {}
    for path in list((ROOT / "webui" / "src").rglob("*.ts")) + list(
        (ROOT / "webui" / "src").rglob("*.tsx")
    ):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"""^\s*import\s[^;]*?from\s+['"]([^'"]+)['"]""", text, re.M):
            spec = match.group(1)
            if spec.startswith(".") or spec.startswith("/") or spec.startswith("node:"):
                continue
            parts = spec.split("/")
            name = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
            front_used.setdefault(name, set()).add(path.name)
    front_missing = [
        f"{name} ← {'、'.join(sorted(front_used[name]))}"
        for name in sorted(front_used)
        if name not in front_declared
    ]
    assert not front_missing, "前端 import 未在 package.json 声明：\n      " + "\n      ".join(front_missing)

    return (
        f"Python {len(used)} 个第三方模块、前端 {len(front_used)} 个包，"
        f"全部在 {'/'.join(declared_files)} 与 webui/package.json 中声明"
    )


def _requirement_packages(name: str) -> list[str]:
    """读 requirements*.txt 里的包名（去掉注释、空行与 `-r xxx` 引用行）。"""
    import re

    path = ROOT / name
    assert path.is_file(), f"缺少 {name}"
    packages: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.split("#", 1)[0].strip()
        if not text or text.startswith("-"):
            continue
        packages.append(re.split(r"[<>=!\[;]", text)[0].strip().lower())
    return packages


def t_bundle_budget() -> str:
    """体积与依赖预算：“小而美”得是一个会被违反的数字。

    为什么要单独钉一条（`依赖声明完整性` 管的是“用了没声明”，管不到“声明得太多”）：
    每多一个运行期依赖，用户盘上就是一堆 .pyd/.dll；每多一个前端依赖，就是打进
    bundle 的源码。这些东西**只会悄悄长**：加功能的人看不到全貌，谁也不会为
    “就多一个包”单独做一次决策，等安装包从 16MB 变成 60MB 再回就晚了。
    所以把实测基线写成天花板：要加东西可以，但得明着改这里的数字。

    基线（本机实测）：运行期依赖 7 个、构建期再加 1 个；前端 dependencies 4 个；
    dist JS 831 KB / CSS 54 KB；免安装版 39.2 MB / 217 文件；安装包 16.5 MB。
    这轮加“一键送到切片软件”没引入任何新依赖（只用标准库），就是这条规则的执行。
    """
    import json

    checked: list[str] = []
    absent: list[str] = []
    over: list[str] = []  # 一项超预算就记下全部，不撞到第一个就停

    runtime = _requirement_packages("requirements.txt")
    if len(runtime) > 8:
        over.append(
            f"运行期依赖从 7 个涨到 {len(runtime)} 个：{sorted(runtime)}。"
            "它们会被 PyInstaller 全部装进用户机器；确实要加，请连这里的基线一起改并写明理由。"
        )
    checked.append(f"运行期依赖 {len(runtime)}/8")

    extra_build = [p for p in _requirement_packages("requirements-build.txt") if p not in runtime]
    if len(extra_build) > 2:
        over.append(f"构建期专属依赖 {extra_build} 超过预算 2 个（它们不进用户包，但拖慢 CI）")
    checked.append(f"构建期额外 {len(extra_build)}/2")

    package_json = ROOT / "webui" / "package.json"
    assert package_json.is_file(), "缺少 webui/package.json"
    meta = json.loads(package_json.read_text(encoding="utf-8"))
    front = sorted(meta.get("dependencies", {}))
    if len(front) > 5:
        over.append(
            f"前端 dependencies {front} 超过预算 5 个 —— 这些会被打进用户加载的 bundle；"
            "能用现有依赖做到的就别新增包"
        )
    checked.append(f"前端依赖 {len(front)}/5")

    # 产物体积：没构建过的仓库里没这些文件，诚实地写进结果里，不拿“没查到”当“通过”
    dist = ROOT / "webui" / "dist"
    if dist.is_dir():
        for label, suffix, limit_kb in (("JS", ".js", 900), ("CSS", ".css", 64)):
            group = [p for p in dist.rglob(f"*{suffix}") if p.is_file()]
            assert group, f"webui/dist 存在但没有 {label} 产物"
            biggest = max(group, key=lambda p: p.stat().st_size)
            kb = biggest.stat().st_size / 1024
            if kb > limit_kb:
                over.append(
                    f"{label} 产物 {biggest.name} 已经 {kb:.0f} KB，超预算 {limit_kb} KB"
                    "（基线 JS 831 KB / CSS 54 KB）。先看能不能剔掉不用的部分；"
                    "确实需要，就连基线一起改并写明原因。"
                )
            checked.append(f"{label} {kb:.0f}/{limit_kb} KB")
    else:
        absent.append("webui/dist")

    app_dir = ROOT / "build" / "dist" / "AI-3D-Forge"
    if app_dir.is_dir():
        files = [p for p in app_dir.rglob("*") if p.is_file()]
        total_mb = sum(p.stat().st_size for p in files) / 1024 / 1024
        if total_mb > 44:
            over.append(f"免安装版 {total_mb:.1f} MB 超预算 44 MB（基线 39.2 MB）")
        if len(files) > 240:
            over.append(f"免安装版 {len(files)} 个文件，超预算 240（基线 217）")
        checked.append(f"免安装版 {total_mb:.1f}/44 MB・{len(files)}/240 文件")
    else:
        absent.append("免安装版")

    setup = ROOT / "installer" / "Output" / "AI-3D-Forge-Setup.exe"
    if setup.is_file():
        mb = setup.stat().st_size / 1024 / 1024
        if mb > 19:
            over.append(f"安装包 {mb:.1f} MB 超预算 19 MB（基线 16.5 MB）")
        checked.append(f"安装包 {mb:.1f}/19 MB")
    else:
        absent.append("安装包")

    assert not over, "体积/依赖超预算：" + "；".join(over)
    # 同上：一条什么都没查的守卫给的是假安全感。但**下限只能按「不依赖构建产物的那几项」算**：
    # 声明类三项（运行期/构建期/前端依赖）始终要核到，而 JS/CSS/免安装版/安装包
    # 在**新 clone / CI** 上根本不存在（实测克隆体里只剩 3 项，旧下限 4 直接假红）。
    # 没核到的那些由下面的 tail 写明，不拿「没查到」冒充「通过」。
    assert len(checked) >= 3, f"预算守卫只核对了 {len(checked)} 项，太少（守卫没生效？）：{checked}"
    tail = f"（未构建，本次没核对：{'、'.join(absent)}）" if absent else ""
    return "预算内：" + "、".join(checked) + tail


def t_projects() -> str:
    from aiforge import projects

    projects.reset_for_tests()
    store = projects.store()
    project = store.create("自检模型")
    try:
        projects.atomic_write_text(project.scad_path, "cube([5,5,5]);\n")
        project.revision = 1
        project.summary = "自检"
        store.save(project)
        store.append_history(
            project,
            projects.HistoryEntry(revision=1, created_at=projects.now_iso(), summary="自检"),
        )

        loaded = store.get(project.id)
        assert loaded is not None, "应能按 id 读回项目"
        assert loaded.read_scad().startswith("cube"), loaded.read_scad()
        assert len(loaded.history) == 1, loaded.history
        assert loaded.to_summary()["id"] == project.id

        listed = store.list()
        assert any(p.id == project.id for p in listed), "列表应包含新项目"

        # 重名项目不能互相覆盖
        second = store.create("自检模型")
        assert second.dir != project.dir, "重名项目目录必须区分"

        store.rename(loaded, "改名后的模型")
        assert store.get(project.id).name == "改名后的模型"

        assert store.delete(second.id), "删除应成功（移入 .deleted）"
        assert store.get(second.id) is None, "删除后不应再被列出"

        # ---- 用户数据安全：古怪名字不能跑出项目目录 ----
        # 「卸载/升级都不会丢用户数据」是这份文档承诺过的，所以写入位置必须可控：
        # 名字里带路径分隔符、纯点、Windows 保留名、超长、emoji 都不能
        # 让项目目录跑到 Projects 之外（否则创建/删除就是在动别人的目录）。
        root_dir = store.root.resolve()
        for weird in ("", "   ", "...", "..", ".hidden", "CON", "nul", "a" * 200,
                      "名字/带斜杠", "名字\\反斜杠", "名字:带冒号", "🎉emoji"):
            odd = store.create(weird)
            assert odd.dir.resolve().parent == root_dir, (
                f"名字 {weird!r} 让项目目录跑出了项目根目录：{odd.dir}"
            )
            assert len(odd.dir.name) <= 64, f"名字 {weird!r} 生成的目录名过长：{odd.dir.name}"

        # ---- 删除必须是「移入回收站」而不是真删（误删可恢复）----
        victim = store.create("待删除")
        victim_dir = victim.dir
        assert store.delete(victim.id)
        assert not victim_dir.exists(), "删除后原目录应已移走"
        trash = root_dir / ".deleted"
        assert trash.is_dir(), "应存在 .deleted 回收站目录"
        assert any(victim_dir.name in p.name for p in trash.iterdir()), (
            "回收站里应能找到被删的项目（否则就是真删了）"
        )
        # 但回收站里的东西绝不能再出现在项目列表里
        assert victim.id not in [p.id for p in store.list()], "回收站内容不能出现在列表里"

        # ---- 保存/读取往返不能丢字段（含后加的 metrics.usage / visual_review）----
        trip = store.create("往返测试")
        trip.revision = 3
        trip.params = {"height": 150.0, "m3": 3.0}
        trip.metrics = {"volume_mm3": 1234.5, "usage": {"total_grams": 42.0, "material": "PLA"}}
        trip.printability = {"score": 90, "visual_review": "结论：无明显问题"}
        trip.spec = {"kind": "支架", "fingerprint": "abc123"}
        store.save(trip)
        back = store.get(trip.id)
        assert back is not None and back.revision == 3, back
        assert back.params.get("m3") == 3.0, back.params
        assert (back.metrics or {}).get("usage", {}).get("total_grams") == 42.0, back.metrics
        assert (back.printability or {}).get("visual_review") == "结论：无明显问题", back.printability
        assert (back.spec or {}).get("fingerprint") == "abc123", back.spec

        # ---- 列表与查找必须用同一把 id ----
        # meta.json 缺 id 时（被手改过 / 早期版本写坏），旧写法 `list()` 拿「目录名后
        # 6 位」凑一个 id 给用户看，而 `directory_for()` 只比对 meta 里的 id 字段 ——
        # 侧栏里看得见、点进去就是 404，用户的感受是「软件把我的项目弄丢了」。
        orphan = store.root / "缺id项目-ab12cd"
        orphan.mkdir(parents=True, exist_ok=True)
        # 直接写字面 JSON，不依赖 `import json`：本函数里没有这个导入，上一版
        # 在这儿用了 `json.dumps` → NameError，而两条投毒看上去都「转红了」——
        # 那是假证据：红的原因是 NameError，不是断言咬住了缺陷。
        (orphan / "meta.json").write_text(
            '{"name": "缺 id 的项目"}', encoding="utf-8", newline="\n"
        )
        listed_orphans = [p for p in store.list() if p.dir == orphan]
        assert listed_orphans, "没有 id 的项目也必须出现在列表里（它里的 scad/stl 是用户数据）"
        shown = listed_orphans[0]
        back_orphan = store.get(shown.id)
        assert back_orphan is not None, (
            f"列表自己给出去的 id={shown.id!r} 查不回来 —— 侧栏能看见、点进去 404"
        )
        assert back_orphan.name == "缺 id 的项目", back_orphan.name
        assert store.delete(shown.id), "没有 id 的项目连「移入回收站」也做不到"
        assert not orphan.exists(), "删除后原目录应已移走"

        # ---- 截断到 40 位不能留下尾点（实测 Win32 会把尾部点号规范化掉）----
        # 旧写法只剪一次尾巴就截断：`"a"*39 + "." + "b"*8` 切到 40 位正好停在**点号**上，
        # 于是磁盘上的真目录名是 39 个 a，而代码里拿着的名字多一个点 ——
        # 读写靠路径规范化还能命中，但任何拿目录名做相等比较的地方两边对不上。
        longdot = "a" * 39 + "." + "b" * 8
        made = projects.safe_name(longdot)
        assert not made.endswith((".", " ")), f"safe_name 产出以点号/空格结尾的名字：{made!r}"
        dotproj = store.create(longdot)
        try:
            on_disk = sorted(
                p.name for p in store.root.iterdir() if p.is_dir() and p.name.startswith("aaa")
            )
            assert on_disk and dotproj.dir.name == on_disk[0], (
                f"代码里的目录名 {dotproj.dir.name!r} 与磁盘真名 {on_disk} 不一致（尾点被系统吃掉了）"
            )
        finally:
            shutil.rmtree(dotproj.dir, ignore_errors=True)

        # ---- 回收站：列出 / 恢复 / 彻底删除，且名字只能停在 .deleted 里 ----
        # 「删掉的项目只能去资源管理器手动挪回来」以前是写在确认框里的实话，
        # 现在得有界面；而界面一来就带来两个新风险：按名字找目录（路径穿越）
        # 与不可逆的「彻底删除」（不能拿接口被调当用户真想删）。
        binned = store.create("待恢复的项目")
        (binned.dir / "model.stl").write_bytes(b"solid x\n" + b" " * 40)
        store.save(binned)
        binned_id, binned_name = binned.id, binned.name
        assert store.delete(binned.id)
        # 用增量而不是「回收站只有一条」做断言：同一个 store 里前面用例删掉的项目
        # （以及上一次自检的残留）都还在 `.deleted` 里 —— 先写成 ==1 自己被自己绊倒。
        trashed = store.trash_list()
        mine = [t for t in trashed if t["project_id"] == binned_id]
        assert len(mine) == 1, [t["entry"] for t in mine]
        entry0 = mine[0]
        assert entry0["name"] == binned_name, entry0
        assert entry0["has_stl"] and entry0["size_bytes"] > 0, entry0
        assert entry0["deleted_at"].startswith("20"), entry0
        # 回收站的内容不能算「项目列表」（否则恢复前就会重复出现）
        assert binned_id not in [p.id for p in store.list()]

        restored = store.trash_restore(entry0["entry"])
        assert restored is not None, "恢复失败（界面只能告诉用户「点不动」）"
        assert restored.id == binned_id, "恢复后必须是同一个项目（否则历史/作业对不上）"
        assert (restored.dir / "model.stl").is_file(), "恢复后文件必须跟着回来"
        assert binned_id in [p.id for p in store.list()], "恢复后应重新出现在项目列表里"
        assert entry0["entry"] not in [t["entry"] for t in store.trash_list()], "恢复后条目还挂在回收站里"

        # 同名占用：新建一个同名的再恢复，不许覆盖已有项目
        clash = store.create(binned_name)
        assert store.delete(restored.id)
        again = [t for t in store.trash_list() if t["project_id"] == binned_id]
        assert again, "删两次之后找不到那个条目"
        moved = store.trash_restore(again[0]["entry"])
        assert moved is not None and moved.id == binned_id
        assert moved.dir != clash.dir, "恢复时覆盖掉了同名项目（用户会少一个项目）"
        assert store.get(clash.id) is not None, "原同名项目被恢复顶掉了"
        assert store.delete(moved.id)

        # 名字不能跑出 .deleted：分隔符/.. /绝对路径 / 空名全部当「找不到」
        outside = store.root.parent / "did-not-exist-before.txt"
        outside.write_text("keep", encoding="utf-8")
        try:
            for bad in ("..", "..\\..", "../..", str(store.root), "", "   ", ".", "a:b", "no-such-entry"):
                assert store.trash_purge(bad) is False, f"回收站接受了一个越界名字：{bad!r}"
                assert store.trash_restore(bad) is None, f"恢复接受了一个越界名字：{bad!r}"
            assert outside.is_file(), "越界尝试已经动到了 .deleted 以外的东西"

            # 上面那些全部只靠**字符串**就能拦住，所以它们验不到 resolve() 那一层。
            # 真正的形状是：条目名字完全合法，但它是指向外面的**联接**——
            # 不拦的话，「彻底删除一条回收站记录」就会顺着联接把外面那棵树删掉。
            far = store.root.parent / f"trash-link-target-{os.getpid()}"
            far.mkdir(parents=True, exist_ok=True)
            sentinel = far / "must-survive.txt"
            sentinel.write_text("keep", encoding="utf-8")
            link = store.trash_dir / f"联接条目__{binned.dir.name}"
            made = False
            try:
                if os.name == "nt":
                    # junction 不需要管理员权限（symbolic-link 要），也正是用户机器上会出现的那种
                    import subprocess as _sp

                    proc = _sp.run(
                        ["cmd", "/c", "mklink", "/J", str(link), str(far)],
                        capture_output=True,
                        timeout=60,
                    )
                    made = proc.returncode == 0 and link.exists()
                else:  # pragma: no cover - 非 Windows 走普通符号链接
                    os.symlink(far, link, target_is_directory=True)
                    made = link.is_symlink()
            except OSError:
                made = False
            link_note = "联接用例未跑（建不出联接）"
            if made:
                # 不能只断言「返回 False」：Windows 上 rmtree 自己就拒绝走进 junction，
                # 把 resolve() 防护删掉它也一样返回 False —— 那是一条验不出东西的断言
                # （实测：投毒后守卫仍然全绿）。真正能区分的是「条目有没有被动过」：
                # 没有防护时 trash_restore 会顺着名字把整个 junction **搬走**。
                assert store.trash_purge(link.name) is False, "指向外面的联接条目竟然能彻底删除"
                assert store.trash_restore(link.name) is None, "指向外面的联接条目竟然能恢复"
                assert link.exists() and sentinel.is_file(), (
                    "条目或链接目标被动过了：拒绝必须是「不碰它」，而不是「碰了但报错」"
                )
                assert link.name in [t["entry"] for t in store.trash_list()], (
                    "越界条目从回收站列表里消失了（等于被隐式处理掉了）"
                )
                link_note = "指向 .deleted 外面的联接条目：拒删、拒恢复，且条目原地不动"
            # 先拆链接本身（os.rmdir 对 junction/符号链接只删链接，不走进目标），再删靶子目录
            try:
                os.rmdir(link)
            except OSError:
                pass
            shutil.rmtree(far, ignore_errors=True)
        finally:
            outside.unlink(missing_ok=True)

        # 真 HTTP 往返：路由顺序 + 不可逆操作的显式确认
        from fastapi.testclient import TestClient

        from aiforge.app import create_app

        client = TestClient(create_app(), base_url="http://127.0.0.1:8760", raise_server_exceptions=False)
        got = client.get("/api/projects/trash")
        assert got.status_code == 200, f"{got.status_code} {got.text[:200]}"
        body = got.json()
        assert isinstance(body, dict) and "items" in body, (
            f"/api/projects/trash 被 `/api/projects/{{project_id}}` 吞掉了（路由注册顺序错）：{body}"
        )
        assert body["count"] == len(body["items"]) and body["items"], body
        victims = [t for t in body["items"] if t["project_id"] == binned_id]
        assert victims, f"接口列不出刚删的那个项目：{[t['entry'] for t in body['items']][:6]}"
        victim_entry = victims[0]["entry"]
        noreply = client.post(f"/api/projects/trash/{victim_entry}/purge", json={})
        assert noreply.status_code == 400, f"没给 confirm 就能彻底删除：{noreply.status_code}"
        still = client.get("/api/projects/trash").json()["count"]
        assert still == body["count"], "未确认的 purge 还是删掉了条目"
        ok_purge = client.post(f"/api/projects/trash/{victim_entry}/purge", json={"confirm": True})
        assert ok_purge.status_code == 200, ok_purge.text
        assert client.get("/api/projects/trash").json()["count"] == still - 1
        gone = client.post(f"/api/projects/trash/{victim_entry}/restore", json={})
        assert gone.status_code == 404, f"彻底删除后还能恢复：{gone.status_code}"
        assert store.delete(clash.id)
    finally:
        shutil.rmtree(project.dir, ignore_errors=True)
    return (
        f"创建/读写/历史/重命名/删除/回收站列出与恢复/彻底删需显式确认/越界名字与联接全拒"
        f"（{link_note}）/名字安全/往返不丢字段/无 id 项目可点开 全部正确（{store.root}）"
    )


def t_config_and_secrets() -> str:
    from aiforge.config import store

    cfg = store(refresh=True)

    # 掩码测试用「运行时拼接的假 Key」，绝不把任何真实形状的 Key 写进源码 ——
    # 否则源码包/仓库里会出现一个看起来像真 Key 的字符串（安全审计会直接判为泄露）。
    fake_key = "sk-" + ("0123456789abcdef" * 2)  # 32 位十六进制，与 DeepSeek Key 同形状
    assert cfg.mask_key(fake_key) == "sk-0123****cdef", cfg.mask_key(fake_key)
    assert cfg.mask_key(None) is None
    assert cfg.mask_key("short") == "sh****"

    cfg.update("api", {"model": "deepseek-chat"})
    cfg.reset_for_tests()
    again = store(refresh=True)
    assert again.config.api.model == "deepseek-chat", "配置必须能持久化"

    backend = again.secrets.backend
    assert backend in ("credential_manager", "config_file", "env"), backend

    # BOM 容错：用户用记事本「另存为 UTF-8」改过的 config.json 会带 BOM。
    # 早期写死 utf-8 读取，软件会把自己目录里一份**完好的配置**判成损坏、
    # 回退默认值、并把原文件改名成 .json.corrupt —— 用户看到的是
    # 「明明配好了，重启后设置全没了」。这条必须钉住。
    from aiforge.paths import paths as _paths

    files = _paths().ensure()
    files.config_file.write_text(
        '{"ui": {"theme": "dark"}}', encoding="utf-8-sig"
    )
    bom_store = store(refresh=True)
    assert bom_store.config.ui.theme == "dark", "带 BOM 的配置必须能正常读出来"
    corrupt = files.config_file.with_suffix(".json.corrupt")
    assert not corrupt.exists(), "带 BOM 的配置不该被当成损坏文件改名"
    # 还原成正常配置，避免影响后面的用例
    bom_store.update("ui", {"theme": "light"})

    return f"掩码正确，配置持久化正常，BOM 容错（密钥后端：{backend}）"


def t_appearance_settings() -> str:
    """界面材质（普通 / 液态玻璃）与玻璃透明度。

    为什么必须钉住这四件事：
      1. 夹取语义：上限是拿截图像素实测定的（`webui/scripts/verify-glass-contrast.mjs`），
         后端不夹取，用户就能从 API 写进一个让字读不动的值。
      2. 前后端常量一致：只改一边会出现「滑块能拖到 80，存盘回到 60」。
      3. 非法值必须「忽略并保持原值」：材质是观感字段，最坏也应该是不变，
         而不是把配置写坏之后重启什么都不剩。
      4. 浮层的厚玻璃下限：iOS 26 被骂「太透看不清字」就是弹窗这类浮层失守。
         它只活在 CSS 里，一次「统一简化玻璃令牌」的重构就能抹掉 —— 所以源码守卫
         比截图验收更便宜（verify-glass-contrast 要跑浏览器，不在自检里）。
    """
    import re

    from aiforge import config as config_mod
    from aiforge.config import clamp_glass_transparency as clamp

    default = config_mod.GLASS_TRANSPARENCY_DEFAULT
    upper = config_mod.GLASS_TRANSPARENCY_MAX

    # ---- 1) 夹取 ----
    assert clamp(None) == default, "无法解析的值回默认值，不是 0"
    assert clamp("abc") == default
    assert clamp({}) == default
    assert clamp(-10) == config_mod.GLASS_TRANSPARENCY_MIN
    assert clamp(999) == upper
    assert clamp("42") == 42, "字符串数字要能用（表单交上来就是字符串）"
    assert clamp(25.6) == 26, "浮点应当四舍五入而不是截断"
    assert isinstance(clamp(30), int)

    # ---- 2) 前后端同一组数 ----
    ts_src = (ROOT / "webui" / "src" / "hooks" / "useAppearance.ts").read_text(encoding="utf-8")
    pairs = dict(re.findall(r"export const TRANSPARENCY_(MIN|MAX|DEFAULT) = (\d+)", ts_src))
    assert set(pairs) == {"MIN", "MAX", "DEFAULT"}, f"前端常量读不到：{pairs}"
    assert int(pairs["MIN"]) == config_mod.GLASS_TRANSPARENCY_MIN, "前端 MIN 与后端不一致"
    assert int(pairs["MAX"]) == upper, "前端 MAX 与后端不一致：滑块能拖到后端不认的位置"
    assert int(pairs["DEFAULT"]) == default, "前端默认值与后端不一致"

    # ---- 3) 浮层厚玻璃下限还在 ----
    tokens = (ROOT / "webui" / "src" / "styles" / "tokens.css").read_text(encoding="utf-8")
    floor = re.search(r"--glass-solid-float:\s*max\(\s*(\d+)%", tokens)
    assert floor, (
        "浮层的实底下限被删了：弹窗/菜单会跟着滑块一起变透，"
        "实测透明度 60% 时弹窗正文只剩 1.84:1（要求 4.5:1）"
    )
    assert int(floor.group(1)) >= 90, (
        f"浮层下限被调到 {floor.group(1)}%：无下限对照里，实底 75%（透明度 25%）"
        "就已经只剩 4.14:1，踩不进 4.5:1 的安全区"
    )
    assert "--glass-solid-float: 100%" in tokens, "降低透明度（系统级）时浮层也必须变实底"
    app_css = (ROOT / "webui" / "src" / "styles" / "app.css").read_text(encoding="utf-8")
    float_blocks = [b for b in app_css.split("}") if "--glass-fill-float" in b]
    assert float_blocks, "没有任何浮层在用厚玻璃填充"
    for sel in (".modal", ".menu", ".toast"):
        assert any(sel in b for b in float_blocks), f"{sel} 没走厚玻璃（背后可都是密集文字）"

    # ---- 4) HTTP 往返 ----
    from fastapi.testclient import TestClient

    from aiforge.app import create_app

    client = TestClient(create_app(), base_url="http://127.0.0.1:8760", raise_server_exceptions=False)
    before = client.get("/api/settings").json()
    for field in ("appearance", "glass_transparency"):
        assert field in before, f"GET /api/settings 缺字段 {field}"
    try:
        saved = client.put("/api/settings", json={"appearance": "glass", "glass_transparency": 999})
        assert saved.status_code == 200, saved.text
        assert saved.json()["appearance"] == "glass"
        assert saved.json()["glass_transparency"] == upper, saved.json()
        assert client.get("/api/settings").json()["glass_transparency"] == upper, "必须真的落盘"

        ignored = client.put("/api/settings", json={"appearance": "crystal"})
        assert ignored.status_code == 200, ignored.text
        assert ignored.json()["appearance"] == "glass", "非法材质应被忽略而不是写坏配置"

        low = client.put("/api/settings", json={"glass_transparency": -5})
        assert low.json()["glass_transparency"] == config_mod.GLASS_TRANSPARENCY_MIN
        solid = client.put("/api/settings", json={"appearance": "solid"})
        assert solid.json()["appearance"] == "solid"
    finally:
        client.put(
            "/api/settings",
            json={
                "appearance": before["appearance"],
                "glass_transparency": before["glass_transparency"],
            },
        )
    return (
        f"夹取 0–{upper} 正确、前后端常量一致、浮层厚玻璃下限 {floor.group(1)}% 仍在、"
        "非法值忽略不写坏配置"
    )


def t_envcheck() -> str:
    import asyncio

    from aiforge import envcheck

    report = asyncio.run(envcheck.build_report(use_cache=False, live=False, deep=True))
    ids = {item["id"] for item in report["items"]}
    required = {
        "windows",
        "runtime",
        "python",
        "python_version",
        "openscad",
        "deps",
        "project_dir",
        "temp",
        "network",
        "api",
    }
    missing = required - ids
    assert not missing, f"环境报告缺少必需项：{missing}"
    assert report["total"] >= 10, report["total"]

    # ---- 「AI 服务」不能写死成 DeepSeek（本项目支持智谱 GLM 与混合模式）----
    # 实测过的坑：只配了智谱免费档的用户，在诊断页看到的是「尚未配置 DeepSeek API Key」
    # 与「去 platform.deepseek.com 创建」；而「网络连接」探的也是 api.deepseek.com ——
    # 用户真正依赖的那个地址一次也没测过，却可能因为 DeepSeek 可达而被报成「网络正常」。
    name_ds, probe_ds, signup_ds = envcheck.ai_provider_info("https://api.deepseek.com")
    name_glm, probe_glm, signup_glm = envcheck.ai_provider_info("https://open.bigmodel.cn/api/paas/v4")
    assert (name_ds, name_glm) == ("DeepSeek", "智谱 GLM"), (name_ds, name_glm)
    assert "deepseek" in probe_ds and "platform.deepseek.com" == signup_ds, (probe_ds, signup_ds)
    assert "bigmodel" in probe_glm and "deepseek" not in probe_glm, (probe_glm, "GLM 用户去探 DeepSeek")
    assert "deepseek" not in signup_glm, f"GLM 用户被指引去 DeepSeek 注册：{signup_glm}"
    name_any, probe_any, _ = envcheck.ai_provider_info("https://ai.example.com/v1")
    assert probe_any == "https://ai.example.com/v1", (
        f"自定义地址应探它自己，不然这一项在测一个与用户无关的站点：{probe_any}"
    )
    assert name_any == "AI 服务" and name_ds != name_any, "认不出的提供商也要有中性名字"

    # 标题/指引跟着配置走（只改内存里的值，最后还回去，不碰磁盘）
    from aiforge.config import store as config_store

    cfg = config_store()
    saved_base = cfg.config.api.base_url
    saved_econ = cfg.config.api.economy_base_url
    try:
        cfg.config.api.base_url = "https://open.bigmodel.cn/api/paas/v4"
        item_glm = envcheck.check_api()
        assert "智谱" in item_glm.title and "DeepSeek" not in item_glm.title, item_glm.title
        assert "deepseek" not in (item_glm.action or ""), item_glm.action
        # 混合模式：经济档是另一家时，网络那一项必须两家都探
        cfg.config.api.base_url = "https://api.deepseek.com"
        cfg.config.api.economy_base_url = "https://open.bigmodel.cn/api/paas/v4"
        targets = envcheck.ai_network_targets()
        hosts = " ".join(targets.values())
        assert "deepseek" in hosts and "bigmodel" in hosts, f"混合模式只探了一家：{targets}"
        # 同址不能算两个目标（否则一个故障会被计成「全失败」，假报断网）
        cfg.config.api.economy_base_url = "https://api.deepseek.com/"
        assert len(envcheck.ai_network_targets()) == 1, envcheck.ai_network_targets()
    finally:
        cfg.config.api.base_url = saved_base
        cfg.config.api.economy_base_url = saved_econ
    item_ds = envcheck.check_api()
    assert "DeepSeek" in item_ds.title, f"反向兜底：默认配置下标题不应变成别家（{item_ds.title}）"
    
    # ---- 「诊断探测的墙钟」必须跟着「实际读超时」算，不能自己写死一个数（P2-15）----
    # 实测过的坑：`check_api_live` 以前写死 `timeout: float = 95.0`（当时 `timeout_s=90`，
    # 95>90 对），后来 llm 给智谱加了 1.5 倍放大（变成 135），envcheck 这边的写死数字
    # 没跟着变 —— 好好的智谱 Key 在诊断页会被误报成「超时」，正是那段注释自己
    # 发誓要避免的「反向撤销」。现在墙钟由 `effective_read_timeout` 实时算，两边共用
    # 同一个公式；这条断言钉住「共用」这个事实：把公式改到另一处、或者两边重新
    # 各写一个写死的数，就会红。
    from aiforge.llm import API_PROBE_BASE_TIMEOUT_S, DeepSeekClient, effective_read_timeout
    
    glm_url = "https://open.bigmodel.cn/api/paas/v4"
    ds_url = "https://api.deepseek.com"
    glm_read = effective_read_timeout(API_PROBE_BASE_TIMEOUT_S, glm_url)
    ds_read = effective_read_timeout(API_PROBE_BASE_TIMEOUT_S, ds_url)
    # 正向：智谱确实被放大（不放大 = 公式叉回一处），而 DeepSeek 不受影响
    assert glm_read > ds_read, (glm_read, ds_read)
    assert ds_read == float(API_PROBE_BASE_TIMEOUT_S), ds_read
    # 反向（关键）：墙钟（在 check_api_live 里算的是 read+缓冲）必须严格大于读超时本身，
    # 而不是大于一个早就旧的写死数字；拿以前那个 95 当尺子，智谱用户就会被误报
    assert glm_read > 95.0, (
        f"墙钟基准（{glm_read}）必须大于历史上写死的 95，否则又回到那个「好好的 Key 被判超时」的坑"
    )
    # 墙钟的「+缓冲」那一步在 envcheck 里，不重复测；但得确认这个缓冲确实被用上了：
    # 拿一个能控制配置的假 Key 直接跑 `check_api_live` 不现实（会真发网络请求），
    # 所以这里验证的是「公式共用」这个结构事实：改 `GLM_TIMEOUT_BOOST` 必须同时
    # 影响 `effective_read_timeout` 与 `DeepSeekClient.__init__` 真实设下的超时。
    saved_boost = DeepSeekClient.GLM_TIMEOUT_BOOST
    try:
        DeepSeekClient.GLM_TIMEOUT_BOOST = 2.0
        client_glm = DeepSeekClient("k", glm_url, "glm-4.7-flash", timeout_s=API_PROBE_BASE_TIMEOUT_S)
        assert client_glm.timeout.read == effective_read_timeout(API_PROBE_BASE_TIMEOUT_S, glm_url), (
            "DeepSeekClient 自己的读超时与 effective_read_timeout 算出来的不一样 —— 公式分叉了"
        )
    finally:
        DeepSeekClient.GLM_TIMEOUT_BOOST = saved_boost

    return (
        f"{report['ok_count']}/{report['total']} 项通过（overall={report['overall']}）；"
        "AI 提供商名字/探测地址/注册指引按 base_url 走（DeepSeek / 智谱 / 自定义 / 混合模式）"
    )


def t_api_surface() -> str:
    """接口面不臃肿：路由 ↔ 契约文档 ↔ 前端调用 ↔ 错误码，四向都得对上。

    为什么非要钉到这一步（都是实测出来的洞）：
      * 加了接口不写文档 —— 契约静默腐烂，前端只能靠猜（切片软件那两个接口就这么漏了一次）；
      * 加了接口没人调 —— 纯负担：多一处能坏的地方、多一份本机攻击面，而测试全是绿的
        （做这次审计时发现曾有 8 条路由界面、mock、脚本都没人调）；
      * 后端发了一个新错误码不登记 —— 前端拿不到人话兜底，用户只看到「出了点问题」。
    只查路径不查方法（`request('/settings', {method:'PUT'})` 这种写法要拆 AST 才认得出方
    法与路径的对应关系）：够用 —— 它能抱住“整条路径没人用”，方法写错另外有契约形状用例接。
    """
    import re

    from aiforge.app import create_app

    doc = (ROOT / "docs" / "API-CONTRACT.md").read_text(encoding="utf-8")

    def norm(path: str) -> str:
        """`{project_id}` / `{id}` 归一化成 `{}`：文档与代码的参数名从来不一样。"""
        return re.sub(r"\{[^{}]+\}", "{}", path)

    routes: set[tuple[str, str]] = set()
    for route in create_app().routes:
        path = str(getattr(route, "path", ""))
        if not path.startswith("/api"):
            continue
        for method in sorted(getattr(route, "methods", set()) or set()):
            if method in ("HEAD", "OPTIONS"):
                continue
            routes.add((method, norm(path)))
    assert len(routes) >= 40, f"只枚举到 {len(routes)} 条 /api 路由，明显不对（守卫没生效？）"

    # 1) 每一条路由都必须在契约里出现（标题、正文、表格里的写法都算）
    documented = {
        (method, norm(path))
        for method, path in re.findall(
            r"(GET|POST|PUT|PATCH|DELETE)\s+(/api/[^\s`\u3000)\]，。;；,]+)", doc
        )
    }
    no_doc = sorted(routes - documented)
    assert not no_doc, "这些接口没写进 docs/API-CONTRACT.md：" + "、".join(
        f"{method} {path}" for method, path in no_doc
    )

    # 2) 每一条路由都必须真有人调（FastAPI 自带的自省页除外，界面本来就不该调它）
    introspection = {("GET", "/api/docs"), ("GET", "/api/openapi.json")}
    frontend_text = "\n".join(
        path.read_text(encoding="utf-8")
        for pattern in ("src/**/*.ts", "src/**/*.tsx")
        for path in (ROOT / "webui").glob(pattern)
    )
    assert len(frontend_text) > 200_000, "没读到足够的前端源码，这条检查等于没做"
    uncalled: list[str] = []
    for method, path in sorted(routes - introspection):
        needle = re.compile(norm(path).replace("/api", r"(?:/api)?").replace("{}", "[^/`\"]+"))
        if not needle.search(frontend_text):
            uncalled.append(f"{method} {path}")
    assert not uncalled, "这些接口前端没人调用（多做出来的接口就是臃肿：接上它或者删掉它）：" + "、".join(uncalled)

    # 3) 后端发出去的每一个错误码，都要同时出现在契约 §8 与前端 ApiErrorCode 里
    emitted: set[str] = set()
    for path in sorted((ROOT / "src" / "aiforge").rglob("*.py")):
        emitted.update(re.findall(r'code="([A-Z_]+)"', path.read_text(encoding="utf-8")))
    assert len(emitted) >= 6, f"只扫到 {len(emitted)} 个错误码，明显不对"
    in_table = set(re.findall(r"^\|\s*`([A-Z_]+)`\s*\|", doc, re.MULTILINE))
    types_text = (ROOT / "webui" / "src" / "api" / "types.ts").read_text(encoding="utf-8")
    # （最后一行带分号，所以 `;?` 不能漏 —— 漏了就会把最后一个码误报成“没登记”）
    in_types = set(re.findall(r"^\s*\|\s*'([A-Z_]+)'\s*;?\s*$", types_text, re.MULTILINE))
    drift = sorted(code for code in emitted if code not in in_table or code not in in_types)
    assert not drift, "这些错误码没登记到契约 §8 错误码表或前端 ApiErrorCode：" + "、".join(drift)

    return (
        f"{len(routes)} 条 /api 路由全部有文档、有调用方（自省页 2 条除外）；"
        f"{len(emitted)} 个错误码全部登记在契约与前端类型里"
    )


def t_http_api() -> str:
    try:
        from fastapi.testclient import TestClient
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 httpx/starlette 的 TestClient 支持") from exc

    from aiforge.app import create_app

    # 基址必须是回环地址：TestClient 默认用 "testserver"，会被本机来源保护正确拦截
    # raise_server_exceptions=False：让 500 也走我们自己的错误处理，验证错误结构真的可用
    app = create_app()
    client = TestClient(app, base_url="http://127.0.0.1:8760", raise_server_exceptions=False)

    health = client.get("/api/app/health")
    assert health.status_code == 200, health.text
    assert health.json()["ok"] is True

    version = client.get("/api/env/version")
    assert version.status_code == 200, version.text
    assert version.json()["app"], version.json()

    report = client.get("/api/env/report")
    assert report.status_code == 200, report.text
    assert report.json()["total"] >= 10

    diagnostics = client.get("/api/diagnostics")
    assert diagnostics.status_code == 200, diagnostics.text
    groups = diagnostics.json()["groups"]
    assert len(groups) >= 5, groups
    assert any(g["group"] == "AI 服务" for g in groups), [g["group"] for g in groups]

    detect = client.get("/api/openscad/detect")
    assert detect.status_code == 200, detect.text
    openscad_note = ""
    if detect.json()["found"]:
        assert detect.json()["found"] is True, detect.json()

        cli = client.post("/api/openscad/test")
        assert cli.status_code == 200, cli.text
        assert cli.json()["ok"] is True, cli.json()
    else:
        # 没装 OpenSCAD 的机器（新 clone / CI）上这两条验不了：路由本身已经挨到了（200 +
        # 带 `found` 字段的完整形状）；拿「这台机器没装引擎」当产品缺陷报，
        # 等于逼 CI 去装一个 21MB 的二进制。装了引擎的机器照旧硬断言，覆盖没有变小。
        openscad_note = "（本机无 OpenSCAD：只验接口 200 与字段形状，不验 found/cli_ok）"

    settings = client.get("/api/settings")
    assert settings.status_code == 200, settings.text
    assert "data_dir" in settings.json()

    # 生成流程的两个开关必须能被界面读到、也能被写回：
    # 「生成前先确认需求理解」如果不通，用户就只能被关在确认卡片外面
    sdata = settings.json()
    assert "confirm_spec" in sdata and "announce_in_chat" in sdata, sorted(sdata)
    flipped = client.put("/api/settings", json={"confirm_spec": False})
    assert flipped.status_code == 200, flipped.text
    assert flipped.json()["confirm_spec"] is False, flipped.json().get("confirm_spec")
    restored = client.put("/api/settings", json={"confirm_spec": True})
    assert restored.json()["confirm_spec"] is True, restored.json().get("confirm_spec")

    # 恶意来源必须被拒绝（本机接口不能被子网或其他网页调用）。
    # TestClient 无法按请求改 Host，因此单独建一个「外部地址」的客户端来验证。
    external = TestClient(
        create_app(), base_url="http://192.168.1.50:8760", raise_server_exceptions=False
    )
    blocked = external.get("/api/app/health")
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["code"] == "INTERNAL", blocked.json()

    blocked_origin = client.get("/api/app/health", headers={"Origin": "https://evil.example.com"})
    assert blocked_origin.status_code == 403, blocked_origin.text

    projects = client.get("/api/projects")
    assert projects.status_code == 200, projects.text

    # 没有 API Key 时创建作业必须快速失败并给出 NO_API_KEY
    created = client.post("/api/jobs", json={"prompt": "设计一个手机支架"})
    assert created.status_code in (202, 409), created.text
    if created.status_code == 409:
        assert created.json()["code"] == "NO_API_KEY", created.json()

    missing = client.get("/api/jobs/does-not-exist")
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"], missing.json()

    # 未处理异常必须「两句都真」：给用户的 500 是人话，**并且**日志里真的有 traceback。
    # 为什么要单独钉：界面收到的那句 detail 写着「已记录到日志」，而它完全依赖
    # `log.exception()` 从**当前异常上下文**里取 traceback（Starlette 的 ServerErrorMiddleware
    # 是在 `except` 块里 await 这个处理器的）。把那一行换成 `log.error()` 一样返回 500、
    # 一样不弄坏其它任何检查，日志里却只剩一行没头没尾的中文 —— 用户在「系统诊断」
    # 里捕不到堆栈，我们也捕不到，而软件对外说的是「已记录」。
    # 让它真崩必须动**处理函数运行时调的那个属性**（下面注里说过的同一个坑）。
    import logging

    from aiforge import app as app_module
    from aiforge.api import projects_routes

    logged: list[tuple[str, bool]] = []

    class _ErrorSink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.ERROR:
                logged.append((record.getMessage(), record.exc_info is not None))

    sink = _ErrorSink(level=logging.ERROR)
    app_logger = logging.getLogger(app_module.log.name)
    app_logger.addHandler(sink)
    orig_store = projects_routes.projects_mod.store

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("自检注入的未处理异常")

    projects_routes.projects_mod.store = _boom  # type: ignore[assignment]
    try:
        crashed = client.get("/api/projects")
    finally:
        projects_routes.projects_mod.store = orig_store  # type: ignore[assignment]
        app_logger.removeHandler(sink)
    assert crashed.status_code == 500, crashed.text
    body500 = crashed.json()
    assert body500.get("code") == "INTERNAL" and body500.get("detail"), body500
    assert any("未处理的异常" in msg and with_exc for msg, with_exc in logged), (
        f"500 是给出去了，但日志里没有「带异常上下文」的那一行（实测捕到 {logged}）："
        "那句「已记录到日志」就成了假承诺"
    )

    # 把每一条「不带参数的 GET」都真跑一遍。为什么要推：界面只在 mock 上跑验收，
    # 真后端的接口挂了它在浏览器里看不出来（「一键安装」就是靠这个盲区把 500 发到了
    # 用户手上）。这里不猜哪些接口重要，逐条跑；5xx 统一算挂。
    # （投毒实测：要改**处理函数运行时调的那个属性**才能让它变红 ——
    #   改 router 上的 endpoint 函数没用，FastAPI 在写装饰器时就把函数绑走了。）
    sweep: list[str] = []
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        path = getattr(route, "path", "")
        if "GET" in methods and path.startswith("/api") and "{" not in path:
            sweep.append(path)
    assert len(sweep) >= 12, f"只枚举到 {len(sweep)} 条 GET，这条遍历等于没跑"
    broken: list[str] = []
    for path in sorted(set(sweep)):
        got = client.get(path)
        if got.status_code >= 500:
            broken.append(f"{path} -> {got.status_code} {got.text[:120]}")
    assert not broken, "有 GET 接口直接 500：" + "；".join(broken)

    return (
        f"health/version/report/diagnostics/openscad/settings/安全拦截 全部通过；"
        f"{len(set(sweep))} 条无参数 GET 逐个跑过无 5xx；"
        "未处理异常的 500 与人话同步校验（日志里确实带上了 traceback）"
        + openscad_note
    )


def t_llm_offline() -> str:
    """不联网验证提示词与响应解析。"""
    from aiforge import llm

    prompt = llm.create_user_prompt("设计一个手机支架")
    assert "手机支架" in prompt
    modify = llm.modify_user_prompt("cube([1,1,1]);", "高度改成 150mm")
    assert "150" in modify and "cube" in modify
    repair = llm.repair_user_prompt("cube([1,1,1)", "ERROR: syntax error")
    assert "syntax error" in repair

    parsed = llm.parse_review_json(
        '{"score": 88, "verdict": "pass", "summary": "可以打印", '
        '"issues": [{"severity": "info", "title": "提示", "detail": "d", "suggestion": "s"}]}'
    )
    assert parsed["score"] == 88 and parsed["verdict"] == "pass", parsed

    fenced = llm.parse_review_json('```json\n{"score": 50, "verdict": "fail"}\n```')
    assert fenced["score"] == 50 and fenced["verdict"] == "fail", fenced

    broken = llm.parse_review_json("这不是 JSON")
    # 以前这里断言 `broken["score"] == 70 and broken["verdict"] == "warn"` —— 把「模型没给出
    # 可用结果」与「模型真给出了一个中性评分」当成同一件事，而这个假 70 分会直接
    # 渲染成用户看到的「打印评分」——本轮修的就是这个（跟 parse_snapshot_verdict 的三态修复同形状）。
    # 现在「解析不了」必须是 None，不能拿中性值充数。
    assert broken is None, broken
    # 分数不是数字 / 缺 score 字段：同样不能退回默认 70
    assert llm.parse_review_json('{"score": "挺高的", "verdict": "warn"}') is None
    assert llm.parse_review_json('{"verdict": "pass"}') is None, "缺 score 字段也不能拿默认分充数"
    # 反例：合法但档位单词写错时，仍按模型自己给的分数反推档位（这不是编造，分数是真实的）
    derived = llm.parse_review_json('{"score": 92}')
    assert derived is not None and derived["verdict"] == "pass", derived
    derived2 = llm.parse_review_json('{"score": 30, "verdict": "完美无缺"}')
    assert derived2 is not None and derived2["verdict"] == "fail", derived2

    sysmsg = llm.design_system_prompt()
    assert "@param" in sysmsg and "禁止" in sysmsg or "严禁" in sysmsg
    return "提示词构造与 JSON 解析（含异常兜底）正确"


def t_sse_contract() -> str:
    """作业事件流必须遵守 docs/API-CONTRACT.md 3.3 的载荷约定。

    这类「形状漂移」不会抛异常，只会让前端悄悄退化到兜底分支（例如丢掉 messages），
    因此必须用断言钉住。
    """
    import asyncio
    import inspect

    from aiforge import jobs
    from aiforge.api import jobs_routes

    job = jobs.create_job("p1", 1, "create", prompt="测试")
    queue = job.subscribe()
    try:
        event, payload = queue.get_nowait()
        assert event == "snapshot", event
        assert payload["job_id"] == job.job_id
        assert payload["status"] == "running"
        # 首帧必须是完整 Job 结构
        for field in ("stage", "percent", "messages", "error", "result", "created_at", "finished_at"):
            assert field in payload, f"snapshot 缺少字段：{field}"
        assert isinstance(payload["messages"], list)
        assert isinstance(payload["created_at"], str), "created_at 必须是 ISO8601 字符串"
    finally:
        job.unsubscribe(queue)

    job.say("测试消息")
    job.set_stage("compiling")
    assert job.percent is not None

    # 等待确认规格：SSE 必须推 spec 事件，且快照里也带上它（刷新页面后卡片能重建）
    from aiforge import design as design_mod

    spec_job = jobs.create_job("p-spec", 0, "create", prompt="设计一个支架")
    spec_queue = spec_job.subscribe()
    try:
        spec_queue.get_nowait()  # 丢掉首帧 snapshot
        spec_obj = design_mod.parse_spec_response(
            '{"kind":"支架","specs":[{"name":"height","label":"高度","value":100,'
            '"user_specified":true}],"open_questions":["壁厚没指定"]}'
        )
        jobs.publish_spec(spec_job, spec_obj)
        spec_events = []
        while not spec_queue.empty():
            spec_events.append(spec_queue.get_nowait())
        spec_payload = next((p for e, p in spec_events if e == "spec"), None)
        assert spec_payload is not None, spec_events
        assert spec_payload["awaiting_confirmation"] is True, spec_payload
        assert spec_payload["spec"]["specs"][0]["name"] == "height", spec_payload
        assert spec_payload["spec"]["open_questions"] == ["壁厚没指定"], spec_payload
        # 关键：快照必须一并带上，否则页面刷新后确认卡片消失、作业变成死等
        snap = spec_job.snapshot()
        assert isinstance(snap.get("spec"), dict), snap.get("spec")
        assert snap["spec"]["awaiting_confirmation"] is True
        # 确认之后推的版本必须标成「不再等待确认」
        jobs.publish_spec(spec_job, spec_obj, awaiting=False)
        assert spec_job.snapshot()["spec"]["awaiting_confirmation"] is False

        # 作业流在超时分支上必须把「等待确认」当活着：既不能误判为结束推 error，
        # 也不能只发心跳 —— 前端刷新后就指望这份快照里的 spec。
        source = inspect.getsource(jobs_routes.job_stream)
        assert 'job.status == "awaiting_confirmation"' in source, source
        assert '_sse("snapshot", job.snapshot())' in source, "等待确认必须补发完整快照"
        assert '_sse("done", job.result or {})' not in source, "done 必须推完整快照，不能只推 result"
    finally:
        spec_job.unsubscribe(spec_queue)

    # done 事件必须推完整快照，而不是只有 JobResult
    done_queue = job.subscribe()
    job.result = {"project_id": "p1", "revision": 1, "name": "n", "scad": "cube([1,1,1]);"}
    job.succeed()
    done_events = []
    while not done_queue.empty():
        done_events.append(done_queue.get_nowait())
    done_payload = next((p for e, p in done_events if e == "done"), None)
    assert done_payload is not None, done_events
    assert done_payload["status"] == "done", done_payload.get("status")
    assert isinstance(done_payload.get("messages"), list), "done 快照必须带 messages"
    assert done_payload["finished_at"], "done 快照必须带 finished_at"
    assert done_payload["result"]["scad"].startswith("cube"), done_payload["result"]

    # cancelled 同样推完整快照
    job2 = jobs.create_job("p2", 1, "create", prompt="测试2")
    cancel_queue = job2.subscribe()
    job2.cancel()
    cancelled = [p for e, p in list(cancel_queue._queue) if e == "cancelled"]  # noqa: SLF001
    assert cancelled, "取消必须推 cancelled 事件"
    assert cancelled[0].get("status") == "cancelled", cancelled[0]

    # 前端必须认识后端会推的**每一个**终止事件。
    #
    # 这条断言来自一次真实故障：后端推了 `cancelled`，而前端 switch 里只有
    # message/stage/spec/done/error —— 那帧被 default 静默丢掉，作业状态永远
    # 停在 running，于是「发送键永久禁用」，用户点完停止就再也发不出消息，
    # 只能刷新页面。写代码时看不出来，跑起来就是砖头。
    webui_dir = ROOT / "webui" / "src"
    stream_ts = webui_dir / "hooks" / "useJobStream.ts"
    if stream_ts.is_file():
        hook_src = stream_ts.read_text(encoding="utf-8")
        for event in ("message", "stage", "spec", "done", "cancelled", "error"):
            assert f"case '{event}'" in hook_src, (
                f"后端会推 {event} 事件，但前端 useJobStream 没有处理分支 —— "
                "该帧会被静默丢弃，界面可能永久停在「生成中」"
            )

    # 失败路径
    job2.fail("出错了", hint="建议", code="INTERNAL")
    assert job2.status == "error", job2.status
    assert job2.error["code"] == "INTERNAL"

    # 参数元信息构建
    meta = jobs.build_param_meta(
        "// @param height 100 高度 40 300\nheight = 100;\nwidth = 60;\n",
        {"height": 150, "width": 60},
    )
    by_name = {m["name"]: m for m in meta}
    assert by_name["height"]["label"] == "高度", by_name
    assert by_name["height"]["value"] == 150, by_name
    assert by_name["height"]["min"] == 40 and by_name["height"]["max"] == 300, by_name
    assert by_name["width"]["value"] == 60
    return "快照补发 / done 完整快照 / cancelled / 状态机 / 参数元信息 全部符合契约"


def t_contract_shapes() -> str:
    """核对 HTTP 层的响应形状与契约一致（前端会直接依赖这些字段）。"""
    from fastapi.testclient import TestClient

    from aiforge.app import create_app

    client = TestClient(create_app(), base_url="http://127.0.0.1:8760", raise_server_exceptions=False)

    # 未配置 Key 时 api-key/test 必须是 400 + NO_API_KEY（前置条件不满足，不是测试失败）
    from aiforge.config import store

    saved = store().api_key
    # 环境变量也是 Key 的合法来源（应用支持它作为兜底，另有测试覆盖）。
    # 所以「没有 Key」必须把**两处**都按住：只清存储、不清环境变量的话，
    # 在导出过 DEEPSEEK_API_KEY 的终端里跑自检，这条会真的去联网并返回 200，
    # 于是断言失败 —— 实测踩到过，表现为「自检随机红一次」。
    env_key = os.environ.pop("DEEPSEEK_API_KEY", None)
    store().set_api_key("")
    try:
        response = client.post("/api/settings/api-key/test", json={})
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "NO_API_KEY", response.json()
        assert response.json().get("hint"), "必须给出修复提示"

        # 没有 Key 时创建作业同样快速失败
        job_response = client.post("/api/jobs", json={"prompt": "设计一个支架"})
        assert job_response.status_code == 409, job_response.text
        assert job_response.json()["code"] == "NO_API_KEY", job_response.json()
    finally:
        if saved:
            store().set_api_key(saved)
        if env_key is not None:
            os.environ["DEEPSEEK_API_KEY"] = env_key

    # 项目详情的字段形状
    created = client.post("/api/projects", json={"name": "契约测试"})
    assert created.status_code == 200, created.text
    summary = created.json()
    for field in ("id", "name", "created_at", "updated_at", "revision", "thumbnail_url", "stl_url"):
        assert field in summary, f"ProjectSummary 缺少字段：{field}"

    detail = client.get(f"/api/projects/{summary['id']}").json()
    for field in ("scad", "params", "history", "files"):
        assert field in detail, f"ProjectDetail 缺少字段：{field}"
    assert set(detail["files"]) >= {"scad_path", "stl_path", "export_dir"}, detail["files"]

    settings = client.get("/api/settings").json()
    for field in ("api_key_set", "api_key_masked", "api_key_store", "data_dir", "projects_dir", "theme"):
        assert field in settings, f"Settings 缺少字段：{field}"
    assert "api_key" not in settings, "Settings 绝不能回传完整 Key"

    # UI 相关契约只在「界面已构建」时才成立。
    #
    # 为什么必须分支：`webui/dist` 是构建产物、被 .gitignore 排除，
    # 所以**刚 clone 下来**的仓库里没有它。此时后端会明确返回
    # 「界面资源缺失，无法显示操作界面」(503) 并提示 `npm run build`，
    # 而 `/favicon.ico` 这条路由根本不会注册（404）。
    # 旧断言无条件要求 favicon 返回 204 —— 于是新贡献者做的第一件事
    # （clone → 跑自检）会红，而且报错只有一句 `AssertionError: 404`，
    # 极难查到原因。现在两条分支都断言**真实契约**，只是契约不同：
    #   已构建 → favicon 真的空 204 + `/` 返回入口页
    #   未构建 → `/` 返回 503 且提示里必须给出「怎么修」
    ui_built = (ROOT / "webui" / "dist" / "index.html").is_file()
    if ui_built:
        # favicon 必须返回「真的空 204」。曾经用 JSONResponse(204, content=None)，
        # 那会写出 4 字节 "null" 实体，而 204 不允许有实体 —— 服务端会抛
        # LocalProtocolError 并切断 keep-alive 连接，导致紧随其后的 API 调用失败
        # （浏览器每次加载页面都请求 favicon，用户会看到「无法连接到本地服务」）。
        favicon = client.get("/favicon.ico")
        assert favicon.status_code == 204, favicon.status_code
        assert favicon.content in (b"", None), f"204 不允许有响应体，实际 {favicon.content!r}"
        assert client.get("/").status_code == 200, "/ 应当返回入口页"
        ui_note = "界面已构建：favicon 空 204、首页 200"
    else:
        home = client.get("/")
        assert home.status_code == 503, (
            f"界面未构建时首页应当返回 503 并说明原因，实际 {home.status_code}"
        )
        body = home.json()
        assert "界面资源缺失" in (body.get("detail") or ""), body
        assert "npm run build" in (body.get("hint") or ""), body
        ui_note = "界面未构建：首页 503 且提示 npm run build（clone 后属正常）"

    # 打印机的「覆盖」语义必须可被前端区分：内置机型被用户改过后仍 builtin=True，
    # 前端只能靠 overridden 判断该显示「恢复默认」还是「删除」。
    listing2 = client.get("/api/printers").json()
    builtin_entry = next(p for p in listing2["printers"] if p["id"] == "prusa-mini-04")
    assert builtin_entry.get("overridden") is False, builtin_entry.get("overridden")
    client.put("/api/printers/prusa-mini-04", json={"build_z": 190})
    overridden_entry = next(p for p in client.get("/api/printers").json()["printers"] if p["id"] == "prusa-mini-04")
    assert overridden_entry.get("overridden") is True, "改过的内置机型必须标记 overridden"
    assert overridden_entry["builtin"] is True, "覆盖版本仍然是内置机型"
    client.post("/api/printers/reset")

    # preview 必须支持光固化机型（nozzle_mm = 0）
    resin_preview = client.post(
        "/api/printers/preview",
        json={"nozzle_mm": 0, "build_x": 130, "build_y": 80, "build_z": 160, "extruder": "resin", "material": "树脂"},
    )
    assert resin_preview.status_code == 200, resin_preview.text
    assert resin_preview.json()["constraints"]["min_wall_mm"] < 0.5, resin_preview.json()

    # 下载文件名头：中文项目名不能让 ASCII 兜底退化成「.stl」
    # 实测踩到的真实 Bug：`filename.encode("ascii","ignore")` 对中文名得到的是
    # **扩展名本身**（"桌面手机支架.stl" → ".stl"），它非空，于是
    # `... or "model"` 这个兜底永远不触发 —— 只认 filename= 的客户端会存下
    # 一个名叫 .stl 的隐藏文件，用户以为下载失败。5 个用例里错了 4 个。
    from aiforge.api.projects_routes import attachment_header

    def ascii_part(header: str) -> str:
        return header.split('filename="')[1].split('"')[0]

    for name, expect in (
        ("桌面手机支架.stl", "model.stl"),
        ("水杯架.stl", "model.stl"),
        ("我的模型 v2.scad", "v2.scad"),
        ("3D 零件.stl", "3D.stl"),
        ("model.stl", "model.stl"),
    ):
        got = ascii_part(attachment_header(name))
        assert got == expect, f"{name!r} 的 ASCII 兜底名应为 {expect!r}，实际 {got!r}"
        assert got not in (".stl", ".scad"), f"{name!r} 的兜底名没有主名：{got!r}"
    # 头部结构不能被文件名里的引号/换行破坏（防御性：调用方已经清过一次）
    for evil in ('a"b.stl', "a\r\nX-Injected: 1.stl"):
        header = attachment_header(evil)
        assert "\r" not in header and "\n" not in header, repr(header)
        assert header.count('"') == 2, f"文件名里的引号破坏了头结构：{header!r}"

    client.delete(f"/api/projects/{summary['id']}")
    return (
        "200/400/409 语义、Project/ Settings 字段形状、打印机覆盖语义、下载文件名头 "
        f"全部符合契约（{ui_note}）"
    )


def t_busy_guard() -> str:
    """同一个项目上并发创建作业，必须被 409 BUSY 挡住。

    为什么必须钉住这一条：两个作业会对**同一个项目目录**做
    「归档上一版 → 写 model.scad → 存 meta」，而它们读到的 `project.revision`
    是同一个值 —— 于是 revision 相同、互相覆盖，其中一个模型被静默丢弃，
    多出来的那次如果调用 AI 还会真的花掉用户额度。
    前端也做了乐观锁（按下发送键立刻禁用），但前端只防手滑：
    防不住「响应还没回来就连点两次」、直接调接口、或开着第二个窗口。

    这条测试**完全不联网**：直接造一个停在 running 的作业占住项目，
    然后看三个入口（参数微调 / 直接编译 / 自然语言发送）是否都拒绝，
    以及作业结束后是否**重新放行**（守卫必须能解开，否则一次卡死就把项目锁死了）。
    """
    from fastapi.testclient import TestClient

    from aiforge import jobs as jobs_mod
    from aiforge import projects as projects_mod
    from aiforge.api.jobs_routes import reject_if_busy
    from aiforge.app import create_app
    from aiforge.config import store as config_store

    client = TestClient(create_app(), base_url="http://127.0.0.1:8760", raise_server_exceptions=False)
    pstore = projects_mod.store()
    cfg = config_store()

    project = pstore.create("并发守卫测试")
    projects_mod.atomic_write_text(
        project.scad_path,
        "// @param height 50 高度 10 200\nheight = 50;\ncube([20,20,height]);\n",
    )
    pstore.save(project)

    holding = jobs_mod.create_job(project_id=project.id, revision=project.revision + 1, mode="params")
    assert holding.status == "running", holding.status
    try:
        assert jobs_mod.active_job_for(project.id) is holding, "守卫必须能看见这个 running 作业"

        # 1) 参数微调（这条路由不看 API Key，最能干净地验守卫本身）
        blocked = client.post(f"/api/projects/{project.id}/params", json={"params": {"height": 80}})
        assert blocked.status_code == 409, blocked.text
        body = blocked.json()
        assert body["code"] == "BUSY", body
        assert body.get("hint"), "被挡住时必须告诉用户怎么解开"

        # 2) 直接编译同一个项目
        blocked = client.post(
            "/api/compile", json={"scad": "cube([10,10,10]);", "project_id": project.id}
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["code"] == "BUSY", blocked.json()

        # 3) 自然语言发送。这条路由**先查 API Key**（没有就直接 NO_API_KEY），
        #    所以要在这一步临时让它认为「有 Key」。
        #    刻意用替换类属性的方式而不是 set_api_key()：
        #    自检脚本绝不该去写/删用户 Windows 凭据管理器里的真实密钥。
        real_api_key = type(cfg).api_key
        type(cfg).api_key = property(lambda self: "sk-selfcheck-not-a-real-key")  # type: ignore[assignment]
        try:
            blocked = client.post(
                "/api/jobs",
                json={
                    "prompt": "高度改成 120mm",
                    "project_id": project.id,
                    "instruction": "高度改成 120mm",
                    "mode": "modify",
                },
            )
            assert blocked.status_code == 409, blocked.text
            assert blocked.json()["code"] == "BUSY", blocked.json()
        finally:
            type(cfg).api_key = real_api_key

        # 停在确认卡片上、而且界面**正在监听** → 仍然拒绝：
        # 用户人就在卡片前面，该做的是确认或取消，而不是再发一条
        # （否则他确认回来的那一版会被新作业覆盖）。
        holding.status = "awaiting_confirmation"
        assert holding.waiting_for_spec
        watcher = holding.subscribe()
        assert holding.has_watcher, "subscribe() 之后必须能看到监听者"
        blocked = client.post(f"/api/projects/{project.id}/params", json={"params": {"height": 90}})
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["code"] == "BUSY", blocked.json()

        # 停在确认卡片上、**没有任何界面在监听** → 必须退役并放行。
        #
        # 这是实测死锁的修复：生成 → 弹出规格卡片 → 按 F5（SSE 断开、订阅归零）
        # → 再发一条 → 旧的守卫会回 409「请先点停止」，可卡片没了、停止按钮也没了，
        # 那个项目就永远发不出东西，只能重启软件。
        holding.unsubscribe(watcher)
        watcher = None
        assert not holding.has_watcher
        reject_if_busy(project.id)  # 不抛异常 = 放行
        assert holding.status == "cancelled", f"无人监听的等待作业必须被真正取消，实际 {holding.status}"
        assert jobs_mod.active_job_for(project.id) is None
        # 这里刻意只验守卫函数本身、不真的 POST：那会启动一个真实生成作业
        # （要 OpenSCAD、可能还要联网复核），自检必须离线且确定性。

        # 关键：作业结束后必须重新放行。
        holding.status = "done"
        assert jobs_mod.active_job_for(project.id) is None
        reject_if_busy(project.id)  # 不抛异常即通过
    finally:
        if watcher is not None:
            holding.unsubscribe(watcher)
        holding.status = "done"
        pstore.delete(project.id)

    return (
        "并发守卫：运行中的作业挡住三个入口；等待确认时有人监听则挡住、"
        "没人监听（刷新/切走）则退役放行；结束后重新放行"
    )


def t_printer_knowledge() -> str:
    """打印机知识库的结构完整性与推导逻辑。"""
    from aiforge import printers

    printers.reset_for_tests()
    reg = printers.registry(refresh=True)

    assert len(printers.BUILTIN_PRINTERS) >= 10, len(printers.BUILTIN_PRINTERS)
    ids = [p.id for p in printers.BUILTIN_PRINTERS]
    assert len(ids) == len(set(ids)), "内置机型 id 不能重复"
    for profile in printers.BUILTIN_PRINTERS:
        assert profile.name, profile.id
        assert profile.build_x > 0 and profile.build_y > 0 and profile.build_z > 0, profile.id
        assert profile.nozzle_mm >= 0, profile.id
        assert profile.materials, f"{profile.id} 必须声明可用材料"
        assert profile.layer_min_mm <= profile.layer_max_mm, profile.id
        assert 10 <= profile.recommended_max_overhang_deg <= 89, profile.id
        assert profile.dimensional_tolerance_mm > 0, profile.id

    # 每台机器都要能推导出完整、合理的工艺约束
    for profile in printers.BUILTIN_PRINTERS:
        constraints = printers.resolve_process(profile)
        assert constraints.min_wall_mm > 0, profile.id
        assert constraints.min_hole_dia_mm > 0, profile.id
        assert constraints.max_layer_mm >= profile.layer_min_mm, profile.id
        assert constraints.default_layer_mm <= constraints.max_layer_mm, profile.id
        assert printers.FN_MIN <= constraints.fn <= printers.FN_MAX, (profile.id, constraints.fn)

    # 核心规律：喷嘴越粗，最小可靠壁厚越大
    base = reg.get("generic-220-04")
    assert base is not None
    walls = {}
    layers = {}
    for nozzle in (0.2, 0.4, 0.6, 0.8):
        variant = printers.PrinterProfile.from_dict({**base.to_dict(), "nozzle_mm": nozzle})
        resolved = printers.resolve_process(variant, "PLA")
        walls[nozzle] = resolved.min_wall_mm
        layers[nozzle] = resolved.default_layer_mm
        assert resolved.default_layer_mm <= resolved.max_layer_mm, (nozzle, resolved.default_layer_mm)
        assert resolved.default_layer_mm >= variant.layer_min_mm, (nozzle, resolved.default_layer_mm)
    assert walls[0.2] < walls[0.4] < walls[0.6] < walls[0.8], walls
    assert abs(walls[0.4] - 1.2) < 0.01, walls
    # 推荐层高也必须随喷嘴变化：0.4 喷嘴约 0.2mm，0.8 喷嘴应当明显更厚
    # （写死 0.2mm 会让大喷嘴白白浪费产能，是早期版本的实际缺陷）
    assert abs(layers[0.4] - 0.2) < 0.001, layers
    assert layers[0.8] > layers[0.4] * 1.5, layers

    # 光固化机型必须跳过喷嘴规则
    resin = reg.get("resin-generic-405")
    assert resin is not None
    fdm_overhang_deg = printers.resolve_process(base, "PLA").max_overhang_deg
    resin_constraints = printers.resolve_process(resin)
    assert resin_constraints.is_resin if hasattr(resin_constraints, "is_resin") else True
    assert resin_constraints.min_wall_mm < 0.5, resin_constraints.min_wall_mm
    # 光固化的悬垂门槛必须**比 FDM 更严**，不能更松。
    # 这里以前断言的是「光固化几乎不受悬垂限制（≥70°）」——那个信念是错的，
    # 而且它会作为硬约束注入 AI 提示词，让 AI 放心做出大悬垂、长跨度不加支撑，
    # 打出来就是拉丝、塌陷、脱层。逐层固化时悬垂处没有可依附的上一层，
    # 所以光固化比 FDM 更依赖支撑：DfAM 工艺限值表给的是 SLA 30° vs FDM 45°。
    assert resin_constraints.max_overhang_deg <= fdm_overhang_deg, (
        f"光固化悬垂门槛 {resin_constraints.max_overhang_deg}° 不应比 FDM "
        f"（{fdm_overhang_deg}°）更宽松"
    )
    assert resin_constraints.max_overhang_deg <= 35, (
        f"光固化自支撑角应在 30° 附近，实际 {resin_constraints.max_overhang_deg}°"
    )
    assert resin_constraints.max_horizontal_span_mm <= 10, (
        f"光固化无支撑桥接应远小于 FDM，实际 {resin_constraints.max_horizontal_span_mm}mm"
    )

    # 材料与腔体的匹配提示：ABS 在开放机身上必须给出警告
    open_machine = printers.resolve_process(base, "ABS")
    assert any("翘边" in note or "封闭" in note for note in open_machine.notes), open_machine.notes
    enclosed = printers.resolve_process(reg.get("bambu-x1c-04"), "ABS")
    assert not any("开放式结构" in note for note in enclosed.notes), enclosed.notes

    # 材料本身也要影响结论：热变形温度高的材料在开放机身上才需要告警，
    # PLA 在任何机器上都不该被警告（否则就是无差别刷提示）
    pla_open = printers.resolve_process(base, "PLA")
    assert not any("热变形温度" in note for note in pla_open.notes), pla_open.notes
    assert any("热变形温度" in note for note in open_machine.notes), open_machine.notes
    # 材料密度按材料取值，用于耗材克数估算
    assert abs(printers.material_density("PLA") - 1.24) < 0.001
    assert abs(printers.material_density("ABS") - 1.04) < 0.001
    assert printers.material_density("未知材料") == 1.24, "未知材料应回退到 PLA 密度"

    # $fn 推导：喷嘴越细分段越多；大模型分段更多；都在范围内
    small_nozzle = printers.PrinterProfile.from_dict({**base.to_dict(), "nozzle_mm": 0.2})
    coarse_nozzle = printers.PrinterProfile.from_dict({**base.to_dict(), "nozzle_mm": 0.8})
    assert printers.resolve_fn(small_nozzle, 150) > printers.resolve_fn(coarse_nozzle, 150)
    assert printers.resolve_fn(base, 250) > printers.resolve_fn(base, 30)
    assert printers.resolve_fn(base, 150) >= 40, printers.resolve_fn(base, 150)
    return (
        f"{len(printers.BUILTIN_PRINTERS)} 台内置机型结构完整；"
        f"壁厚随喷嘴 {walls[0.2]}→{walls[0.8]}mm；树脂机跳过喷嘴规则；"
        f"ABS+开放机身有告警；$fn 推导合理"
    )


def t_printer_custom() -> str:
    """用户自定义打印机的增删改与持久化。"""
    import json

    from aiforge import printers

    printers.reset_for_tests()
    reg = printers.registry(refresh=True)
    original_selected = reg.selected().id

    created = reg.create(
        {
            "name": "自检用自定义机型",
            "nozzle_mm": 0.6,
            "build_x": 300, "build_y": 300, "build_z": 400,
            "materials": ["PLA", "PETG"],
            "enclosure": True,
        }
    )
    assert created.id and created.builtin is False, created.builtin
    assert reg.get(created.id) is not None, "新建后应能查到"

    # 自定义机型也要能推导出约束，且参数确实生效
    constraints = printers.resolve_process(created, "PLA")
    assert abs(constraints.min_wall_mm - 1.8) < 0.01, constraints.min_wall_mm
    assert constraints.max_build_z == 400, constraints.max_build_z

    updated = reg.update(created.id, {"build_z": 350, "nozzle_mm": 0.4})
    assert updated.build_z == 350 and updated.nozzle_mm == 0.4, updated
    assert abs(printers.resolve_process(updated, "PLA").min_wall_mm - 1.2) < 0.01

    # 选中并持久化
    reg.select(created.id, "PETG")
    assert reg.selected().id == created.id
    assert reg.effective_material() == "PETG"

    reloaded = printers.PrinterRegistry(reg._file)  # noqa: SLF001 - 自检直接验证落盘
    assert reloaded.get(created.id) is not None, "重启后自定义机型必须还在"
    assert reloaded.selected().id == created.id, "重启后选择必须保留"
    assert reloaded.effective_material() == "PETG"

    # 内置机型不能被删除，但可以被用户覆盖修改
    assert reg.delete("generic-220-04") is False, "纯内置机型不应可删"
    reg.update("generic-220-04", {"build_z": 999})
    assert reg.get("generic-220-04").build_z == 999, "内置机型允许被覆盖"
    reg.reset_custom()
    assert reg.get("generic-220-04").build_z == 250, "重置后应恢复内置原值"

    # 清理
    reg.reset_custom()
    reg.select(original_selected)
    return "自定义机型 增/改/删/持久化/选中保留、内置覆盖与重置 全部正确"


def t_printer_affects_analysis() -> str:
    """同一个模型在「不同喷嘴」的机器上必须得到不同的结论。"""
    from aiforge import mesh, printers

    printers.reset_for_tests()
    reg = printers.registry(refresh=True)
    base = reg.get("generic-220-04")

    # 阈值关系本身就是设计约束：最小外形尺寸必须低于壁厚下限，
    # 否则「壁厚不足」这条检查会被「零件太细」永远盖住、永远触发不了。
    for profile in printers.BUILTIN_PRINTERS:
        constraints = printers.resolve_process(profile)
        assert constraints.rough_min_dim_mm <= constraints.min_wall_mm, (
            f"{profile.id}: 最小可成型尺寸({constraints.rough_min_dim_mm}) "
            f"必须 <= 壁厚下限({constraints.min_wall_mm})，否则壁厚检查不可达"
        )

    fine = printers.PrinterProfile.from_dict({**base.to_dict(), "nozzle_mm": 0.4})
    coarse = printers.PrinterProfile.from_dict({**base.to_dict(), "nozzle_mm": 0.8})
    fine_limits = mesh.limits_from_printer(fine, "PLA")
    coarse_limits = mesh.limits_from_printer(coarse, "PLA")
    assert abs(fine_limits.min_wall_mm - 1.2) < 0.01, fine_limits
    assert abs(coarse_limits.min_wall_mm - 2.4) < 0.01, coarse_limits
    assert abs(fine_limits.rough_min_dim_mm - 0.8) < 0.01, fine_limits
    assert abs(coarse_limits.rough_min_dim_mm - 1.6) < 0.01, coarse_limits

    def make_metrics(thickness: float) -> mesh.MeshMetrics:
        """一个板状模型：厚度可控，另外两轴足够大以免干扰判断。

        壁厚现在走**射线法实测**（`metrics.wall`），不再从包围盒推，
        所以夹具也必须带上实测值 —— 实心板的射线法厚度就等于板的厚度。
        不带的话壁厚分支永远不会触发，这条测试就变成空跑了。
        """
        return mesh.MeshMetrics(
            triangles=12,
            bbox=(80.0, 60.0, thickness),
            bbox_min=(0.0, 0.0, 0.0),
            bbox_max=(80.0, 60.0, thickness),
            volume_mm3=80.0 * 60.0 * thickness,
            watertight=True,
            sits_on_plate=True,
            wall=mesh.WallThickness(
                min_mm=thickness,
                p05_mm=thickness,
                median_mm=thickness,
                samples=12,
                thinnest=[((40.0, 30.0, thickness / 2), thickness)],
            ),
        )

    # 1.3mm 板：0.4mm 喷嘴（最小外形 0.8 / 壁厚 1.2）→ 两轴都过；
    #          0.8mm 喷嘴（最小外形 1.6）→ 连成型尺寸都不够
    plate = make_metrics(1.3)
    mesh._collect_issues(plate, fine_limits)  # noqa: SLF001
    assert not plate.too_thin_wall and not plate.too_thin_rough, f"1.3mm 在 0.4mm 喷嘴上应合格：{plate.issues}"
    plate.issues.clear()
    mesh._collect_issues(plate, coarse_limits)  # noqa: SLF001
    assert plate.too_thin_rough and not plate.too_thin_wall, f"1.3mm 在 0.8mm 喷嘴上应判太细：{plate.issues}"
    assert any("1.6mm" in i and "0.8mm" in i for i in plate.issues), plate.issues

    # 2.0mm 板：0.4mm 喷嘴合格；0.8mm 喷嘴能成型（>1.6）但壁厚不足（<2.4）→ 命中壁厚分支
    mid = make_metrics(2.0)
    mesh._collect_issues(mid, fine_limits)  # noqa: SLF001
    assert not mid.too_thin_wall and not mid.too_thin_rough, f"2.0mm 在 0.4mm 喷嘴上应合格：{mid.issues}"
    mid.issues.clear()
    mesh._collect_issues(mid, coarse_limits)  # noqa: SLF001
    assert mid.too_thin_wall and not mid.too_thin_rough, f"2.0mm 在 0.8mm 喷嘴上应判壁厚不足：{mid.issues}"
    assert any("2.4mm" in i and "0.8mm" in i for i in mid.issues), mid.issues

    # 尺寸超出成型范围（Prusa MINI 180³ 放不下 300mm 的件）
    big = mesh.MeshMetrics(
        triangles=12,
        bbox=(300.0, 300.0, 150.0),
        bbox_min=(0.0, 0.0, 0.0),
        bbox_max=(300.0, 300.0, 150.0),
        volume_mm3=1.0,
        watertight=True,
        sits_on_plate=True,
    )
    mesh._collect_issues(big, mesh.limits_from_printer(reg.get("prusa-mini-04"), "PLA"))  # noqa: SLF001
    assert big.exceeds_build_volume, big.issues
    assert any("180×180×180" in i for i in big.issues), big.issues
    assert any("Prusa MINI" in i for i in big.issues), big.issues

    # 大机器上同一个模型不该报「放不下」
    big.issues.clear()
    big.exceeds_build_volume = False
    mesh._collect_issues(big, mesh.limits_from_printer(reg.get("large-500-04"), "PLA"))  # noqa: SLF001
    assert not big.exceeds_build_volume, big.issues

    # 树脂机不按喷嘴判断：1.3mm 在树脂机上完全合格（树脂最小细节 0.4mm）
    resin = reg.get("resin-generic-405")
    plate.issues.clear()
    plate.too_thin_wall = False
    plate.too_thin_rough = False
    mesh._collect_issues(plate, mesh.limits_from_printer(resin, "树脂"))  # noqa: SLF001
    assert not plate.too_thin_wall, f"树脂机不该按喷嘴判壁厚：{plate.issues}"
    assert not plate.too_thin_rough, f"1.3mm 在树脂机上应合格：{plate.issues}"

    # ---- 螺纹洗白分支必须区分真螺旋/同心环/判不出，不能只看 `thread_depth is not None` ----
    # 以前这个分支完全不读 `metrics.thread_is_helix`：同心环假螺纹（`detect_helix_thread`
    # 已判成 False）或者样本不够判不出（None）都会走进「检测到螺旋螺纹，不是壁做得薄」
    # 的洗白文案，还会挤掉下面真正该报的薄壁告警。`reqtests.py` 那边同样的三分支早就
    # 修了（见 `thread:real` 那段），这里一直没同步 —— 同一类判据养两份、修一份漏一份。
    def make_thread_metrics(is_helix: bool | None) -> mesh.MeshMetrics:
        m = make_metrics(2.0)
        m.thread_depth_mm = 0.46
        m.thread_is_helix = is_helix
        return m

    spiral = make_thread_metrics(True)
    mesh._collect_issues(spiral, coarse_limits)  # noqa: SLF001
    assert spiral.too_thin_wall, "真螺旋也得置位 too_thin_wall（牙尖确实是射线法量到的最薄处）"
    assert any("真螺旋螺纹" in i for i in spiral.issues), spiral.issues
    assert not any("同心环" in i for i in spiral.issues), spiral.issues
    assert not any("0.75mm 细牙" in i for i in spiral.issues), (
        "「即 0.75mm 细牙」是把螺距当成实测数字写进文案，`measure_thread_depth` 从来没测过螺距"
    )

    rings = make_thread_metrics(False)
    mesh._collect_issues(rings, coarse_limits)  # noqa: SLF001
    assert any("同心环而不是螺旋" in i for i in rings.issues), rings.issues
    assert not any("检测到真螺旋螺纹" in i for i in rings.issues), (
        "同心环不能被说成螺旋（这正是要避免的那类洗白）"
    )

    unknown = make_thread_metrics(None)
    mesh._collect_issues(unknown, coarse_limits)  # noqa: SLF001
    joined = "\n".join(unknown.issues)
    assert "没能判定是螺旋还是同心环" in joined, unknown.issues
    assert "真螺旋螺纹" not in joined, "样本不足不能当成已确认是真螺旋"
    assert "同心环而不是螺旋" not in joined, "样本不足不能反过来咬定是同心环"

    # 反向（关键）：没测出螺纹起伏时，普通薄壁告警必须照常触发，不能被螺纹分支「顺手」吃接走。
    no_thread = make_metrics(2.0)
    no_thread.thread_depth_mm = None
    no_thread.thread_is_helix = None
    mesh._collect_issues(no_thread, coarse_limits)  # noqa: SLF001
    assert any("2.4mm" in i and "0.8mm" in i for i in no_thread.issues), no_thread.issues
    assert not any("螺旋螺纹" in i for i in no_thread.issues), no_thread.issues

    return "薄壁/最小尺寸/成型范围三类判定均随机型与喷嘴正确变化；螺纹洗白分支按真螺旋/同心环/判不出三分"


def t_printer_prompt_injection() -> str:
    """打印机约束必须真的进入 AI 提示词，而不是只存在数据库里。"""
    from aiforge import llm, printers

    printers.reset_for_tests()
    reg = printers.registry(refresh=True)
    profile = reg.get("prusa-mini-04")
    constraints = printers.resolve_process(profile, "PLA")
    block = printers.compact_constraints(profile, constraints, "PLA")

    # 关键数字都要出现
    for needle in ("180", "0.4mm", "1.2mm", "$fn", "PLA", "50°"):
        assert needle in block, f"约束块缺少 {needle}：\n{block}"

    design = llm.create_user_prompt("设计一个手机支架", None, block)
    assert "手机支架" in design and block in design, "生成提示词必须带上打印机约束"
    assert "目标打印机" in design

    modify = llm.modify_user_prompt("cube([10,10,10]);", "高度改成150mm", block)
    assert block in modify and "150" in modify

    review = llm.review_user_prompt("cube([1,1,1]);", "测量数据", "需求", block)
    assert block in review, "打印性检查必须知道是哪台机器"

    # 系统提示词要明确要求按目标机器设计
    system = llm.design_system_prompt()
    assert "打印机适配" in system and "成型范围" in system, "系统提示词缺少打印机适配要求"
    review_system = llm.REVIEW_SYSTEM_PROMPT
    assert "这台具体的打印机" in review_system, "评审系统提示词必须以目标机器为准绳"
    return "约束块完整注入生成/修改/评审三条提示词链路"


def t_printer_http() -> str:
    """打印机接口的形状与语义。"""
    from fastapi.testclient import TestClient

    from aiforge import printers
    from aiforge.app import create_app

    printers.reset_for_tests()
    printers.registry(refresh=True)
    client = TestClient(create_app(), base_url="http://127.0.0.1:8760", raise_server_exceptions=False)

    listing = client.get("/api/printers")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["selected_id"], body
    assert len(body["printers"]) >= 10, len(body["printers"])
    assert body["materials"], body
    first = body["printers"][0]
    for field in ("id", "name", "nozzle_mm", "build_volume_mm", "constraints", "checklist"):
        assert field in first, f"机型缺少字段 {field}"

    selected = client.get("/api/printers/selected").json()
    assert selected["printer"]["id"] == body["selected_id"], selected
    assert selected["note"], selected

    # 切换机型（`target` 必须真的被用上：以前这行只是把机型查出来就丢了，
    # 底下又另写一份字面量，两者不一致时没人报 —— 见自检「赋值了却从未被读」那条判据）
    target = next(p for p in body["printers"] if p["id"] == "prusa-mini-04")
    switched = client.post("/api/printers/select", json={"printer_id": target["id"], "material": "PLA"})
    assert switched.status_code == 200, switched.text
    assert switched.json()["printer"]["id"] == target["id"]
    assert client.get("/api/settings").json()["printer_id"] == target["id"], "选择必须写进设置"

    # 详情
    detail = client.get("/api/printers/prusa-mini-04")
    assert detail.status_code == 200, detail.text
    assert detail.json()["build_volume_mm"]["x"] == 180
    assert client.get("/api/printers/no-such-printer").status_code == 404

    # 新建
    created = client.post(
        "/api/printers",
        json={"name": "接口测试机型", "nozzle_mm": 0.5, "build_z": 300, "materials": ["PLA"]},
    )
    assert created.status_code == 201, created.text
    new_id = created.json()["id"]
    assert created.json()["builtin"] is False

    # 参数校验：喷嘴不可能为 0 或 5mm
    bad = client.post("/api/printers", json={"name": "坏的", "nozzle_mm": 5})
    assert bad.status_code == 422, bad.text

    # 修改与删除
    updated = client.put(f"/api/printers/{new_id}", json={"build_z": 333})
    assert updated.status_code == 200 and updated.json()["build_z"] == 333, updated.text
    assert client.delete(f"/api/printers/{new_id}").status_code == 200
    assert client.get(f"/api/printers/{new_id}").status_code == 404

    # 内置机型不可删
    assert client.delete("/api/printers/prusa-mini-04").status_code == 400

    # 预览接口：不保存也能算出这台配置意味着什么
    preview = client.post(
        "/api/printers/preview",
        json={"nozzle_mm": 0.6, "build_x": 200, "build_y": 200, "build_z": 200, "material": "ABS", "enclosure": False},
    )
    assert preview.status_code == 200, preview.text
    assert abs(preview.json()["constraints"]["min_wall_mm"] - 1.8) < 0.01, preview.json()
    assert preview.json()["notes"], "开放机身 + ABS 应当给出提示"

    # 知识库自述
    knowledge = client.get("/api/printers/knowledge/summary").json()
    assert knowledge["builtin_count"] >= 10
    assert len(knowledge["features"]) >= 8, knowledge["features"]
    for feature in knowledge["features"]:
        assert feature["label"] and feature["affects"], feature

    # 重置
    assert client.post("/api/printers/reset").json()["ok"] is True
    assert client.post("/api/printers/select", json={"printer_id": "generic-220-04"}).status_code == 200
    return "机型列表/切换/新建/修改/删除/预览/知识库自述/重置 全部符合契约"


def t_webview_runtime() -> str:
    """WebView2 运行时检测必须可用且不拖慢启动。"""
    import time

    from aiforge import webview_runtime

    started = time.perf_counter()
    status = webview_runtime.detect()
    elapsed_ms = (time.perf_counter() - started) * 1000

    # 检测在打开窗口前同步执行，必须极快（实测 < 5ms），否则会拖慢启动
    assert elapsed_ms < 2000, f"WebView2 检测耗时过长：{elapsed_ms:.0f} ms"
    assert status.detail, "必须给出可读结论"

    payload = status.to_dict()
    for field in ("installed", "version", "source", "detail", "install_hint", "download_url"):
        assert field in payload, f"缺少字段 {field}"
    assert payload["download_url"].startswith("https://"), payload["download_url"]

    if status.installed:
        assert status.version, "已安装时必须带版本号"
        assert status.source in ("registry", "filesystem"), status.source
    else:
        # 缺失时必须给出「怎么办」，否则用户只会看到浏览器莫名其妙打开
        assert status.install_hint, "缺失时必须给出处理说明"

    # 版本目录的选取必须是「数值上最新」，不是「字符串倒序第一个」。
    # 本机实测过干扰形状：`...\\EdgeWebView\\Application` 里并列着 `SetupMetrics`
    # 与 `154.0.4258.62`；而字符串序里 `9` 比 `1` 大，同时装了 99.x / 154.x 的机器
    # 会把**旧**运行时当新的报给用户。
    import tempfile

    from aiforge.webview_runtime import _from_filesystem, _version_key

    assert _version_key("154.0.4258.62") > _version_key("99.0.1150.38"), (
        "版本排序还是字符串序（154 应大于 99）"
    )
    with tempfile.TemporaryDirectory(prefix="webview2-fixture-") as td:
        fixture = Path(td)
        for name in ("99.0.1150.38", "154.0.4258.62", "SetupMetrics"):
            made = fixture / name
            made.mkdir()
            if name != "SetupMetrics":
                (made / "msedgewebview2.exe").write_bytes(b"MZ")
        picked, picked_source = _from_filesystem([fixture])
        assert picked == "154.0.4258.62", (
            f"从夹具里挑出的不是最新运行时，拿到的是 {picked!r}（旧字符串序会拿到 99.0.1150.38）"
        )
        assert picked_source == "filesystem", picked_source

    # 两种证据对用户的意义不一样，结论必须分层写。
    # 「注册表有登记」与「只在磁盘上找到文件」不能共用一句「已就绪」：
    # WebView2 的官方 loader 是按注册表那个 GUID 项找运行时的，本机证不了
    # 「有文件、无登记」时它能不能命中 —— 拿不定的那一层必须写给用户看。
    saved_probes = (webview_runtime._from_registry, webview_runtime._from_filesystem)
    try:
        webview_runtime._from_registry = lambda: (None, None)
        webview_runtime._from_filesystem = lambda roots=None: ("154.0.4258.62", "filesystem")
        only_files = webview_runtime.detect()
        assert only_files.installed is True, "只找到文件时仍然该说运行时在（关掉这条会直接把人推进浏览器）"
        assert "未登记" in only_files.detail and "注册表" in only_files.detail, (
            f"只找到文件却被说成系统已登记：{only_files.detail}"
        )
        webview_runtime._from_filesystem = lambda roots=None: (None, None)
        webview_runtime._from_registry = lambda: ("154.0.4258.62", "registry")
        registered = webview_runtime.detect()
        assert "注册表已登记" in registered.detail, f"注册表证据应当明说来源：{registered.detail}"
        assert registered.detail != only_files.detail, "两种证据写成了同一句话，上面那层区分等于没做"
    finally:
        (webview_runtime._from_registry, webview_runtime._from_filesystem) = saved_probes

    # 缓存不能因为重复调用而失效或变慢
    assert webview_runtime.cached_status() is webview_runtime.cached_status(), "应缓存结果"
    return f"检测耗时 {elapsed_ms:.1f} ms，installed={status.installed}，来源={status.source}"


def t_icon_assets() -> str:
    """应用图标必须存在、结构有效、且小尺寸下仍可辨认。

    没有图标（或 16×16 糊成一团）的软件在用户眼里就是半成品，
    而且 Windows 会在快捷方式里显示默认图标，用户根本找不到。
    """
    import struct
    import subprocess
    import sys as _sys

    root = ROOT
    icon = root / "build" / "AI-3D-Forge.ico"
    if not icon.is_file():
        # 允许按需生成，保证自检在干净仓库里也能跑
        subprocess.run(
            [_sys.executable, str(root / "scripts" / "make_icon.py")],
            check=True, capture_output=True,
        )
    assert icon.is_file(), icon

    raw = icon.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", raw, 0)
    assert (reserved, kind) == (0, 1), f"不是合法 ICO：reserved={reserved} type={kind}"
    sizes: list[int] = []
    for index in range(count):
        offset = 6 + index * 16
        width, height = raw[offset], raw[offset + 1]
        # 高也必须看：ICO 目录项里宽高是两个字节，只查宽会让一个**方形**图标需求
        # 默默放过一张长条图（任务栏拉伸后变形，运行时不会报错）。
        # 256px 在 ICO 里两字节都记 0，所以这里比的是同一个表示。
        assert height == width, f"图标目录项不是方形：{width}×{height}"
        payload_len, payload_off = struct.unpack_from("<II", raw, offset + 8)
        assert payload_off + payload_len <= len(raw), "ICO 目录项越界"
        assert raw[payload_off : payload_off + 4] == b"\x89PNG", "条目应为 PNG 数据"
        sizes.append(256 if width == 0 else width)
    assert 16 in sizes, f"缺少 16×16（任务栏/列表视图要用）：{sizes}"
    assert 256 in sizes, f"缺少 256×256（大图标视图要用）：{sizes}"
    assert len(sizes) >= 5, sizes
    return f"{len(sizes)} 个尺寸：{', '.join(str(s) for s in sizes)}"


def t_first_run_setup() -> str:
    """--first-run-setup 必须存在、可调用、且失败时不阻断安装。"""
    import subprocess
    import sys as _sys

    main_py = ROOT / "main.py"
    text = main_py.read_text(encoding="utf-8")
    assert "--first-run-setup" in text, "缺少首个启动准备入口"
    assert "_first_run_setup" in text, "缺少实现"

    # 帮助信息里必须能看到这个参数（安装器就是靠它调用）
    proc = subprocess.run(
        [_sys.executable, str(main_py), "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--first-run-setup" in (proc.stdout or ""), "帮助信息里没有该参数"

    # 安装器必须调用它，而且不能是 runhidden（否则用户看不到进度，以为卡死）
    iss = (ROOT / "installer" / "AI-3D-Forge.iss").read_text(encoding="utf-8")
    assert "--first-run-setup" in iss, "安装器没有调用首次准备"
    run_section = iss.split("[Run]", 1)[1].split("[UninstallRun]", 1)[0]
    assert "--first-run-setup" in run_section, "首次准备不在 [Run] 段里"
    # 注意：Inno Setup 用「分号」分隔同一行的参数，而整行注释也以分号开头。
    # 因此只能剔除「整行注释」，绝不能按分号截断 —— 那样会把 Parameters: 字段
    # 整段删掉（也会让 runhidden 之类的检查变成永远通过）。
    body = " ".join(
        line for line in run_section.splitlines() if not line.lstrip().startswith(";")
    )
    assert "runhidden" not in body, (
        "首次准备不能是 runhidden —— 它会下载 21MB，用户必须看得见进度"
    )
    assert "waituntilterminated" in body, "必须等待准备完成再进入完成页"
    assert "StatusMsg" in body, "必须给出进度提示文字"
    assert "first-run.log" in body, "准备工作必须留下日志，便于排查失败原因"
    # 静默卸载绝不能弹模态框：/SUPPRESSMSGBOXES 不抑制 [Code] 里的 MsgBox，
    # 会让人看不到对话框的无人值守卸载永久挂住（实测踩过）。
    code_section = iss.split("[Code]", 1)[1] if "[Code]" in iss else ""
    if "MsgBox" in code_section:
        assert "UninstallSilent" in code_section, (
            "[Code] 里的 MsgBox 必须用 UninstallSilent 保护，否则静默卸载会永久挂起"
        )
    return "入口存在、帮助可见、安装器以可见方式调用并留日志、无静默卸载隐患"


def t_frozen_webview_deps() -> str:
    """打包版必须包含 pywebview 运行所需的模块。

    这是「只在发布版上复现」那一类故障的守门人：曾经在 .spec 的 excludes 里
    排除了 proxy_tools，以为只有 Flask/Django 后端才需要它 ——
    结果构建机上一切正常（site-packages 里有），打包版一启动就
    "No module named 'proxy_tools'" 并静默退化成浏览器窗口。
    """
    spec = (ROOT / "AI-3D-Forge.spec").read_text(encoding="utf-8")
    excludes_block = spec.split("excludes:", 1)[1].split("]", 1)[0]

    # 这些模块是运行时真正需要的，绝不能被排除。
    # bottle / proxy_tools 是最坑的两个：webview/http.py 与 webview/__init__.py
    # 在**顶层** import 它们，排除后 import webview 直接失败，
    # 原生窗口永远起不来、静默退回浏览器，而构建机上一切正常。
    must_not_exclude = {
        "bottle": "webview/http.py 顶层 import bottle",
        "proxy_tools": "webview/__init__.py 顶层 from proxy_tools import module_property",
        "webview": "原生窗口本体",
        "keyring": "API Key 凭据存储",
        "httpx": "调用 DeepSeek 与下载 OpenSCAD",
    }
    for module, why in must_not_exclude.items():
        assert f'"{module}"' not in excludes_block, (
            f"{module} 不能出现在 .spec 的 excludes 里（{why}）"
        )

    # 反向检查：曾经踩过的 JS 桥接依赖必须被显式收集
    assert "webview.platforms" in spec or "collect_submodules" in spec, "必须收集 pywebview 的平台后端"

    # 运行时真的能 import 到它们（开发环境同样需要，否则等价故障会在开发期被掩盖）
    import importlib

    for module in ("webview", "bottle", "proxy_tools"):
        try:
            importlib.import_module(module)
        except ImportError as exc:
            raise AssertionError(
                f"当前环境缺少 {module}：{exc}。开发环境装齐才能保证打包版正确。"
            ) from exc

    # 打包产物若已存在，直接验证「原生窗口链路可用」——
    # 这是唯一能真正抓住「打包漏依赖」的检查。
    from aiforge.webview_runtime import check_native_window_ready

    ready, detail = check_native_window_ready()
    return f"bottle/proxy_tools 未被排除且可导入；原生窗口链路：{'可用' if ready else detail[:40]}"


def t_browser_fallback_notice() -> str:
    """退回浏览器时必须给用户明确的说明与退出方式。

    曾经的糟糕表现：打包版因缺依赖静默退回浏览器，用户只看到"软件打开了浏览器"，
    既不知道为什么，也不知道服务还在后台跑、关掉标签页之后还在。
    """
    source = (ROOT / "src" / "aiforge" / "server.py").read_text(encoding="utf-8")
    assert "browser_fallback=1" in source, "回退时必须在 URL 上带标记，界面才能说明原因"
    assert "_notify_browser_mode" in source, "打包版回退时必须弹提示，告知如何退出"
    assert "shutdown_requested" in source, "必须支持通过界面「退出」结束进程"

    from aiforge import server

    assert hasattr(server, "_notify_browser_mode"), "缺少浏览器模式提示实现"
    # 提示内容必须点明「不会自动关闭」与「怎么退出」
    import inspect

    body = inspect.getsource(server._notify_browser_mode)  # noqa: SLF001
    assert "退出" in body, "提示里必须告诉用户怎么退出"
    assert "不会自动关闭" in body, "提示里必须说明服务不会自动关闭"

    # 单向检查会放过真正的坑：后端一直会在 URL 上带标记，但前端从来没读过它，
    # 于是用户手册承诺的「页面顶部说明原因 + 一键安装」其实并不存在（那两个接口也是死的）。
    # 所以两头都得查：标记有人写，也得有人读、读到了要给出安装入口。
    api_text = "\n".join(
        p.read_text(encoding="utf-8")
        for pattern in ("**/*.ts", "**/*.tsx")
        for p in (ROOT / "webui" / "src").glob(pattern)
    )
    assert len(api_text) > 50_000, f"只读到 {len(api_text)} 字符前端源码，这条双向检查等于没做"
    # 只能查“真在读它”的那个表达式，不能只查单词本身：App.tsx 写这个横幅的注释里
    # 也出现了 `browser_fallback` 字样，只查单词的话，把读取删掉守卫还是绿的（投毒实测过）。
    assert "get('browser_fallback')" in api_text, "前端没真读 URL 上的 browser_fallback 标记，回退时用户看不到任何说明"
    assert "'/app/window-runtime/install'" in api_text, "前端没发安装请求，「一键安装」就是句空话"

    # 地址上只该有标记，不该有一句说明文字：上一版还拼了 &reason=<中文句子>，
    # 但前端从来没读过它（文案是拉接口拿的），于是它既是一个死参数，又把一长串
    # 会被截断的中文塞进地址栏。这里把它钉死，防止再长回来一半。
    assert "&reason=" not in source, "回退 URL 上不要拼 reason：界面不读它，只认 browser_fallback 标记"
    # 弹窗里提到的按钮名必须跟界面上真的一样（写过「安装组件」，界面上叫「一键安装」）。
    assert "安装组件" not in source, "提示里的按钮名与界面不一致（界面上那个叫「一键安装」）"
    assert "一键安装" in source and "一键安装" in api_text, "提示与界面的按钮名需要互相对得上"

    # 最后是真跑一次：横幅里的「一键安装」背后那条接口在打包版里必须是通的。
    # （发出去的 1.0.2 里它就是死的 —— 见 t_no_undefined_names 的由来。）
    from fastapi.testclient import TestClient

    from aiforge import webview_runtime
    from aiforge.app import create_app

    real_try_install = webview_runtime.try_install
    webview_runtime.try_install = lambda: (True, "自检桩：不会真的安装任何东西")
    try:
        http = TestClient(
            create_app(), base_url="http://127.0.0.1:8760", raise_server_exceptions=False
        )
        status = http.get("/api/app/window-runtime")
        assert status.status_code == 200, status.text
        body = status.json()
        for field in ("installed", "detail"):
            assert field in body, f"横幅要读 {field}，但 /api/app/window-runtime 没返回"
        installed = http.post("/api/app/window-runtime/install")
        assert installed.status_code == 200, (
            f"「一键安装」请求失败（{installed.status_code}）：{installed.text[:120]}"
        )
        assert installed.json().get("ok") is True, installed.json()
    finally:
        webview_runtime.try_install = real_try_install
    return (
        "回退有原因说明、有退出指引、有界面退出通道；前端确实读到标记，"
        "且「一键安装」接口真的能跑通（桩替换了安装动作）"
    )


def t_ps1_are_ascii() -> str:
    """所有 .ps1 必须是纯 ASCII（含注释）。

    这条不是洁癖：Windows PowerShell 5.1 把无 BOM 的 .ps1 按系统 ANSI 代码页
    解码，中文会被破坏。最阴的一种是行尾中文标点解出反引号 → 被当作续行符 →
    吃掉下一行代码 → 脚本静默中途结束、不报错（已实际踩过两次）。
    因此定成硬规则：中文只出现在被调用的 Python 程序输出里。
    """
    offenders: list[str] = []
    for path in sorted((ROOT / "scripts").glob("*.ps1")) + sorted(ROOT.glob("*.ps1")):
        data = path.read_bytes()
        bad_lines = sum(1 for line in data.split(b"\n") if any(byte > 127 for byte in line))
        if bad_lines:
            offenders.append(f"{path.name}({bad_lines} 行)")
    assert not offenders, (
        "以下 .ps1 含非 ASCII 字符，必须改成纯 ASCII：" + "、".join(offenders)
    )
    count = len(list((ROOT / "scripts").glob("*.ps1"))) + len(list(ROOT.glob("*.ps1")))
    return f"{count} 个 .ps1 全部为纯 ASCII"


def _ps_code_skeleton(text: str) -> list[str]:
    """.ps1 的「代码骨架」：注释和字符串的内容换成空格，行数与列位置不变。

    为什么要骨架：`2>&1` 出现在字符串里（`& cmd /c "prog > log 2>&1"`）是交给 cmd.exe
    的重定向，跟 PowerShell 的错误流无关；出现在注释里更不算数。只有代码里的才是。
    不处理 here-string —— 本仓 6 个 .ps1 里只有一处，且所在文件不含任何 `2>&1`；
    真要出现，报的是「这一处算代码」，行号照旧给出来，一眼能判断。
    """
    out: list[str] = []
    in_block = False
    for line in text.split("\n"):
        buf: list[str] = []
        i = 0
        quote = ""
        while i < len(line):
            ch = line[i]
            if in_block:
                if line.startswith("#>", i):
                    in_block = False
                    buf.append("  ")
                    i += 2
                else:
                    buf.append(" ")
                    i += 1
                continue
            if quote:
                if ch == "`" and quote == '"':
                    buf.append("  ")
                    i += 2
                    continue
                if ch == quote:
                    if line.startswith(quote * 2, i):  # 引号成对 = 转义
                        buf.append("  ")
                        i += 2
                        continue
                    quote = ""
                buf.append(" ")
                i += 1
                continue
            if line.startswith("<#", i):
                in_block = True
                buf.append("  ")
                i += 2
                continue
            if ch == "#":
                buf.append(" " * (len(line) - i))  # 行注释：整行余下抹掉
                break
            if ch in "\"'":
                quote = ch
                buf.append(" ")
                i += 1
                continue
            buf.append(ch)
            i += 1
        out.append("".join(buf))
    return out


def t_ps1_native_stderr() -> str:
    """.ps1 里合并原生命令的 stderr（`2>&1`），必须写在显式降过 $ErrorActionPreference 的函数里。

    为什么钉这条 —— 本机 PowerShell 5.1 实测（原生命令往 stderr 写一行，然后 exit 0，
    也就是「命令其实成功了，只是顺便说了句话」）：

    - `cmd ... 2>&1 | Out-Null`      → RemoteException **终止脚本**
    - `cmd ... 2>&1 | ForEach-Object` → 同上
    - `$x = cmd ... 2>&1`（不接管道）  → 同上，有没有管道不是触发条件
    - `cmd ... | Out-Null`（不合并）   → 活下来，退出码是真值
    - 把 $ErrorActionPreference 降到 'Continue' 再合并 → 活下来，退出码是真值，文本也拿到

    另一半：报错之后读 $LASTEXITCODE 也读不到命令的真实退出码——同一个 exit 0 的子进程，
    分 5 次跑，4 次读到 -1、1 次读到真的 0（那一次就是侥幸）。所以「外面包个 try/catch
    再读退出码」也修不好它 —— 正解见 scripts/build.ps1 的 Invoke-Native。

    同一个坑本仓踩过两次：npm 那段注释写着「seen in practice」，然后一模一样的形状在
    pip 那一行又把整个构建打死（pypi.org 读超时，pip 只是打印了一句 "WARNING: Retrying"），
    报出来的还是 PowerShell 的 NativeCommandError 而不是 pip 的话 —— 成功和失败都报错，
    两边都是错的。光靠注释没拦住，故有此守卫。
    """
    offenders: list[str] = []
    sites = 0
    paths = sorted((ROOT / "scripts").glob("*.ps1")) + sorted(ROOT.glob("*.ps1"))

    def enclosing(skel: list[str], idx: int) -> tuple[str, int] | None:
        """往上找包住这一行的 function；先撞到顶格 `}` 就算在顶层。

        为什么不能简单用「最近的一个 function 行」：本仓的函数全定在文件开头，
        函数之后的脚本体是顶层代码 —— 那样会把它们全算成最后一个函数内部的，
        顶层的违规就检不出来了（投毒 P1 实测到的就是这个）。
        """
        for j in range(idx, -1, -1):
            line = skel[j]
            if re.match(r"^\}", line):
                return None
            m = re.match(r"^function\s+(\S+)", line)
            if m:
                return (m.group(1), j)
        return None

    for path in paths:
        raw_lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
        skel = _ps_code_skeleton("\n".join(raw_lines))
        # 值只会在骨架里被抹掉，所以「降没降」看原文，「是不是注释」看骨架。
        lowers = {
            i
            for i, raw in enumerate(raw_lines)
            if skel[i].strip() and re.match(r"^\s*\$ErrorActionPreference\s*=\s*'Continue'", raw)
        }

        for i, line in enumerate(skel):
            if "2>&1" not in line:
                continue
            sites += 1
            owner = enclosing(skel, i)
            if owner is None:
                offenders.append(
                    f"{path.name}:{i + 1} 在函数外（顶层，$ErrorActionPreference 默认为 Stop）合并了 stderr"
                )
                continue
            name, start = owner
            if not any(j in lowers for j in range(start, i)):
                offenders.append(
                    f"{path.name}:{i + 1} 所在函数 {name}() 里没有 $ErrorActionPreference = 'Continue'"
                )
    assert not offenders, (
        "合并原生命令 stderr 的写法会在 PowerShell 5.1 下把成功当成致命错误："
        + "；".join(offenders)
        + "。改法：走 scripts/build.ps1 的 Invoke-Native，或在函数里先降 'Continue' 再合并"
    )
    return f"{len(paths)} 个 .ps1 里 {sites} 处合并原生命令 stderr，全部在降过 $ErrorActionPreference 的函数里"


def t_no_dead_assignments() -> str:
    """不得有「算出来却从未被读」的赋值（自己走 AST，不引 pyflakes）。

    为什么钉这一条（实测踩过）：jobs.py 里写过 `if sampled := any(...)` —— 条件算完
    就丢，抽样渲染的告警**一次都没发出过**：用户以为 AI 看的是完整模型，实际是抽稀图。
    这类缺陷编译器不报、运行不报、老自检也查不到（「代码不养闲人」只查**顶层**死代码与
    未用导入，函数里少一个没人读的局部它看不见）。上一轮是靠外部 pyflakes 扫出来的，
    而项目明确不引 pyflakes/ruff（多一个依赖多一分臃肿）—— 所以这一面自己守住：
    修好的 bug 不该有无声无息长回来的机会。

    扫描面 = 产品代码（src/aiforge + main.py）+ 自检主干（selfcheck.py / selfcheck_design.py）。
    自检主干也进是因为：判据脚本里一个算了不用的值，就是一条**本来打算写却没写**的断言
    —— selfcheck_design.py 的 `clean` 就是这么被抓住的（它是当时全仓唯一一处 F841）。
    build/ 下的一次性取数探针不扫（那是用完就扔的工具，不是产品也不是判据）。

    只收**赋值语句**（`=` / `:=` / `+=` / 带注解赋值）：不收 for 循环变量与
    `with ... as` 句柄 —— 那两种「没用上」大多只是写啰嗦了，不是一种失明；把它们
    拉进来只会让守卫一修就红，反而没人看它报的东西。
    """
    import ast
    import textwrap          # 只给下面的对照集用，不往模块级拉（那里已经挤了 8 个标准库）

    def own_scope_nodes(fn):
        """只走 fn **自己那一层**作用域的节点，遇到嵌套 def/lambda/class 就停。

        不停会怎样：`class Fake: calls = 0` 写在函数体里，`calls` 是**类属性**，
        读它用的是 `Fake.calls` —— 裸名永远不出现在 Load 里。不处理嵌套作用域时，
        11 处命中里 10 处是这种假阳（pyflakes 一处都不报）—— 假阳多到一定程度的
        守卫跟没有一样：下一次真报红的时候没人信它。
        """
        stack = list(fn.body)
        while stack:
            node = stack.pop()
            yield node
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue  # 另起一层作用域，它自己的局部由它自己那一轮扫
            stack.extend(ast.iter_child_nodes(node))

    def unused_assigns(src: str, filename: str) -> list[tuple[int, str, str]]:
        tree = ast.parse(src, filename=filename)
        hits: list[tuple[int, str, str]] = []
        funcs = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for fn in funcs:
            # 「读」走整棵子树（含嵌套）：内层函数读外层变量算读到了，不报
            loads: set[str] = set()
            declared: set[str] = set()
            for node in ast.walk(fn):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    loads.add(node.id)
                elif isinstance(node, (ast.Global, ast.Nonlocal)):
                    declared.update(node.names)
                elif isinstance(node, ast.Delete):
                    loads.update(t.id for t in node.targets if isinstance(t, ast.Name))
            for node in own_scope_nodes(fn):
                if isinstance(node, ast.Assign):
                    raw_targets: list[ast.expr] = list(node.targets)
                elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                    raw_targets = [node.target]
                elif isinstance(node, ast.NamedExpr):
                    raw_targets = [node.target]
                else:
                    continue
                for tgt in raw_targets:
                    if not isinstance(tgt, ast.Name):
                        continue  # a.b = 1 / a, b = ... 不在这张网里
                    if tgt.id.startswith("_") or tgt.id in loads or tgt.id in declared:
                        continue
                    hits.append((tgt.lineno, tgt.id, fn.name))
        return hits

    # --- 阳性/阴性对照：这张网必须既能抓到、又不抓错 ---
    fixture = '''
    def bad_simple():
        got = 1 + 1
        return 2

    def bad_walrus(items):
        if picked := any(items):
            return "yes"
        return "no"

    def ok_underscore():
        _ = 1 + 1
        value = 2
        return value

    def ok_closure():
        outer = 5
        def inner():
            return outer
        return inner

    def ok_class_attr():
        class Fake:
            calls = 0
            def bump(self):
                Fake.calls += 1
                return Fake.calls
        return Fake

    def ok_del():
        tmp = [1, 2]
        del tmp
        return 1
    '''
    caught = {(name, fn) for _ln, name, fn in unused_assigns(textwrap.dedent(fixture), "<fixture>")}
    assert caught == {("got", "bad_simple"), ("picked", "bad_walrus")}, (
        f"对照集判错了（实测抓到 {sorted(caught)}）：这把「算了不用」的尺子自己先不准，"
        "报出来的绿也不能信 —— 要查 own_scope_nodes / Load 判定"
    )

    pkg = ROOT / "src" / "aiforge"
    targets = sorted(p for p in pkg.rglob("*.py") if "__pycache__" not in p.parts)
    targets += [ROOT / "main.py",
                ROOT / "scripts" / "selfcheck.py",
                ROOT / "scripts" / "selfcheck_design.py"]
    assert len(targets) >= 40, f"只扫到 {len(targets)} 个文件，这条检查等于没跑"

    offenders: list[str] = []
    for path in targets:
        for line, name, fn in unused_assigns(path.read_text(encoding="utf-8", errors="ignore"), str(path)):
            offenders.append(f"{path.relative_to(ROOT).as_posix()}:{line} {fn}() 里的 {name}")
    assert not offenders, (
        f"{len(targets)} 个文件里找到 {len(offenders)} 处「赋值了却从未被读」：" + "；".join(offenders)
        + "。改法：要么把它用起来（告警/断言/返回值），要么不要绑定名字"
        "（直接写调用、或赋给 `_`）—— 算了不用就是那里本来该有一句话却没写"
    )
    return (
        f"{len(targets)} 个文件（产品代码 + main.py + 自检主干）扫过，0 处「赋值了却从未被读」；"
        "对照集双向都对：裸赋值/海象算完即丢必报，下划线/闭包/类属性/del 必不报"
        "（类属性那一条是实测假阳：不区分作用域时 10/11 条命中都是它）"
    )


def t_no_shadowed_imports() -> str:
    """产品代码里不得有"函数内重复导入模块级已有名字"（会做出 500）。

    为什么钉这一条（实测踩到）：`settings_routes.update_settings` 里写了
    `from .errors import AppError`（函数内），Python 就把 AppError 当成**整个函数的局部变量**；
    那行导入只在"选了不存在的 OpenSCAD 路径"时执行，所以其余分支里的
    `raise AppError(...)`（打印机不存在、切片软件没检测到）全部先撞
    `UnboundLocalError` → 用户只看到一个含义不明的 500「软件内部出现了意外问题」，
    而自检、录入接口、类型检查都看不出来（它只在那条分支被走到时才炸）。

    只扫 src/aiforge：scripts/ 下的自检脚本里那些带 `as _xxx` 别名或开头就导入的写法
    不会造成这个 bug，而产品代码里一个 500 就是用户看到的故障。
    """
    import ast

    pkg = ROOT / "src" / "aiforge"
    offenders: list[str] = []
    scanned = 0
    for path in sorted(pkg.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        top: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                top |= {a.asname or a.name.split(".")[0] for a in node.names}
        for func in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            inner: dict[str, int] = {}
            for node in ast.walk(func):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for a in node.names:
                        name = a.asname or a.name.split(".")[0]
                        inner[name] = min(inner.get(name, 10**9), node.lineno)
            for name, line in inner.items():
                if name not in top:
                    continue
                # 除了这条 import 本身，函数里还用到这个名字 → 局部绑定会顶掉模块级的
                if any(
                    isinstance(n, ast.Name) and n.id == name and n.lineno != line
                    for n in ast.walk(func)
                ):
                    offenders.append(f"{path.relative_to(ROOT)}:{line} {func.name} 局部导入 {name}")
    assert not offenders, (
        "函数内重复导入了模块级已有的名字，会让它在整个函数里变成未赋值的局部变量：" + "；".join(offenders)
    )
    return f"{scanned} 个模块无遮蔽导入（函数内不重复导入模块级名字）"


def _collect_top_level_bindings(stmts: list, into: set, ast_mod) -> None:
    """模块级绑定：穿过 if/try/for 等复合语句，但不进入 def/class 体。"""
    for st in stmts:
        if isinstance(st, (ast_mod.FunctionDef, ast_mod.AsyncFunctionDef, ast_mod.ClassDef)):
            into.add(st.name)
        elif isinstance(st, ast_mod.Assign):
            for t in st.targets:
                _collect_bind_target(t, into, ast_mod)
        elif isinstance(st, (ast_mod.AnnAssign, ast_mod.AugAssign)):
            _collect_bind_target(st.target, into, ast_mod)
        elif isinstance(st, (ast_mod.Import, ast_mod.ImportFrom)):
            for a in st.names:
                into.add("*" if a.name == "*" else (a.asname or a.name.split(".")[0]))
        elif isinstance(st, (ast_mod.For, ast_mod.AsyncFor)):
            _collect_bind_target(st.target, into, ast_mod)
        elif isinstance(st, (ast_mod.With, ast_mod.AsyncWith)):
            for item in st.items:
                if item.optional_vars:
                    _collect_bind_target(item.optional_vars, into, ast_mod)
        elif isinstance(st, ast_mod.Try):
            for h in st.handlers:
                if h.name:
                    into.add(h.name)
                _collect_top_level_bindings(h.body, into, ast_mod)
            _collect_top_level_bindings(st.body + st.orelse + st.finalbody, into, ast_mod)
        elif isinstance(st, (ast_mod.If, ast_mod.While)):
            _collect_top_level_bindings(st.body + st.orelse, into, ast_mod)


def _collect_bind_target(node, into: set, ast_mod) -> None:
    if isinstance(node, ast_mod.Name):
        into.add(node.id)
    elif isinstance(node, (ast_mod.Tuple, ast_mod.List)):
        for elt in node.elts:
            _collect_bind_target(elt, into, ast_mod)
    elif isinstance(node, ast_mod.Starred):
        _collect_bind_target(node.value, into, ast_mod)


def _collect_any_binding(node, into: set, ast_mod) -> None:
    """一个函数子树里出现过的所有绑定（过近似：宁可漏报，不要误报）。"""
    for sub in ast_mod.walk(node):
        if isinstance(sub, ast_mod.Name) and isinstance(sub.ctx, (ast_mod.Store, ast_mod.Del)):
            into.add(sub.id)
        elif isinstance(sub, ast_mod.arg):
            into.add(sub.arg)
        elif isinstance(sub, (ast_mod.Import, ast_mod.ImportFrom)):
            for a in sub.names:
                into.add(a.asname or a.name.split(".")[0])
        elif isinstance(sub, ast_mod.ExceptHandler) and sub.name:
            into.add(sub.name)
        elif isinstance(sub, (ast_mod.FunctionDef, ast_mod.AsyncFunctionDef, ast_mod.ClassDef)):
            into.add(sub.name)
        elif isinstance(sub, ast_mod.Global):
            into.update(sub.names)


def _scan_undefined_names(source: str, ast_mod, ignore: set[str]) -> list[tuple[int, str, str]]:
    """返回 [(行号, 函数名, 用到的名字)]：这些名字在该名字的可见作用域里根本不存在。"""
    tree = ast_mod.parse(source)
    module_scope: set[str] = set()
    _collect_top_level_bindings(tree.body, module_scope, ast_mod)
    if "*" in module_scope:  # 有 import * 就没法判断，直接跳过整个文件
        return []

    parent: dict = {}
    for node in ast_mod.walk(tree):
        for child in ast_mod.iter_child_nodes(node):
            parent[child] = node

    problems: list[tuple[int, str, str]] = []
    funcs = tuple((ast_mod.FunctionDef, ast_mod.AsyncFunctionDef, ast_mod.ClassDef))
    for st in ast_mod.walk(tree):
        if not isinstance(st, funcs):
            continue
        scope = set(module_scope)
        _collect_any_binding(st, scope, ast_mod)
        cur = parent.get(st)
        while cur is not None:  # 外层函数里的局部名字对闭包可见
            if isinstance(cur, funcs):
                _collect_any_binding(cur, scope, ast_mod)
            cur = parent.get(cur)
        for node in ast_mod.walk(st):
            if (
                isinstance(node, ast_mod.Name)
                and isinstance(node.ctx, ast_mod.Load)
                and node.id not in scope
                and node.id not in ignore
            ):
                problems.append((node.lineno, st.name, node.id))
    return sorted(set(problems))


def t_no_undefined_names() -> str:
    """产品代码里不得用到「整个模块根本没有」的名字（一走到就是 NameError → 500）。

    为什么单独钉这一条（已发到用户手上的版本里踩到了）：
    `api/app_routes.py` 的 `install_window_runtime` 用了 `asyncio.to_thread(...)`，
    可整个模块没有顶层 `import asyncio` —— 唯一那一行写在**另一个**函数 `shutdown()` 里，
    而函数内导入只对那个函数生效。结果用户手册承诺的「一键安装」一点就是 500，
    而它跟「函数内导入顶掉模块级名字」（t_no_shadowed_imports）是同一枚硬币的两面，
    那个守卫抓不到这一面，所以自己查一遍（不引入 pyflakes/ruff：多一个依赖多一分臃肿）。

    只查 src/aiforge + main.py：脚本里的临时探针不受这条约束。
    内置一个阳性对照（自己种一个缺陷，必须能抓到），防止守卫默默变成空跑。
    """
    import ast
    import builtins

    ignore = set(dir(builtins)) | {
        "__file__",
        "__name__",
        "__doc__",
        "__spec__",
        "__package__",
        "__builtins__",
    }

    # 对照 1：正确写法不得报（防误伤）；对照 2：坏写法必须报（防空跑）。
    ok = _scan_undefined_names(
        "import asyncio\n\n\nasync def a(p):\n    return await asyncio.to_thread(int, p)\n",
        ast,
        ignore,
    )
    assert not ok, f"这条守卫对正确写法也报错，会把日常开发堵死：{ok}"
    planted = _scan_undefined_names(
        "import os\n\n\ndef a():\n    return os.getcwd()\n\n\ndef b():\n    return asyncio.to_thread(int)\n",
        ast,
        ignore,
    )
    assert planted, "这条守卫连自己种的缺陷都抓不到，等于没做"
    assert planted[0][2] == "asyncio", f"抓到的不是预期的名字：{planted}"

    targets = sorted((ROOT / "src" / "aiforge").rglob("*.py")) + [ROOT / "main.py"]
    files = [p for p in targets if "__pycache__" not in p.parts]
    assert len(files) >= 30, f"只扫到 {len(files)} 个文件，这条检查范围不对"
    offenders: list[str] = []
    for path in files:
        for line, func, name in _scan_undefined_names(path.read_text(encoding="utf-8"), ast, ignore):
            offenders.append(f"{path.relative_to(ROOT)}:{line} {func}() 用了未定义的 {name}")
    assert not offenders, "函数里用到了模块根本没有的名字，走到就 NameError：" + "；".join(offenders)
    return f"{len(files)} 个产品文件的局部名字全部可解析（含阳性/阴性对照）"


def _strip_code_prose(src: str) -> str:
    """把 # 注释截掉、docstring 抹成空行（它们里的名字是给人看的，不算引用）。

    注意行尾注释只能截 `#` 之后的部分：上一版把整行抹掉，于是
    `chosen = jobs._prefer(..., spec)  # noqa: SLF001` 这类写法让真实调用凭空消失，
    守卫一口气把十几个好好工作的函数报成死代码。误伤比漏报贵得多。
    """
    import ast
    import io
    import tokenize

    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    prose_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            prose_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    cut_col: dict[int, int] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                ln = tok.start[0]
                cut_col[ln] = min(cut_col.get(ln, 10**9), tok.start[1])
    except (tokenize.TokenError, IndentationError):
        pass
    out: list[str] = []
    for i, line in enumerate(src.splitlines(), 1):
        if i in prose_lines:
            out.append("")
        elif i in cut_col:
            out.append(line[: cut_col[i]].rstrip())
        else:
            out.append(line)
    return "\n".join(out)


def _strip_code_prose_ts(src: str) -> str:
    """抹掉 JS/TS 注释，保留行号。

    不抹的话，「这个导出其实没人用了，只在注释里提了一嘴」会被注释洗白。
    但只认**行首**的 `//` 与 `/*`（JSDoc 形状）：字符串里也会碰见 `/*`（实测
    Viewer3D.tsx 里有一句 `"/*' } })"`），拿正则跨行配对会一路吞掉上百行真代码。
    同一行尾巴上的 `//` 可以截，但要先看前面引号是否成对（URL 里的 `//` 不是注释）。
    所有判断失败的方向都是「少抹」，只会漏报不会误伤。
    """
    lines = src.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith("//"):
            out.append("")
            i += 1
            continue
        if stripped.startswith("/*"):
            end = -1
            for j in range(i, min(i + 20, len(lines))):  # 块注释超 20 行 = 配对坏了，不认
                if "*/" in lines[j]:
                    end = j
                    break
            if end == -1:
                out.append(line)
                i += 1
                continue
            for j in range(i, end):
                out.append("")
            tail = lines[end].split("*/", 1)[1]
            out.append(tail if tail.strip() else "")
            i = end + 1
            continue
        cut = -1
        k = line.find("//")
        while k != -1:
            head = line[:k]
            if (head.count('"') + head.count("'") + head.count("`")) % 2 == 0:
                cut = k
                break
            k = line.find("//", k + 2)
        out.append(line[:cut].rstrip() if cut != -1 else line)
        i += 1
    return "\n".join(out)


def _strip_prose(path, text: str) -> str:
    """按语言抹注释；认不出的语言原样返回（宁可漏报也不误伤）。"""
    if path.suffix in {".ts", ".tsx", ".js", ".mjs"}:
        return _strip_code_prose_ts(text)
    return _strip_code_prose(text)


def t_no_dead_symbols() -> str:
    """代码不养闲人：顶层函数/常量/组件不得「整个仓库没有任何代码引用」，导入不得不用。

    为什么钉这一条（本轮按「小而美」清查实测捣出来 24 处）：死代码比 bug 贵 ——
    它会长期骗人。一个 OpenSCAD 数字签名检查函数根本没人调，文档里却写着
    「这一点写在某某函数里」；三个网格兜底常量早就被机型知识库里的同名字段取代，
    读代码的人仍会以为那里有一层默认值；还有 7 个从未渲染过的图标。
    最坏的一类是**反向**的：一份专门写好的图片复核系统提示词没接上，
    而解析器就按那份提示词的第一行契约判定 —— 守卫把它钉住之后，
    这类「写了但没接上」的东西要么接上、要么删掉，不许继续躺着。

    判据（宁可漏报也不误伤日常开发）：
      * 只算「名字在整个代码语料里只出现 1 次」的顶层符号 —— 那 1 次就是它的定义行；
        注释与 docstring **不算**引用（不然守卫自己的文案就会把要它查的名字洗白）；
        但 `mod.func()` 这种点号调用**算**引用（否则跨模块调用全部隐身）；
      * 语料 = 产品代码 + 前端源码 + 自检/构建脚本 + mock + 打包配置，
        所以「只被自检调用」算有人用（那是刻意保留的可测接口）；
      * 带装饰器的函数跳过：框架拿装饰器注册，名字不会出现在调用处；
      * 自带一个阳性对照，防止守卫默默变成空跑。

    废导入的判据**和上面不一样**，必须按文件判（见 `_unused_imports`）：
      上一版这里也套了「全仓只出现 1 次」这道过滤器，于是 `import tempfile` 只要
      别的文件也写着 tempfile 就被洗白 —— 实测这条把产品里 9 处真没用的导入
      全放过，而自检文案写着「含未用导入检查」并全绿。

    还有一类必须按「双向」判：包 `__init__.py` 里的「包结构」清单（见 `_scan_package_map`）。
    它光秃秃只写模块名，不带点号不带括号，所以上面那条文档引用守卫管不到它。
    实测漏例：`shortcuts.py` 已随两个没人调的端点删掉，这张地图还留着它那一行。
    """
    import ast
    import re

    product_py = [
        p
        for p in sorted((ROOT / "src" / "aiforge").rglob("*.py"))
        if "__pycache__" not in p.parts and p.name != "__init__.py"
    ] + [ROOT / "main.py"]
    web_ts = sorted(p for p in (ROOT / "webui" / "src").rglob("*.ts*") if p.suffix != ".d.ts")
    corpus_files = (
        product_py
        + web_ts
        + sorted((ROOT / "scripts").rglob("*.py"))
        + sorted((ROOT / "scripts").rglob("*.ps1"))
        + sorted((ROOT / "webui" / "scripts").glob("*.mjs"))
        + [ROOT / "AI-3D-Forge.spec", ROOT / "webui" / "mock-server.mjs", ROOT / "webui" / "index.html"]
        + sorted((ROOT / "installer").glob("*.iss"))
    )
    texts = {p: p.read_text(encoding="utf-8", errors="ignore") for p in corpus_files if p.is_file()}
    assert len(texts) >= 80, f"只读到 {len(texts)} 个代码文件，这条检查的覆盖范围不对"
    # 产品文件自己的定义行也得在语料里（所以计数 ≤ 1 才是「只有定义」）；
    # 但别人的引用只算**代码**，注释/docstring 先抹掉。
    stripped_corpus = "\n".join(_strip_prose(p, t) for p, t in texts.items())

    def count_refs(name: str, corpus: str) -> int:
        # 左边界只排除「名字自己是个长名字的后缀」，但**必须**让 `mod.func()` /
        # `obj.CONST` 算引用：Python 跨模块调用就是点号形式，上一版多排除了
        # 一个 `.`，直接把 35 个好好工作的函数报成死代码。
        return len(re.findall(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", corpus))

    def refs(name: str) -> int:
        return count_refs(name, stripped_corpus)

    # ---- 阳性/阴性对照：种在隔离小语料里，不借用真实代码（否则 `live` 这种
    # 常见词会被全仓引用污染，对照就成了自证）----
    planted_src = (
        "import os\nimport json\n\n\n"
        "def _probe_live():\n    return os.getcwd()\n\n\n"
        "def _probe_orphan():\n    return 1\n\n\n"
        "_PROBE_USED = 3\n_PROBE_UNUSED = 4\n\n\n"
        "probe_sink = (_probe_live, _PROBE_USED, os.getcwd)\n\n\n"
        "def _probe_commented():  # 只在注释里提到\n    return 0\n"
    )
    planted_stripped = _strip_code_prose(planted_src)
    planted_dead = sorted(n for n, _ in _scan_dead_python_symbols(planted_src, ast) if count_refs(n, planted_stripped) <= 1)
    assert planted_dead == ["_PROBE_UNUSED", "_probe_commented", "_probe_orphan"], f"死代码对照失败：{planted_dead}"
    planted_unused = sorted(n for n, _ in _scan_unused_imports(planted_src, ast))
    assert planted_unused == ["json"], f"未用导入对照失败：{planted_unused}"

    # 未用导入的对照必须打在**真实全仓语料**上：上面那段隔离小语料里 `json` 只出现
    # 一次，所以它验不出「被全仓同名洗白」这个坑。这里三个名字在全仓都出现上百次：
    # `json` 该被报出来，`tempfile`/`socket` 各靠一种**写下凭据**的方式豁免。
    planted_unused_src = (
        "import json\nimport tempfile  # noqa: F401 - 只为副作用留着\nimport socket\n\n\n"
        "def _probe_live3():\n    return 1\n\n\nprobe_sink3 = (_probe_live3,)\n"
    )
    planted_texts = dict(texts)
    planted_texts[ROOT / "src" / "aiforge" / "_probe_consumer.py"] = "from .probe_mod import socket\n"
    planted_unused2 = [
        n
        for _, n in _unused_imports(
            planted_unused_src, ROOT / "src" / "aiforge" / "probe_mod.py", planted_texts, ast
        )
    ]
    assert planted_unused2 == ["json"], f"按文件判的未用导入对照失败：{planted_unused2}"

    dead: list[str] = []
    unused_imports: list[str] = []

    for path in product_py:
        src = texts[path]
        rel = str(path.relative_to(ROOT))
        dead += [f"{rel}:{ln} {n}" for n, ln in _scan_dead_python_symbols(src, ast) if refs(n) <= 1]
        for lineno, name in _unused_imports(src, path, texts, ast):
            unused_imports.append(f"{rel}:{lineno} 导入了 {name} 但本文件没用到")

    decl_re = re.compile(
        r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|const|let|class|interface|type|enum)\s+([A-Za-z0-9_$]+)",
        re.M,
    )
    for path in web_ts:
        stripped = _strip_prose(path, texts[path])
        rel = str(path.relative_to(ROOT))
        for m in decl_re.finditer(stripped):
            name = m.group(1)
            if name.startswith("__") or name in {"App", "main"}:
                continue
            if refs(name) <= 1:
                dead.append(f"{rel}:{stripped[: m.start(1)].count(chr(10)) + 1} {name}")

    # 前端侧对照：注释里的名字不算引用，被引用的不报
    planted_ts = (
        "export function probeTsLive(): number {\n  return 1\n}\n\n"
        "function probeTsOrphan(): number {\n  return 2\n}\n\n"
        "export const probeTsConsumer = probeTsLive()\n\nvoid probeTsConsumer\n\n"
        "// probeTsOrphan 只出现在注释里\n"
    )
    ts_stripped = _strip_code_prose_ts(planted_ts)
    ts_dead = sorted({n for n in decl_re.findall(ts_stripped) if count_refs(n, ts_stripped) <= 1})
    assert ts_dead == ["probeTsOrphan"], f"前端死代码对照失败：{ts_dead}"

    # 第三类：文档声称某个 `module.func()` 存在，代码里却已经没这个名字了。
    # 死代码最阴的一种形态不是多占几 KB，而是让文档与代码互相作证一个不存在的能力。
    # CHANGELOG 不扫：它是历史记录，提已删名字是它的本职。
    doc_files = [p for p in _doc_files() if p.name != "CHANGELOG.md"]
    defined = {p.stem: texts[p] for p in product_py}
    stale_docs: list[str] = []
    for doc in doc_files:
        if not doc.is_file():
            continue
        for ln, token in _scan_stale_doc_refs(doc.read_text(encoding="utf-8", errors="ignore"), defined):
            stale_docs.append(f"{doc.relative_to(ROOT)}:{ln} {token}")

    planted_docs = _scan_stale_doc_refs(
        "见 `mesh.gone_probe_fn()`；`os.getcwd()` 不算（os 不是产品模块）；"
        "`mesh.sha256_of()` 也不算（它真的在）。",
        {"mesh": "def sha256_of(path):\n    return 0\n"},
    )
    assert [t for _, t in planted_docs] == ["mesh.gone_probe_fn()"], f"文档引用对照失败：{planted_docs}"

    # 第四类：`aiforge/__init__.py` 的「包结构」清单。上面那条 `_scan_stale_doc_refs` 只认
    # `module.func()` 这种点号形式，管不到清单里**光秃秃一个模块名**。实测漏的就是这个：
    # shortcuts.py 随两个没人调的端点删掉时，CHANGELOG 与竞争分析都改了，只有这张地图
    # 还留着「shortcuts   Windows 快捷方式」一行 —— 而它是新人读包的第一入口。
    pkg_init = ROOT / "src" / "aiforge" / "__init__.py"
    pkg_real = {p.stem for p in (ROOT / "src" / "aiforge").iterdir()
                if p.suffix == ".py" and p.name != "__init__.py"}
    # 子包按「目录里有 .py」算，不是按「有 __init__.py」算：api/ 就是一个隐式命名空间包
    # （没有 __init__.py，但 from .api import app_routes 一直在跑）。要求 __init__.py
    # 会把一个真在用的包当成不存在，那条清单行就会被自己的守卫误杀。
    pkg_real |= {d.name for d in (ROOT / "src" / "aiforge").iterdir()
                 if d.is_dir() and not d.name.startswith("__") and any(d.glob("*.py"))}
    assert len(pkg_real) >= 20, f"只枚举到 {len(pkg_real)} 个包内模块，这条检查的范围不对"
    pkg_map = _scan_package_map(pkg_init.read_text(encoding="utf-8"), pkg_real)

    planted_map = _scan_package_map(
        "包结构：\n    paths            真的在\n    shortcuts        已删的模块（就是实测漏的那个）\n"
        "    api/*            子包用通配写法\n        _probe_deep  缩进 8 空格，不是顶层清单行\n", {"paths", "api", "mesh"})
    assert planted_map == ["清单里的 `shortcuts` 在包里不存在", "`mesh` 没写进包结构清单"], \
        f"包结构清单对照失败：{planted_map}"

    # 第五类：随包分发的文档里指到的仓库文件，必须真的能在那个包里找到。
    # 这一类不是「文档写错个名字」那么轻：docs/PUBLISH-TO-GITHUB.md 曾经指着一个
    # `build\work\sync_github_repo.py`，那在 .gitignore 里，别人 clone 后照文档跑第一步就死；
    # 同一份文档还拿它当「发布工具」。发布流程的引用必须是**仓库里跑得起来的命令**。
    # 判据故意宽：只拿尾段在全仓兑现（`cd webui` 后写 `scripts\x.mjs` 是对的），
    # 所以它抓的是「彻底指不到」而不是「相对路径写得不够全」。
    internal_docs = _internal_doc_paths()
    ship_docs = [p for p in _doc_files() if p.relative_to(ROOT).as_posix() not in internal_docs]
    assert len(ship_docs) >= 12, f"只扫到 {len(ship_docs)} 篇随包文档，这条检查的范围不对"
    repo_paths = _repo_relpaths()
    # 文档里提**生成物**路径是正常的（`webui/dist/index.html`、`installer/Output/*.exe`），
    # 它们本来就是「跑一下构建才会有」，不入库也不进包。豁免只看目录前缀，
    # 且**只豁免非脚本**（否则 `build/work/*.py` 这种「发布工具自己不在包里」就漏掉了）。
    generated_prefixes = ("webui/dist/", "installer/Output/", "build/")
    assert len(internal_docs) >= 3, f"只读到 {len(internal_docs)} 个不随包分发的文档：{sorted(internal_docs)}"
    dangling: list[str] = []
    points_internal: list[str] = []
    for doc in ship_docs:
        # CHANGELOG 提已删名字是它的本职（和第三类同一个理由），但指内部文档不是。
        msgs = _scan_ship_pointers(
            doc.read_text(encoding="utf-8", errors="ignore"),
            internal_docs,
            repo_paths,
            exempt_missing=(doc.name == "CHANGELOG.md"),
            generated=generated_prefixes,
        )
        rel_doc = doc.relative_to(ROOT).as_posix()
        for m in msgs:
            # 两类分开报：「指到仓库里没有的文件」与「指到不入库的内部记录 / 它的代号」
            # 是两种病，后者是隐私与文档诚实问题，不是「路径写错了」。
            internalish = m.startswith("指向不随包分发") or m.startswith("提到内部记录的代号")
            (points_internal if internalish else dangling).append(f"{rel_doc} {m}")
    dangling = sorted(set(dangling))
    points_internal = sorted(set(points_internal))

    planted_ship = _scan_ship_pointers(
        "\n".join([
            r"跑 `scripts/gone_probe.py`；`scripts\live.py` 在包里；"
            r"`scripts\verify-flow.mjs` 在 cd webui 后跑，尾段能兑现；`webui\dist\index.html` 是生成物。",
            r"参照 `docs\INTERNAL-NOTE.md` §9.2。",
            r"详情见 `docs\INTERNAL-NOTE.md`（不随仓库分发）。",
            r"发布流程写的是跑 `build\work\sync_probe.py`。",
            r"取证见 `内部取证明细` §9.5。",
        ]),
        {"docs/INTERNAL-NOTE.md"},
        {"scripts/live.py", "webui/scripts/verify-flow.mjs", "docs/KEEP.md"},
        generated=("webui/dist/",),
    )
    assert planted_ship == [
        "指向仓里根本没有的 scripts/gone_probe.py（第 1 行）",
        "指向不随包分发的 docs/INTERNAL-NOTE.md（第 2 行）",
        # 带了「不随仓库分发」声明的那一行现在也报：诚实的括号挡不住读者打不开文件
        "指向不随包分发的 docs/INTERNAL-NOTE.md（第 3 行）",
        "指向仓里根本没有的 build/work/sync_probe.py（第 4 行）",
        "提到内部记录的代号「内部取证明细」（第 5 行）",
    ], f"文档路径对照失败：{planted_ship}"

    # 阴性对照：提一句「本地日志不入库」但不拿它当出处，不得被误报
    # （否则下一轮所有人都会为了避开这条守卫而把取证句子删干净）。
    clean_ship = _scan_ship_pointers(
        "跑 `scripts/live.py`；契约见 `docs/KEEP.md`；原始跑动日志不入库，所以不作出处。",
        {"docs/INTERNAL-NOTE.md"},
        {"scripts/live.py", "webui/scripts/verify-flow.mjs", "docs/KEEP.md"},
    )
    assert not clean_ship, f"合法写法被误报：{clean_ship}"

    # 两类问题一次全报：分两条断言时，死代码会遮蔽废导入（实测投毒时只看到
    # 前者，后者要等前者修干净才露出来），一轮约等于只查了一半。
    problems = []
    if dead:
        problems.append("这些顶层名字除了定义那一行再没有任何代码引用（要么接上、要么删掉）：" + "；".join(dead))
    if unused_imports:
        problems.append("产品代码里有导入却不用的名字（白增启动开销与体积）：" + "；".join(unused_imports))
    if stale_docs:
        problems.append("文档写着 `module.xxx()` 但产品里已经没这个名字：" + "；".join(stale_docs))
    if pkg_map:
        problems.append("包结构清单与真实模块不符：" + "；".join(pkg_map))
    if dangling:
        problems.append("随包分发的文档指向仓里找不到的文件（别人照文档跑就会死）：" + "；".join(dangling))
    if points_internal:
        problems.append("公开文档指着不随包分发的内部记录：" + "；".join(points_internal))
    assert not problems, "\n".join(problems)
    return (
        f"{len(product_py)} 个产品模块 + {len(web_ts)} 个前端文件的顶层名字全部有人引用，"
        f"{len(doc_files)} 篇文档里的 `module.func()` 引用全部对得上代码，"
        f"包结构清单与真实 {len(pkg_real)} 个模块逐条一致，"
        f"{len(ship_docs)} 篇随包文档里指到仓库的路径全部落得在地上（全仓可指 {len(repo_paths)} 个文件），"
        "且不拿不入库的内部记录当出处"
        "（含阳性对照与按文件判的未用导入检查）"
    )


_DOC_PATH_TOKEN = re.compile(
    r"(?<![\w./\\-])((?:src|webui|scripts|docs|installer|\.github|build)[\\/][\w.\-]+(?:[\\/][\w.\-]+)*"
    r"\.(?:py|ps1|mjs|md|json|iss|txt|tsx|ts|yml|html|spec|scad))"
)
# `build` 为什么要在前缀里：这条守卫要抓的头条缺陷就是「文档指着 `build\work\*.py`」，
# 不列 build 它连 token 都扫不到（实测投毒方向报 0 条 —— 一条什么都没抓到的守卫）。
# 下面 `_scan_ship_pointers` 里的生成物豁免只放非脚本，所以 `build\...\python.exe` 不会被误杀。

# 内部记录的**代号**（不是文件名，是文档里用来指它的那个名字）。为什么要一起禁：
# 「见 `内部取证明细` §9.5」连 `.md` 都没有，路径对照根本扫不到；上一轮就是把 §号 的
# 判据收紧才发现随包文档里有 32 处这种指针 —— 读者拿到的是一条永远打不开的出处。
_INTERNAL_ALIASES = ("内部取证明细",)


def _internal_doc_paths() -> set[str]:
    """不随包分发的内部记录清单（posix 相对路径）—— 只从 export_source.ps1 读。

    为什么不在这里再写一份：「什么是公开的」只能有一个定义。两边各写一份的话，
    漂移不会报错，只会一直漂到某人把内部记录推上公开仓库那天。
    """
    ps1 = (ROOT / "scripts" / "export_source.ps1").read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"\$internalDocs\s*=\s*@\(([^)]*)\)", ps1)
    assert m, "export_source.ps1 里没读到 $internalDocs 清单，这条检查等于没跑"
    names = set(re.findall(r"'([^']+?)'", m.group(1)))
    assert names, f"$internalDocs 里解出 0 个名字，这条检查等于没跑：{m.group(1)[:120]!r}"
    return {n.replace("\\", "/") for n in names}


def _repo_relpaths() -> set[str]:
    """仓库里**真的会被别人拿到**的文件（posix 相对路径）。

    口径以 `git ls-files -z` 为准，不再看本地磁盘：上一版用 `os.walk` 数文件，
    于是**开发机器上的生成物会让文档指针“合法”** —— 实测把镜像仓库克隆到
    项目外跑 `--offline`，开发机全绿的「随包文档指针」在克隆体里当场变红：
    两份文档指着 `webui/scripts/shots/contrast-report.json`，那是 gitignore 的本地产物，
    别人 clone 后根本打不开。（用 `-z` 而不按行解：否则非 ASCII 路径会被八进制转义，
    同一类「中文名文件静默漏扫」的坑。）

    git 不可用时退回目录剪枝的遍历（宁可少判也不假报）。
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"], cwd=str(ROOT), capture_output=True, timeout=120
        )
    except Exception:  # noqa: BLE001 - 没 git 也要能跑完自检
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout:
        names = {p.decode("utf-8", "replace") for p in proc.stdout.split(b"\0") if p}
        return {n for n in names if not n.startswith("webui/dist/")}

    import os

    pruned = {"build", "node_modules", ".git", "__pycache__", ".pytest_cache", ".venv", "venv"}
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in pruned]
        for fn in filenames:
            rel = (Path(dirpath) / fn).relative_to(ROOT).as_posix()
            if rel.startswith("webui/dist/"):
                continue
            out.add(rel)
    return out


def _scan_ship_pointers(
    text: str, internal: set[str], repo_paths: set[str], exempt_missing: bool = False,
    generated: tuple[str, ...] = (),
) -> list[str]:
    r"""一篇文档里的仓库路径引用与内部记录代号，哪些指不到东西。

    真检查与阳性对照共用这一份实现 —— 判据写成两份的话，对照验的是另一套规则，
    「对照通过」就不能证明真检查会报什么。

    只用尾段在全仓兑现（`p.endswith("/" + rel)`），不去推 `cd` 后的 cwd：文档里
    `cd webui` + `node scripts\x.mjs` 是对的，要求它写全路径只会逼人把文档改得更冗长。
    这条守卫抓的是「彻底指不到」与「指到不进包的内部记录」，不是「写得不够全」。
    """
    out: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for alias in _INTERNAL_ALIASES:
            if alias in line:
                out.append(f"提到内部记录的代号「{alias}」（第 {lineno} 行）")
        for tok in dict.fromkeys(_DOC_PATH_TOKEN.findall(line)):
            rel = tok.replace("\\", "/")
            if rel.startswith(generated) and not rel.endswith((".py", ".ps1", ".mjs")):
                # 生成物目录里的「非脚本」可以提（`build\venv\...\python.exe` 是解释器、
                # `webui\dist\index.html` / `installer\Output\*.exe` 是产物）；但**脚本不算**：
                # 指 `build\work\something.py` 就是指一个 clone 下来根本不会存在的代码。
                continue
            if rel in internal:
                # 「同行写了不随仓库分发」不再豁免：说明它不在包里仍然是一条别人打不开的
                # 出处。随包文档只能指随包分发的东西，改了这一条才不会出现
                # 「文档里每个指针都指向仓库外的东西」（1.0.3 那轮清掉 32 处内部指针时定下的口径）。
                out.append(f"指向不随包分发的 {rel}（第 {lineno} 行）")
            elif exempt_missing:
                continue
            elif not any(p == rel or p.endswith("/" + rel) for p in repo_paths):
                out.append(f"指向仓里根本没有的 {rel}（第 {lineno} 行）")
    return out


def _scan_package_map(init_text: str, real_names: set[str]) -> list[str]:
    """比对包 `__init__.py` 里「包结构」清单与真实模块名，双向都要对上。

    清单行的形状是「四个空格 + 名字 + （可选的 `/*` 子包写法）+ 至少两个空格 + 说明」。
    之所以不做「只查已列出的名字存不存在」的单向检查：
    少列一个新模块同样是这张地图骗人（实测这一轮就漏了 11 个新模块）。
    """
    import re

    out: list[str] = []
    listed_keys: set[str] = set()
    for m in re.finditer(r"^[ \t]{4}([a-z_][a-z0-9_]*)(?:/\*)?[ \t]{2,}\S", init_text, re.M):
        name = m.group(1)
        listed_keys.add(name)
        if name not in real_names:
            out.append(f"清单里的 `{name}` 在包里不存在")
    for name in sorted(real_names - listed_keys):
        out.append(f"`{name}` 没写进包结构清单")
    return out


def _scan_dead_python_symbols(src: str, ast_mod) -> list[tuple[str, int]]:
    """返回模块级「函数/类/大写常量」的名字与行号（带装饰器的跳过）。"""
    try:
        tree = ast_mod.parse(src)
    except SyntaxError:
        return []
    out: list[tuple[str, int]] = []
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, (ast_mod.FunctionDef, ast_mod.AsyncFunctionDef, ast_mod.ClassDef)):
            if node.decorator_list:  # 框架按装饰器注册，名字不会出现在调用处
                continue
            names.append(node.name)
        elif isinstance(node, ast_mod.Assign):
            names += [t.id for t in node.targets if isinstance(t, ast_mod.Name) and t.id.isupper()]
        elif isinstance(node, ast_mod.AnnAssign) and isinstance(node.target, ast_mod.Name):
            if node.target.id.isupper():
                names.append(node.target.id)
        out += [(n, node.lineno) for n in names if not n.startswith("__")]
    return out


def _scan_unused_imports(src: str, ast_mod) -> list[tuple[str, int]]:
    """返回「导入了但整个文件再也没出现」的名字（不含 __future__）。"""
    import re

    try:
        tree = ast_mod.parse(src)
    except SyntaxError:
        return []
    stripped = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    out: list[tuple[str, int]] = []
    for node in ast_mod.walk(tree):
        if not isinstance(node, (ast_mod.Import, ast_mod.ImportFrom)):
            continue
        if isinstance(node, ast_mod.ImportFrom) and node.module == "__future__":
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            binding = alias.asname or alias.name.split(".")[0]
            if binding == "annotations":
                continue
            if len(re.findall(r"(?<![\w$])" + re.escape(binding) + r"(?![\w$])", stripped)) <= 1:
                out.append((binding, node.lineno))
    return out


def _unused_imports(src: str, path, texts: dict, ast_mod) -> list[tuple[int, str]]:
    """本文件确实没用到、且不是「故意留着」的导入。返回 (行号, 名字)。

    为什么不能拿全仓引用次数当过滤器（上一版的坑）：一个导入只会在**它自己的文件**
    里被用；`import tempfile` 在别的文件出现一百次，也不能替本文件那一次没用到辩护。
    叠上全仓过滤器后，实测产品里 9 处废导入（tempfile/json/socket/time/field/
    Callable/extract_numbers/read_triangles/winforms）全部静默过关。

    豁免只有三种，每一种都要有**写在代码里的凭据**，不接受「名字在别处也出现过」：
      1. 那一行标了 `# noqa: F401`（只为副作用 / 只为探测存在而导入）；
      2. 名字在本模块 `__all__` 里（显式再导出）；
      3. 别的模块真的写着 `from .本模块 import … 名字`（隐式再导出有人依赖）。
    """
    import re

    try:
        tree = ast_mod.parse(src)
    except SyntaxError:
        return []

    exported: set[str] = set()
    for node in getattr(tree, "body", []):
        if not isinstance(node, (ast_mod.Assign, ast_mod.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, (ast_mod.List, ast_mod.Tuple)):
            continue
        targets = node.targets if isinstance(node, ast_mod.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast_mod.Name) and target.id == "__all__":
                exported |= {
                    e.value for e in value.elts if isinstance(e, ast_mod.Constant) and isinstance(e.value, str)
                }

    consumed: set[tuple[str, str]] = set()
    stmt_re = re.compile(r"from\s+[\w.]*\b(\w+)\s+import\s+(\((?:[^()]|\n)*\)|[^\n#]*)")
    for other_path, other in texts.items():
        if other_path == path:
            continue
        for m in stmt_re.finditer(other):
            for member in re.findall(r"[A-Za-z_]\w*", m.group(2)):
                consumed.add((m.group(1), member))

    lines = src.splitlines()
    stem = path.stem
    out: list[tuple[int, str]] = []
    for name, lineno in _scan_unused_imports(src, ast_mod):
        stmt_line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        if re.search(r"#\s*noqa:\s*F401\b", stmt_line):
            continue
        if name in exported or (stem, name) in consumed:
            continue
        out.append((lineno, name))
    return out


def _scan_stale_doc_refs(doc_text: str, defined: dict[str, str]) -> list[tuple[int, str]]:
    """文档里的 `module.member()`：module 是产品模块时，member 必须真在那个模块里。

    只认这种「点号加括号」的确定形式，所以第三方 API（`os.getcwd()`、`subprocess.run()`）
    因为 module 不在产品模块表里而天然跳过 —— 不需要维护白名单，也不会误杀外部文档。
    实测全仓 39 处这类引用，噪音 0（唯一一处就是本轮删掉的 openscad 签名函数）。
    """
    import re

    out: list[tuple[int, str]] = []
    for m in re.finditer(r"(?<![\w.])([a-z_][a-z0-9_]{2,})\.([A-Za-z_]\w*)\(\)", doc_text):
        stem, member = m.group(1), m.group(2)
        body = defined.get(stem)
        if body is None:
            continue
        if not re.search(r"(?<![\w$])" + re.escape(member) + r"(?![\w$])", body):
            out.append((doc_text[: m.start()].count("\n") + 1, f"{stem}.{member}()"))
    return out


def _doc_files() -> "list[Path]":
    """会被扫描的文档集（`module.func()` 引用与 `§号` 引用共用一份清单）。

    两处各写一份清单，早晚会一份多一份少 —— 那等于守卫在盯着一张没人维护的列表。
    这里**含** CHANGELOG：章节引用对历史记录同样成立（它只豁免「提到已删函数」这一类）。
    """
    wanted = [
        *sorted((ROOT / "docs").glob("*.md")),
        ROOT / "README.md",
        ROOT / "README.en.md",
        ROOT / "README-开发者.md",
        ROOT / "README-用户.md",
        ROOT / "SECURITY.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "CODE_OF_CONDUCT.md",
        ROOT / "CHANGELOG.md",
        ROOT / "scripts" / "probes" / "README.md",
    ]
    return [p for p in wanted if p.is_file()]


_DOC_HEADING = re.compile(r"(?m)^#{1,4}\s*(\d+(?:\.\d+)?)\.?\s")
_DOC_REF = re.compile(r"§\s*(\d+(?:\.\d+)?)")
_DOC_NAME_BEFORE = re.compile(r"[\w\-./]*\.md")
# 按名字指自己人写的章节：见下面「XXX」段。写错名字时它比 §号更阴 —— 没人会去搜。
_DOC_NAMED_REF = re.compile(r"「([^」\n]{2,30})」(?:段|小节)")
_DOC_HEADING_TEXT = re.compile(r"(?m)^#{1,6}\s+(.+?)\s*$")


def _heading_titles(text: str) -> set[str]:
    """标题正文，去掉 markdown 记号与空白 —— 只用来做包含判断，宁可宽不要误伤。"""
    out: set[str] = set()
    for m in _DOC_HEADING_TEXT.finditer(text):
        title = m.group(1).replace("`", "").replace("*", "").strip()
        out.add(title)
        out.add(title.replace(" ", ""))
    return out


def _named_title_exists(name: str, titles: set[str]) -> bool:
    want = name.replace(" ", "").strip()
    return any(want and (want in t or want in t.replace(" ", "")) for t in titles)


def _scan_named_section_refs(
    text: str, own_name: str, titles_of: dict[str, set[str]]
) -> list[tuple[int, str]]:
    """每一条「X」段 / 「X」小节：X 必须是本文件（或引用处前 60 字写明的那份 .md）的真标题。"""
    out: list[tuple[int, str]] = []
    own = titles_of.get(own_name, set())
    for m in _DOC_NAMED_REF.finditer(text):
        name = m.group(1).strip()
        if _named_title_exists(name, own):
            continue
        ln = text[: m.start()].count("\n") + 1
        named = _DOC_NAME_BEFORE.findall(text[max(0, m.start() - 60): m.start()])
        if not named:
            out.append((ln, f"「{name}」段 在本文件里找不到这个标题，引用处前 60 字内也没写是哪份 .md"))
            continue
        key = Path(named[-1]).name
        titles = titles_of.get(key)
        if titles is not None and _named_title_exists(name, titles):
            continue
        where = "那里没有这个标题" if titles is not None else "但它不在扫描集里（文件名写错了？）"
        out.append((ln, f"「{name}」段 指向 {key}，{where}"))
    return out


def _scan_section_refs(text: str, own_name: str, heads_of: dict[str, set[str]]) -> list[tuple[int, str]]:
    """每一条 `§N` / `§N.M`：或者就是**本文件**的编号标题，或者引用处前面写了是哪份 `.md`。"""
    out: list[tuple[int, str]] = []
    own = heads_of.get(own_name, set())
    for m in _DOC_REF.finditer(text):
        ref = m.group(1)
        if ref in own:
            continue
        ln = text[: m.start()].count("\n") + 1
        named = _DOC_NAME_BEFORE.findall(text[max(0, m.start() - 60): m.start()])
        if not named:
            out.append((ln, f"§{ref} 在本文件里没有这个编号标题，引用处前 60 字内也没写是哪份 .md"))
            continue
        key = Path(named[-1]).name
        heads = heads_of.get(key)
        if heads is not None and ref in heads:
            continue
        where = "那里没有这个编号标题" if heads is not None else "但它不在扫描集里（文件名写错了？）"
        out.append((ln, f"§{ref} 指向 {key}，{where}"))
    return out


def _scan_heading_order(text: str) -> list[tuple[int, str]]:
    """编号标题必须按升序出现，子节的父号必须就是当前那节 —— 顺序倒了读者会先撞见后一节。

    「节」是文里**最浅**的那一层编号标题：有人写 `## 1.`（COMPETITIVE-GAPS），
    有人写 `# 1.` + `## 1.1`（USER_GUIDE）。写死成 `##` 会把后者整篇误报成孤儿子节
    —— 这个误伤在首跑就撞上了（33 条），所以它现在也是一条阴性对照。
    """
    found = list(re.finditer(r"(?m)^(#{1,4})\s*(\d+)(?:\.(\d+))?\.?\s", text))
    if not found:
        return []
    top_level = min(len(m.group(1)) for m in found)
    out: list[tuple[int, str]] = []
    top = 0
    for m in found:
        level, first, second = len(m.group(1)), int(m.group(2)), m.group(3)
        ln = text[: m.start()].count("\n") + 1
        if level == top_level:
            if first < top:
                out.append((ln, f"{m.group(0).strip()} 排到了上一节（#{top_level} {top}.）后面，章节顺序倒了"))
            top = max(top, first)
        elif second is not None and first != top:
            out.append((ln, f"{m.group(0).strip()} 的父号不是当前那节（#{top_level} {top}.）"))
    return out


def t_doc_section_refs() -> str:
    """文档章节引用（`§号` 与「按名字指的段」）必须指得到真标题，编号章节不得倒着排。

    为什么补这一条：本轮写文档时先写下了 `§9.1`/`§9.3` 的前向引用，章节本身却还没落地；
    同一篇文档的 `## 8` 又物理排在 `## 6` 前面 —— 都是「文档骗人」，和已删函数的引用同一类。
    已有的守卫只盯 `module.func()` 这种点号形式，管不到 `§号`。靠人记得查不如写成守卫。

    名字引用是同一轮里抓出来的第二种句式：CHANGELOG 写了「见下面『守卫与验收的实测数据』段」
    而那个标题根本不存在 —— `§号` 的正管不到它，因为它是按中文标题指人的。
    """
    docs = _doc_files()
    bodies = {p: p.read_text(encoding="utf-8", errors="ignore") for p in docs}
    heads_of = {p.name: set(_DOC_HEADING.findall(t)) for p, t in bodies.items()}
    titles_of = {p.name: _heading_titles(t) for p, t in bodies.items()}

    findings: list[str] = []
    scanned = 0
    scanned_named = 0
    for p in docs:
        text = bodies[p]
        scanned += len(_DOC_REF.findall(text))
        scanned_named += len(_DOC_NAMED_REF.findall(text))
        for ln, why in _scan_section_refs(text, p.name, heads_of):
            findings.append(f"{p.relative_to(ROOT)}:{ln} {why}")
        for ln, why in _scan_named_section_refs(text, p.name, titles_of):
            findings.append(f"{p.relative_to(ROOT)}:{ln} {why}")
        for ln, why in _scan_heading_order(text):
            findings.append(f"{p.relative_to(ROOT)}:{ln} {why}")

    # 双向对照（阴性在前）：合法写法不得报，否则这条守卫每轮都在制造假工作。
    fake_heads = {"probe.md": {"1", "2", "2.1"}, "USER_GUIDE.md": {"2.2"}}
    clean = _scan_section_refs(
        "本节见 §2.1；另见 §1。跨文件要写全：docs/USER_GUIDE.md §2.2。",
        "probe.md",
        fake_heads,
    )
    assert not clean, f"合法引用被误报：{clean}"
    dirty = _scan_section_refs(
        "悬空的前向引用 §9.1；指向别的文档但那里没有：docs/USER_GUIDE.md §7.7；"
        "没写文件名：§40.4；写对了的：§2。",
        "probe.md",
        fake_heads,
    )
    assert [ln for ln, _ in dirty] == [1, 1, 1], f"投毒没被全抓到（应 3 条）：{dirty}"
    order_bad = _scan_heading_order("## 1. a\n## 3. b\n## 2. c\n")
    assert len(order_bad) == 1 and "顺序倒了" in order_bad[0][1], f"章节倒序没抓到：{order_bad}"
    assert not _scan_heading_order("## 1. a\n### 1.1 x\n## 2. b\n### 2.3 y\n"), "正常编号被判成倒序"
    assert _scan_heading_order("## 1. a\n### 2.1 x\n"), "孤儿子节没抓到"
    # H1 当节、H2 当子节（USER_GUIDE 的写法）：不得误报，但真挂错了要报
    assert not _scan_heading_order("# 1. a\n## 1.1 x\n# 2. b\n## 2.9 y\n"), "一级标题当节时被误伤"
    assert len(_scan_heading_order("# 1. a\n## 2.1 x\n")) == 1, "一级标题下的孤儿子节没抓到"

    # 按名字指章节：只抽了一种句式，不抽就等于没守（本轮真写过一条悬空的）
    fake_titles = {"probe.md": _heading_titles("## 9. 守卫\n### 9.2 单实例锁的实测\n")}
    clean_named = _scan_named_section_refs(
        "机制见下面「守卫」段；子节写「单实例锁的实测」小节；跨文件：docs/USER_GUIDE.md 的「安装」段。",
        "probe.md",
        {**fake_titles, "USER_GUIDE.md": _heading_titles("## 安装\n")},
    )
    assert not clean_named, f"合法的名字引用被误报：{clean_named}"
    dirty_named = _scan_named_section_refs(
        "见下面「守卫与验收的实测数据」段；跨文件指错：docs/USER_GUIDE.md 的「没写过」段。",
        "probe.md",
        fake_titles,
    )
    assert len(dirty_named) == 2, f"悬空的名字引用没全抓到（应 2 条）：{dirty_named}"

    assert scanned >= 40, f"只扫到 {scanned} 处 §引用，多半是正则或文档集坏了（空跑的守卫最危险）"
    assert scanned_named >= 1, (
        f"扫到 {scanned_named} 处「X」段引用，正则或文档集坏了 —— 从没跑过样本的守卫比没守卫更危险"
    )
    assert not findings, "文档里有指不到标题的引用：\n" + "\n".join(findings)
    return (
        f"{len(docs)} 篇文档 / {scanned} 处 §引用 + {scanned_named} 处按名字指的「X」段全部指得到真标题，"
        "编号章节无倒序（含 §号悬空、§号跨文件指错、名字指错、孤儿子节四类投毒与两类阴性对照）"
    )


def t_design_mechanism() -> str:
    """运行「设计规格与改动校验」的专项自检（确定性，不调用 AI）。

    这套机制单独放在 selfcheck_design.py 里便于针对性运行；
    这里把它纳入构建流程，确保「只改该改的」在每次构建时都被验证过 ——
    它直接决定「改完会不会变差」，是产品体验的核心。

    跑法接在 `_design_run()` 上（结果缓存）：「文档里的自检项数与真源一致」要用
    它**真跑出来的** `[OK]` 条数当尺子，两处各跑一遍就是白白多等 3 秒。
    """
    rc, output = _design_run()
    if rc != 0:
        tail = "\n".join(output.strip().splitlines()[-12:])
        raise AssertionError("设计规格与改动校验自检未通过：\n" + tail)
    return f"{output.count('[OK]')} 项设计机制检查全部通过"


def t_installer_metadata() -> str:
    """安装器必须带上品牌图标与版本元数据（否则在系统里就是"无名软件"）。

    版本号这里额外钉一件事：**只能有一个来源**。实测漂移过 —— version.py 已经是
    1.0.1、.iss 还写着 1.0.0，于是「程序和功能」里、exe 属性里都是错版本，
    而用户报障时报的也是这个号（1.0.1 的修复因此被多对了一个版本）。
    现在 version.py 是唯一事实来源，.iss 里那个只在手编时生效的兜底值必须「一眼假」：
    填一个看着像发布版的数字，手编就会带着错号出厂且没人发现。
    """
    iss = (ROOT / "installer" / "AI-3D-Forge.iss").read_text(encoding="utf-8")
    for directive in (
        "SetupIconFile=",
        "UninstallDisplayIcon=",
        "UninstallDisplayName=",
        "AppPublisher=",
        "VersionInfoProductName=",
        "LicenseFile=",
        "DefaultDirName=",
    ):
        assert directive in iss, f"安装器缺少 {directive}"
    # 图标文件必须真实存在，否则编译会失败
    icon = ROOT / "build" / "AI-3D-Forge.ico"
    icon_note = ""
    if icon.is_file():
        assert icon.stat().st_size > 0, f"图标是空文件：{icon}"
    else:
        # 这个 .ico 是 `make_icon.py` 在构建时生成的（`build/` 不入库），
        # 所以新 clone / CI 上它本来就应当不存在 —— 拿它当失败就是假红。
        # 真正必须存的是**入库的那份**（LicenseFile），下面单独查。
        icon_note = f"（{icon.name} 由构建生成，本仓库未构建：只核对 .iss 里的引用）"

    # ---- 版本单一来源 ----
    import re

    ps1 = (ROOT / "scripts" / "build_installer.ps1").read_text(encoding="utf-8")
    assert "src\\aiforge\\version.py" in ps1, "build_installer.ps1 不再从 version.py 读版本"
    assert "/DAppVersion=$AppVersion" in ps1, "build_installer.ps1 没把版本传给 ISCC（手编会拿到兜底值）"

    from aiforge.version import APP_VERSION

    fallback = re.search(r'#ifndef\s+AppVersion\s*\n\s*#define\s+AppVersion\s+"([^"]*)"', iss)
    assert fallback, (
        "`.iss` 里 `#ifndef AppVersion` 的结构变了：没有这层保护，"
        "命令行传进来的版本会被文件里的 #define 静默顶掉（就是当初漂移的成因）"
    )
    sentinel = fallback.group(1)
    assert sentinel != APP_VERSION, (
        f".iss 的兜底版本又变成了真实版本号 {sentinel}：下次升版就会漂"
    )
    assert re.fullmatch(r"0\.0\.0-[^\"\s]+", sentinel), (
        f".iss 的兜底版本是 {sentinel!r}，不是一眼假的占位号（应为 0.0.0-hand-compile）"
    )
    return (
        f"图标/发布者/卸载项/许可证/默认目录均已声明；版本单一来源 version.py={APP_VERSION}，"
        f".iss 兜底为 {sentinel}{icon_note}"
    )


def t_paths() -> str:
    from aiforge.paths import paths

    files = paths(refresh=True).ensure()
    for directory in (files.data_dir, files.projects_dir, files.log_dir, files.runtime_dir):
        assert directory.exists(), directory
    ok, message = files.is_writable(files.projects_dir)
    assert ok, message
    return f"数据目录与项目目录均可用"


def _git_ignored(rel_paths: "list[str]") -> "tuple[set[str], str]":
    """问 git「这些相对路径会不会被忽略」，返回（被忽略集合, 状态说明）。

    为什么需要它：`_shipped_text_files()` 的注释写着「口径必须和 .gitignore 一致」，
    实现却是一份硬编码目录黑名单 —— 本轮新出现的 `.ai-memory/`（工具工作记录，
    里面写的就是本机绝对路径）黑名单里没有，于是两个守卫同时把它当提交集扫：
    一个报 CRLF、另一个本该报本机路径泄漏。拿 git 当单一事实源，
    以后任何新增的忽略目录都不会再跑错范围（也正是发布流程 `git add -A` 用的同一套规则）。

    **状态必须带原因**：第一版只 `return None` 不说明原因，于是「本文件没导入 subprocess」
    的 NameError 被伪装成「git 不可用」，默默退回了黑名单。降级可以，但不能拿一个
    假的解释代替真的 bug（宽 `except` 抹掉原因就是这个效果）。
    """
    import subprocess

    if not rel_paths:
        return set(), "没有待判定的路径"
    # 两个必须同时做对的细节（都是量出来的，不是推测）：
    # 1) `-c core.quotePath=false`：git 默认把非 ASCII 路径**转义加引号**输出
    #    （`"build/work/__\345\217\243...__.md"`），拿它去做集合比对就全部对不上。
    #    上一轮「`git ls-files` 的八进制转义让两份中文名文档从未被密钥扫描」
    #    就是同一个坑的另一个版本，这次是它回过来咬了我的新代码。
    # 2) 走**字节**而不是 text=True：文本模式会把输入的 '\n' 翻译成 '\r\n'，
    #    git 再带着那个 `\r` 把路径输出回来（Windows 管道实测）。
    payload = ("\n".join(rel_paths) + "\n").encode("utf-8", "surrogateescape")
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotePath=false", "check-ignore", "--stdin"],
            cwd=str(ROOT),
            input=payload,
            capture_output=True,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001 - 降级可以，但不能不说原因
        return set(), f"git 不可用（{type(exc).__name__}: {exc}）"
    if proc.returncode not in (0, 1):
        # 0=有命中、1=全没命中，其他是 git 自己出错（不在仓库里等）
        return set(), (
            f"git 执行失败（rc={proc.returncode}）："
            f"{(proc.stderr or b'').decode('utf-8', 'replace').strip()[:120]}"
        )
    text = (proc.stdout or b"").decode("utf-8", "replace")
    return {ln.strip() for ln in text.splitlines() if ln.strip()}, "ok"


def _shipped_text_files() -> "list[Path]":
    """仓库里「会进版本库 / 源码包」的文本文件。

    口径必须和 `.gitignore`、`scripts\\export_source.ps1` 一致：
    build/（venv、缓存、自检临时数据、打包产物）、node_modules、dist、
    界面截图与体检报告都是**生成物**，既不该被扫描，也本来就不会被提交。

    黑名单只当 **git 不可用时的兜底**；正常运行时走 `_git_ignored()`，
    否则每新增一个忽略目录都要记得改这里 —— 而这件事一漏就是误报或漏报。
    """
    unshipped_dirs = {
        "build", "node_modules", "dist", "dist-ssr", ".git", ".vscode", ".idea",
        ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
        "Output", "shots", ".deleted", ".ai-memory",
    }
    text_exts = {
        ".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".css", ".html", ".json", ".md",
        ".txt", ".ps1", ".bat", ".cmd", ".iss", ".yml", ".yaml", ".toml", ".cfg",
        ".spec", ".scad", ".in", ".example",
    }
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in unshipped_dirs for part in path.parts):
            continue
        if path.name in (".gitignore", ".gitattributes") or path.suffix.lower() in text_exts:
            files.append(path)
    files = sorted(files)
    ignored, _git_note = _git_ignored([p.relative_to(ROOT).as_posix() for p in files])
    if ignored:
        files = [p for p in files if p.relative_to(ROOT).as_posix() not in ignored]
    return files


def t_source_encoding() -> str:
    """提交集里的文本文件不能有 BOM、替换字符、GBK 乱码，行尾还得统一。

    为什么必须自动化：编码损坏是**静默**的 —— 代码照样能跑、测试照样能过，
    只有用户看到乱码报错时才暴露。实测踩过三轮：
      * 用 PowerShell 的 `Get-Content | Set-Content` 往返一个 .py 文件，
        PS 5.1 按 ANSI 代码页读 UTF-8，中文全变乱码，而且行尾的中文字节被吃掉、
        把 16 行**合并**了 7 处（306 行变 290 行）—— 代码语义已经错了；
      * 文档里一行示例路径被写成了 U+FFFD（原字节不可恢复）；
      * 一次工具写入把 27 个提交文件写成了 CRLF 行尾（仓库约定是 LF），
        代码照跑、测试照过，只有 diff 满屏空白变动时才被人看见。
    三次都是「看着像成功、实际已损坏」，只有扫描才能挡住。

    扫描范围必须等于「会被 `git add -A` 提交的范围」：上一版拿硬编码黑名单自己划，
    新的忽略目录（本轮的 `.ai-memory/`）一出现就把工具工作记录当成提交集扫，当场误报。
    下面第 0 段把这个口径本身钉住（双向：忽略区必须被排除、随包文件必须在集合里）。
    """
    # 0) 口径校验：拿 git 的忽略规则当单一事实源，而不是再维护一份名单
    probe = ["build/work/__口径探针__.md", "README.md", "docs/HANDOVER.md"]
    ignored, git_note = _git_ignored(probe)
    scope_note = "扫描范围按 git 忽略规则取（与 `git add -A` 同源）"
    if git_note != "ok":
        # 不硬失败：没 git 的环境（某些容器）也得能跑完自检。但原因必须出现在结果里：
        # 上一版只留一句「git 不可用」，把自己漏导入 subprocess 的 NameError 抹成了静默降级。
        scope_note = f"git 忽略规则不可用，退回目录黑名单；原因：{git_note}"
    else:
        assert "build/work/__口径探针__.md" in ignored, (
            f"忽略判定失效（{sorted(ignored)}）：build/ 下的工具文件会被当成提交集，"
            "本轮误报就是这么来的"
        )
        assert "README.md" not in ignored and "docs/HANDOVER.md" not in ignored, (
            f"随包文档被误当成忽略：{sorted(ignored)} —— 口径反了会「扫不到东西却报全绿」"
        )
        listed = {p.relative_to(ROOT).as_posix() for p in _shipped_text_files()}
        assert "README.md" in listed and "docs/HANDOVER.md" in listed, (
            f"随包文档不在扫描集合里（共 {len(listed)} 个），这条守卫就是空跑"
        )
    # 这些字符只在「GBK 误读 UTF-8 中文」的产物里成堆出现，正常中文里几乎见不到。
    # 刻意用码点构造而不是字面量：否则本文件自己就带着一堆疑似乱码字符，
    # 规则会被自己绊倒（这个坑在写这条规则时当场踩到过）。
    marks = {
        chr(cp)
        for cp in (
            0x6D93, 0x9286, 0x951B, 0x6769, 0x9422, 0x7487, 0x7D1D, 0x9359,
            0x579A, 0x6924, 0x6D30, 0x6D60, 0x71B8, 0x93C8, 0x572D, 0x93B4,
            0x6D63, 0x935A, 0x7AF4, 0x9428, 0x8BF2, 0x7481,
        )
    }

    problems: list[str] = []
    scanned = 0
    for path in _shipped_text_files():
        raw = path.read_bytes()
        rel = path.relative_to(ROOT).as_posix()
        scanned += 1
        if raw[:3] == b"\xef\xbb\xbf":
            problems.append(f"{rel}: 文件带 UTF-8 BOM（本项目一律无 BOM）")
        # 行尾：.gitattributes 写的是 `* text=auto eol=lf`，所以提交集必须是 LF；
        # .bat/.cmd 反过来必须 CRLF（cmd.exe 对 LF 批处理有历史毛病）。
        # 注意只能查字节：`read_text()` 默认会把行尾归一化，CRLF 就这么溜过去了。
        has_crlf = b"\r\n" in raw
        if path.suffix.lower() in (".bat", ".cmd"):
            if not has_crlf:
                problems.append(f"{rel}: .bat/.cmd 应当用 CRLF（cmd.exe 的历史毛病），实际是 LF")
        elif has_crlf:
            problems.append(f"{rel}: 含 CRLF 行尾，仓库约定（.gitattributes eol=lf）是 LF")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            problems.append(f"{rel}: 不是合法 UTF-8（{exc}）")
            continue
        if "\ufffd" in text:
            problems.append(f"{rel}: 含替换字符 U+FFFD（原字节已丢失）")
        # 私用区字符在源码/文档里没有任何正当用途，出现即损坏
        pua = sorted({ch for ch in text if 0xE000 <= ord(ch) <= 0xF8FF})
        if pua:
            problems.append(f"{rel}: 含私用区字符 {[hex(ord(c)) for c in pua[:4]]}（典型编码损坏）")
        # 乱码特征字只能当**统计**判据用：文档里可能正当地引用一段乱码示例
        # （本文件自身的踩坑记录里就有一个）。所以要求两条同时成立：
        #   1) 至少 6 个**不同**的特征字；
        #   2) 它们占全文汉字的比例 ≥ 1%。
        # 实测分得很开：真损坏（205 个 / 22 种 / 占汉字 14.6%）
        # vs 文档里的示例（4 个 / 4 种 / 占汉字 0.019%），两边都差一个数量级以上。
        hits = marks & set(text)
        mark_count = sum(1 for ch in text if ch in marks)
        cjk_count = sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF)
        if len(hits) >= 6 and mark_count / max(cjk_count, 1) >= 0.01:
            problems.append(
                f"{rel}: 疑似 GBK 乱码（{len(hits)} 种特征字、占汉字 "
                f"{mark_count / max(cjk_count, 1):.1%}）{[hex(ord(c)) for c in sorted(hits)[:4]]}"
            )

    assert not problems, "；".join(problems)
    # 「扫了 0 个文件」必须当成失败，不能当通过。
    # 这条是实测补上的：把仓库 clone 到 `build/work/` 下面跑自检时，
    # 路径里带了 `build` 这个被跳过目录名，于是所有文件都被当成"不该提交的"跳过，
    # 结果这两条守卫显示「0 个提交文本文件：无 BOM…」并**通过** ——
    # 一条什么都没检查的守卫比没有守卫更糟，因为它给的是假安全感。
    assert scanned >= 20, (
        f"只扫到 {scanned} 个提交文本文件，明显不对"
        "（仓库是不是被放在 build/ 之类的被跳过目录下了？）"
    )
    return f"{scanned} 个提交文本文件：无 BOM、无 U+FFFD、无乱码字符、行尾统一（{scope_note}）"


def t_no_personal_paths() -> str:
    """提交集里不能出现本机绝对路径（开源的硬性要求）。

    为什么必须自动化：这类泄漏只有「别人 clone 之后」才暴露，
    而肉眼很难在 1800 行文档里发现一行本机路径 —— 实测就漏了三处
    （搭建命令、踩坑示例、目录树根）外加前端 mock 里 7 处真实账号名。
    规则**不能硬编码本机路径**（那样规则自己就是泄漏），
    所以用运行时推导：当前用户目录 + 当前仓库根目录。

    比较必须**忽略大小写**：Windows 路径不区分大小写，而盘符大小写取决于当时怎么写的。
    本轮实测：同一个仓库根路径只把盘符写成小写，旧判据**一条也不报**
    —— 别人 clone 后看到的仍然是本机绝对路径。
    这里刻意不把本机路径写成字面量（那本身就是泄漏），只描述形状。
    """
    home = Path.home()
    needles: list[tuple[str, str]] = [(str(ROOT), "本机仓库绝对路径")]
    if home.name and home.name.lower() not in {"public", "default", "all users"}:
        needles.append((str(home), "本机用户目录"))

    # JS/JSON 里的反斜杠是转义的（C:\\Users\\x），正斜杠写法也很常见，都要覆盖
    variants: list[tuple[str, str]] = []
    for needle, label in needles:
        variants.append((needle.lower(), label))
        variants.append((needle.replace("\\", "/").lower(), label))

    def leaks(text: str) -> str | None:
        """命中哪一类本机路径；没命中返回 None。全文小写比对（盘符大小写不算豁免）。"""
        low = text.lower()
        flattened = low.replace("\\\\", "\\")
        for needle, label in variants:
            if needle in low or needle in flattened:
                return label
        return None

    # 判据自己的双向对照：小写盘符必须被报，与本机无关的相对路径不能误报
    assert leaks(f"{str(ROOT)[0].lower()}{str(ROOT)[1:]}\\build\\work\\x.py") == "本机仓库绝对路径", (
        "本机路径判据对「盘符小写」失效：Windows 路径不区分大小写，"
        "写 `d:\\…` 就能把仓库绝对路径递进公开仓库"
    )
    assert leaks("build\\\\work\\\\testdata\\\\x.py") is None, "相对路径被误报成本机绝对路径"

    problems: list[str] = []
    scanned = 0
    for path in _shipped_text_files():
        rel = path.relative_to(ROOT).as_posix()
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        label = leaks(text)
        if label:
            problems.append(f"{rel}: 含{label}")

    assert not problems, "；".join(problems)
    assert scanned >= 20, (
        f"只扫到 {scanned} 个提交文本文件，明显不对"
        "（仓库是不是被放在 build/ 之类的被跳过目录下了？）"
    )
    return f"{scanned} 个提交文本文件：没有本机用户目录 / 仓库绝对路径"


def t_oss_files() -> str:
    """开源文件完整性（必需文件 / README 要点 / 交叉链接）。

    这两支守卫单独成脚本是为了能在 ubuntu 的 CI 上跑（不依赖 OpenSCAD/Windows），
    自检里只是把它们也拉进来跑一遍，避免"CI 有、本地没有"的盲区。
    """
    import subprocess as _subprocess

    proc = _subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_oss_files.py"), "--quiet"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, "开源文件不完整：\n" + output.strip()[-800:]
    return "必需文件齐全、README 要点覆盖、相对链接不断链"


def t_no_secrets() -> str:
    """密钥 / 令牌 / 口令 / Cookie / 本机路径扫描（当前文件集）。"""
    import subprocess as _subprocess

    proc = _subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_no_secrets.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, "发现敏感信息或不该提交的文件：\n" + output.strip()[-800:]
    scanned = ""
    for line in output.splitlines():
        if "已扫描" in line:
            scanned = line.strip()
    return f"未发现密钥/令牌/口令/本机路径（{scanned or '已扫描提交集'}）"


def t_secret_scanner_not_blind() -> str:
    """密钥扫描器本身不许"瞎"：中文名文件必须真的进了扫描集合。

    为什么单独一条：`check_no_secrets.py` 是开源前最后一道密钥闸门，而它曾经
    对两份中文名文档（`README-开发者.md` 188KB、`README-用户.md`）**完全失明** ——
    `git ls-files` 默认 `core.quotepath=true` 会把非 ASCII 路径转义成
    `"\\346\\226\\207..."`，还原出的 Path 在磁盘上不存在，`is_file()` 为假，
    于是被当作"不是文件"静静跳过。扫描照样打印"已扫描 180 个文本文件"、
    结论照样是 NO_SECRET_FOUND —— 写在那两份文件里的真 Key 能一路绿灯发出去。

    这类缺陷的特征就是**静默**，所以只能靠断言把它钉住，不能靠"跑一遍没报错"。
    这里做双向验证（阴性在前，防止把合法写法判成错）：
      * 阳性 —— 磁盘上真实存在的中文名文件，出现在 `shipped_files()` 里；
      * 阴性 —— 八进制转义形式的路径**不该**被当成已存在的文件。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import check_no_secrets as cns
    finally:
        sys.path.pop(0)

    # 阴性对照先跑：转义路径在磁盘上不存在，不该混进扫描集合。
    escaped = '"2\\346\\234\\21020\\346\\227\\245(1)/x.mp4"'
    assert not (ROOT / escaped).is_file(), (
        f"转义路径竟然存在于磁盘上，_git() 的解码假设已变：{escaped}"
    )

    scanned_names = {p.name for p in cns.shipped_files()}
    chinese_docs = [
        p for p in sorted(ROOT.glob("*.md")) if not p.name.isascii()
    ]
    # 阴性对照：仓库里确实有中文名文档（本项目有），否则这条断言是空跑的。
    assert chinese_docs, "根目录一份中文名 .md 都没有 —— 仓库结构变了，这条守卫失去意义"
    missing = [p.name for p in chinese_docs if p.name not in scanned_names]
    assert not missing, (
        "这些中文名文件没进密钥扫描集合（core.quotepath 又变回 true 了？）："
        + "、".join(missing)
    )
    # 投毒对照：把一条八进制转义路径塞进去，必须认不出来（否则守卫是常亮的）。
    assert not (ROOT / escaped).exists(), "投毒样本竟真实存在"

    return (
        f"{len(chinese_docs)} 份中文名文档（{'、'.join(p.name for p in chinese_docs)}）"
        f"都在扫描集合内；转义路径不会被误当成文件"
    )


# 一次都不许进仓库的扩展名：视频 / 压缩包 / 可执行 / 光盘镜像。
# 产品截图（.png/.jpg）不在此列 —— 它们是该发布的资产，只放 docs/images/。
HEAVY_EXTS = (
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv",
    ".zip", ".7z", ".rar", ".gz", ".xz", ".iso",
    ".exe", ".dll", ".msi",
)


def t_add_a_wont_grab_heavy_files() -> str:
    """`git add -A` 会收进来的那个集合，不许出现大文件与视频/压缩包/可执行。

    为什么单独一条：`docs/PUBLISH-TO-GITHUB.md` 规定的提交步骤就是 `git add -A`。
    本仓库根目录出现过一份与项目无关的 `2月20日(1)/`（43MB 的 mp4 加一张封面 jpg），
    一次 `git add -A` 就能把它永久写进 git 历史 —— 之后 clone 的人得连这份视频一起
    下载，而 git 历史里的东西删不干净（要 filter-repo + 强推）。上一轮把规则补进了
    `.gitignore`，注释里自称「三道一起上：这里忽略、路径守卫拦、导出白名单拦」，
    可 `grep selfcheck.py` 找不到任何一项量过 git 这道：**规则有没有真生效没人验证**。
    少一个前导斜杠、目录名打错、被后面某条 `!` 反选覆盖、或者干脆把这一段挪到
    `build/` 之类规则前面 —— 它都会静默失效，而失效的表现是「什么都没发生」，
    直到某次提交才炸。

    两种量法互相补位：
      * 实测集 —— 直接量 `check_no_secrets.shipped_files()`。它就是「git add -A 会
        收进来的集合」（`--cached --others --exclude-standard`，自动尊重 .gitignore，
        且带 `core.quotepath=false`），所以不用真去动索引；判体积与扩展名。
      * 假想路径 —— 用 `git check-ignore --stdin` 问**规则本身**。工作区里现在
        可能没有那份视频（用户会删），但规则还得对着未来的落盘生效，所以拿不存在的
        路径去问；这一支不依赖任何文件真的存在。

    阈值全部按实测写，不拍脑袋：当前提交集 189 个文件 / 约 3.6 MB，最大单项
    就是 `scripts/selfcheck.py` 自己（约 0.5 MB），其中媒体只有 `docs/images/` 三张
    产品截图。所以单项上限 2 MB（约 4 倍余量）、总量上限 12 MB（约 3 倍余量）——
    既拦得住 43MB 的视频，也不会把一次正常的大型重构判成失效。
    """
    import subprocess as _sp

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import check_no_secrets as cns
    finally:
        sys.path.pop(0)

    max_file = 2 * 1024 * 1024
    max_total = 12 * 1024 * 1024

    files = cns.shipped_files()
    # 防空转：集合本身不对（git 没跑起来、退回了目录遍历）时，下面的「0 处违规」毫无意义。
    assert len(files) >= 120, (
        f"提交集只有 {len(files)} 个文件（实测 189）—— 尺子不对，"
        "先查 shipped_files() 是不是又对中文名/未跟踪文件失明了"
    )

    sizes = {p: (p.stat().st_size if p.is_file() else 0) for p in files}
    oversized = sorted(
        (f"{p.relative_to(ROOT).as_posix()} {sizes[p] / 1024 / 1024:.1f}MB"
         for p in files if sizes[p] > max_file)
    )
    assert not oversized, (
        "这些文件会随 `git add -A` 进仓库且超过 2 MB（实测最大约 0.5 MB，超限的基本都是"
        "物料/生成物）：\n  " + "\n  ".join(oversized)
        + "\n要么加进 .gitignore，要么改成产物目录；别靠 export 白名单兜 —— 那挡不住 git。"
    )
    total = sum(sizes.values())
    assert total <= max_total, (
        f"提交集总量 {total / 1024 / 1024:.2f} MB 超过 12 MB（实测约 3.6 MB）"
        "—— 仓库正在被当成网盘用，clone 的人会感觉到"
    )
    heavy = sorted(
        p.relative_to(ROOT).as_posix() for p in files
        if p.suffix.lower() in HEAVY_EXTS
    )
    assert not heavy, (
        "这些视频/压缩包/可执行文件会进仓库（实测提交集里一个都没有）："
        + "、".join(heavy)
    )

    def ignored(paths: list[str]) -> set[str]:
        """问 git：这些路径里哪些会被 .gitignore 挡掉（不碰工作区、不碰索引）。"""
        proc = _sp.run(
            # 这里不能用 -v：-v 会把**被 ! 反选救回来**的路径也打成一行
            # （第三列是 `!/*.mp4`），照“有输出就是被忽略”去解就会把
            # 反选失效当成规则生效。实测：加一行 `!/*.mp4` 后，-v 仍返
            # `probe-selfcheck.mp4`，不带 -v 就不返 —— 守卫第一版就是在这上面漏了。
            ["git", "-c", "core.quotepath=false", "check-ignore", "--stdin"],
            cwd=str(ROOT),
            # 必须是 bytes：text=True 会把 \n 翻译成 \r\n，git 收到的路径就带着 \r，
            # 于是 `/*.mp4` 匹配不上 `probe.mp4\r` —— 实测第一版就是这么假阴的。
            input="\n".join(paths).encode("utf-8"),
            capture_output=True,
            timeout=60,
            check=False,
        )
        hit: set[str] = set()
        for line in (proc.stdout or b"").decode("utf-8", "replace").splitlines():
            # 不带 -v 时每行就是一个被忽略的路径（可能带引号，例如含制表符/非 ASCII）
            name = line.strip().strip('"')
            if name:
                hit.add(name)
        return hit

    # 阳性对照：这些假想路径**必须**被忽略。名字一律取 probe- 前缀，绝不落到磁盘。
    must_ignore = [
        "probe-selfcheck.mp4",
        "probe-selfcheck.webm",
        "2月20日(1)/probe-inside.mkv",
        "build/probe-selfcheck.zip",
        "installer/Output/probe-selfcheck.exe",
    ]
    # 阴性对照：这些**必须不**被忽略 —— 忽略规则不许宽到吞掉正常资产
    # （产品截图就是最容易误伤的一类）。
    must_keep = [
        "docs/images/probe-selfcheck.png",
        "src/aiforge/probe_selfcheck.py",
        "scripts/probe-selfcheck.mjs",
        "README-用户.md",
    ]
    assert len(must_ignore) >= 4 and len(must_keep) >= 3, "对照集被改小了，这道闸门会被掏空"
    got = ignored(must_ignore + must_keep)
    leak = [p for p in must_ignore if p not in got]
    assert not leak, (
        ".gitignore 挡不住这些路径，`git add -A` 会真把它们收进去：" + "、".join(leak)
        + " —— 上一轮补的那几条媒体/私人目录规则已经失效（改坏了或压根没生效）"
    )
    over = [p for p in must_keep if p in got]
    assert not over, (
        "忽略规则误伤了正常资产，这些该提交的文件不会被 `git add -A` 收进去："
        + "、".join(over)
    )

    biggest = max(sizes, key=lambda p: sizes[p])
    return (
        f"提交集 {len(files)} 个文件 / {total / 1024 / 1024:.2f} MB，"
        f"最大单项 {sizes[biggest] / 1024:.0f} KB（{biggest.relative_to(ROOT).as_posix()}），"
        f"0 个视频/压缩包/可执行；{len(must_ignore)} 条假想媒体路径必被忽略、"
        f"{len(must_keep)} 条正常资产必不被忽略"
    )


def main() -> int:
    # 默认降低日志噪音：自检脚本会故意触发"AI 返回的不是合法 JSON"这类降级路径，
    # 那些是预期行为，不该在构建输出里看起来像错误。加 --verbose 可看完整日志。
    import logging

    global OFFLINE

    OFFLINE = "--offline" in sys.argv or os.environ.get("AIFORGE_SELFCHECK_OFFLINE") == "1"

    logging.basicConfig(
        level=logging.DEBUG if "--verbose" in sys.argv else logging.ERROR,
        format="%(levelname)s %(name)s: %(message)s",
    )

    print("=" * 70)
    print("AI 3D Forge 后端自检")
    if OFFLINE:
        print("（离线模式：跳过需要 OpenSCAD / 图形界面 / 网络的检查，跳过的项逐条列出）")
    print("=" * 70)

    section("模块与基础")
    check("模块导入", t_imports)
    check("无遮蔽的局部导入", t_no_shadowed_imports)
    check("模块内未定义名字", t_no_undefined_names)
    check("赋值了却没人读（算了不用）", t_no_dead_assignments)
    check("代码不养闲人（无死代码/无废导入）", t_no_dead_symbols)
    check("派生进程必走守护", t_no_unguarded_subprocess)
    check("subprocess 参数不重名", t_no_kwargs_collision)
    check("子进程输出不会被解码吃掉", t_subprocess_decoding)
    check("编译不报警告", t_no_syntax_warnings)
    check("文档章节引用指得到真标题", t_doc_section_refs)
    check("文档里的自检项数与真源一致", t_selfcheck_count_documented)
    check("路径与目录", t_paths)
    check("配置与密钥", t_config_and_secrets)
    check("外观材质与玻璃透明度", t_appearance_settings)

    section("安装与运行环境")
    check("WebView2 运行时检测", t_webview_runtime)
    check("应用图标资源", t_icon_assets)
    check("首个启动准备流程", t_first_run_setup)
    check("单实例锁（真两进程）", t_single_instance)
    check("打包依赖完整性", t_frozen_webview_deps)
    check("浏览器回退体验", t_browser_fallback_notice)
    check("构建脚本纯 ASCII", t_ps1_are_ascii)
    check("构建脚本不踩 stderr 终止坑", t_ps1_native_stderr)
    check("安装器元数据", t_installer_metadata)

    section("SCAD 处理")
    check("源码安全校验", t_scad_validation)
    check("参数提取与改写", t_scad_params)

    section("3D 引擎")
    # 引擎类检查放在最前面，理由只有一条：**它们需要 OpenSCAD 已经可用**。
    # 这条是「别人的机器上 clone 下来跑」实测暴露的：全新环境里没有 OpenSCAD，
    # 而下面是"拿 OpenSCAD 编译一个立方体再分析网格"的检查 ——
    # 它会以 `CompileError: 没有找到 OpenSCAD` 失败，用户看到的却是
    # 「STL 网格分析 失败」，像是代码有问题。自检应当像应用本身一样**先把
    # 环境准备好**（自动安装那一步本来就是干这个的，且已有安装时只花 1 秒复用）。
    check("OpenSCAD 自动安装", t_openscad_install)
    check("OpenSCAD 检测与 CLI", t_openscad_detect)
    check("STL 网格分析", t_mesh_analysis)
    check("STL 读取健壮性", t_stl_robustness)
    check("长分析心跳", t_analysis_heartbeat)
    check("声明间隙实测与判定降级", t_declared_gap_verdict)
    check("助手调用作用域", t_helper_job_scope)
    check("子进程不变量守护", t_child_process_guard)
    check("守护接线（启动即生效）", t_child_guard_wiring)
    check("守护存活双向保证", t_child_guard_survival)
    check("安装失败不破坏已有环境", t_install_no_destroy)
    check("换安装目录可回滚", t_install_swap_rollback)
    check("验收脚本不覆盖用户配置", t_install_test_keeps_config)
    check("SCAD → STL 编译", t_compiler)

    section("业务层")
    check("设计规格与改动机制", t_design_mechanism)
    check("配合尺寸不冒充整件外形", t_fit_scope_dimension)
    check("需求复述公布具体取值", t_announce_spec_values)
    check("规格名↔参数名改名桥", t_name_bridge)
    check("改动契约语义（只授权该改的）", t_contract_intent)
    check("整体缩放真的生效", t_user_scale)
    check("编译报错给出可执行改法", t_compile_advice)
    check("无控制台时诊断不崩", t_console_streams)
    check("GBK 控制台不会打断输出", t_console_encoding)
    check("高 DPI 工作区与窗口尺寸", t_dpi_workarea)
    check("网格质量采纳判据", t_mesh_quality_adoption)
    check("几何完整性自动修复", t_geometry_repair)
    check("回退历史版本", t_restore_revision)
    check("几何修复链路接线", t_repair_wiring)
    check("固定零件库可编译", t_parts_library)
    check("标准零件确定性直出", t_deterministic_part)
    check("GLM 提供商支持", t_glm_provider)
    check("孔径射线实测", t_hole_measure)
    check("孔径改动判据", t_hole_criterion)
    check("逐轴尺寸校正兜底", t_axis_dimension_fix)
    check("零厚度薄膜检测", t_sliver_detection)
    check("修改提示词携带补充说明", t_modify_prompt_clarifications)
    check("提供商分槽 Key 与经济档", t_provider_keys_and_economy)
    check("3MF 导出", t_3mf_export)
    check("切片软件检测规则", t_slicer_detect)
    check("底部入口链接可达", t_social_footer_links)
    check("需求可执行测试（PR/RS/IR）", t_requirement_tests)
    check("实心底与开口方向判定", t_solid_bottom_verdict)
    check("质量基准脚本结构", t_bench_prompts_script)
    check("螺纹识别（螺旋/同心环）", t_thread_detection)
    check("模型外观复核", t_visual_review)
    check("模块引用静态检查", t_module_aliases)
    check("依赖声明完整性", t_declared_dependencies)
    check("体积与依赖预算", t_bundle_budget)
    check("项目持久化", t_projects)
    check("环境检测报告", t_envcheck)
    check("作业与事件流", t_sse_contract)
    check("进度可信度", t_progress_accuracy)
    check("网络失败可读且可重试", t_network_errors)
    check("接口响应形状", t_contract_shapes)
    check("并发作业守卫", t_busy_guard)
    check("提示词与解析（离线）", t_llm_offline)

    section("打印机知识库")
    check("知识库结构与参数推导", t_printer_knowledge)
    check("自定义机型增删改", t_printer_custom)
    check("机型影响几何判定", t_printer_affects_analysis)
    check("约束注入 AI 提示词", t_printer_prompt_injection)
    check("打印机 HTTP 接口", t_printer_http)

    section("HTTP 接口")
    check("关键路由与安全拦截", t_http_api)
    check("接口面不臃肿", t_api_surface)

    section("开源与提交卫生")
    check("开源文件完整性", t_oss_files)
    check("敏感信息扫描", t_no_secrets)
    check("密钥扫描器没有漏扫中文名文件", t_secret_scanner_not_blind)
    check("git add -A 不捡大文件", t_add_a_wont_grab_heavy_files)
    check("源码编码完好", t_source_encoding)
    check("无本机路径泄漏", t_no_personal_paths)

    # 总结部分同样不能裸用 print：这里印的是失败明细（带任意异常消息），
    # 恰好是“只有崩了才会走到”的那一段 —— 它自己再崩一次就什么都看不到了。
    _SAFE_PRINT("\n" + "=" * 70)
    if SKIPPED:
        _SAFE_PRINT(f"通过 {len(PASSED)} 项，跳过 {len(SKIPPED)} 项，失败 {len(FAILED)} 项")
        _SAFE_PRINT("-" * 70)
        _SAFE_PRINT("  跳过的项（离线模式；它们**没有**被验证过）：")
        for name, reason in SKIPPED:
            _SAFE_PRINT(f"  ○ {name} —— {reason}")
        _SAFE_PRINT("  发布前请在不带 --offline 的环境跑完整版。")
    else:
        _SAFE_PRINT(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        _SAFE_PRINT("-" * 70)
        for name, error in FAILED:
            _SAFE_PRINT(f"  ✗ {name}: {error}")
    _SAFE_PRINT("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
