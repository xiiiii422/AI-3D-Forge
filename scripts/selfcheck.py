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

import os
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

# `--offline` 时跳过的检查：需要真实 OpenSCAD 二进制、Windows 注册表/图形界面，
# 或者会访问网络。CI（ubuntu / 无 OpenSCAD 的 windows）用这个模式。
# 注意：跳过的项会**单独计数并逐条打印原因**，绝不混进"通过"。
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
    "几何完整性自动修复": "需要真实几何数据（由 OpenSCAD 产出）",
    "固定零件库可编译": "需要对每个零件真实编译（依赖 OpenSCAD）",
    "模型外观复核": "需要渲染模型（依赖 OpenSCAD）",
    "快捷方式解析": "需要 Windows .lnk 解析",
    "接口响应形状": "部分断言依赖已构建的前端产物 webui/dist",
}


def check(name: str, fn) -> None:
    if OFFLINE and name in OFFLINE_SKIPS:
        reason = OFFLINE_SKIPS[name]
        SKIPPED.append((name, reason))
        print(f"  [SKIP] {name} —— {reason}（CI 离线模式；发布前请跑完整版）")
        return
    try:
        result = fn()
        PASSED.append(f"{name}" + (f" —— {result}" if result else ""))
        print(f"  [OK]   {name}" + (f" —— {result}" if result else ""))
    except Exception as exc:  # noqa: BLE001
        FAILED.append((name, f"{type(exc).__name__}: {exc}"))
        print(f"  [FAIL] {name} —— {type(exc).__name__}: {exc}")
        if os.environ.get("AIFORGE_SELFCHECK_TRACE") == "1":
            traceback.print_exc()


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------
def t_imports() -> str:
    import importlib

    modules = [
        "aiforge",
        "aiforge.paths",
        "aiforge.version",
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
        "aiforge.shortcuts",
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
    return "安全拦截（含 include/use 读文件）/ 参数提取 / BOM 剥离 均正确"


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
        normals, centroids, drop = mesh._transform_faces(axes, [], [sample], [sample])
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

    return "损坏 STL（NaN/inf/截断）明确报错、尾部竞态仍容忍、朝向变换语义逐位不变"


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

    def slowed(path, limits=None):
        # 50ms 的分析 + 10ms 心跳 = 至少 4 次心跳，确定性足够
        time_mod.sleep(0.05)
        return original(path, limits)

    compiler.analyze_stl = slowed  # type: ignore[assignment]
    try:
        metrics = asyncio.run(
            jobs._analyze_with_heartbeat(
                job, cube, mesh.AnalysisLimits(), heartbeat_s=0.01
            )
        )
    finally:
        compiler.analyze_stl = original  # type: ignore[assignment]

    assert metrics.triangles == 12, metrics.triangles
    beats = [m.text for m in job.messages if "仍在分析" in m.text]
    assert beats, "长分析期间必须推心跳，否则前端看门狗会把正常作业取消掉"
    assert any("秒" in text for text in beats), beats
    return f"长分析心跳正常（{len(beats)} 次），不会被看门狗误杀"


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
    这里验证的是三层兜底各自真的成立：
      1. Windows 作业对象带 `KILL_ON_JOB_CLOSE`（内核保证：进程一死子进程全灭）；
      2. 登记表 + `terminate_all()` 能真的杀掉子进程；
      3. 超时路径不留孤儿；启动清扫**只**杀我们自己装的 OpenSCAD。
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

    procguard.terminate_all()
    return "退出时不留孤儿：作业对象带 KillOnJobClose、登记表可回收、超时不留孤儿、清扫不误杀"


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


def t_thread_detection() -> str:
    """螺纹识别：真螺旋 vs 同心环（后者看着有纹路，实际拧不上）。

    用**合成点云**测，不需要 OpenSCAD，所以离线也能跑：
      * 真螺旋：牙顶只出现在一个薄片高度里的一小段角度上；
      * 同心环（rotate_extrude 的产物）：同一个高度上四周到处都有牙顶。
    用户反馈的「72 转 67 滤镜转接环做不出来」正是后者 —— 模型封闭、尺寸也对，
    只有专门量才能发现拧不上。
    """
    import math as _math

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
        boundary_edges=0, non_manifold_edges=0,
    )
    assert jobs._geometry_integrity_problems(clean) == [], "合格模型不该触发修复"
    assert jobs._integrity_rank(clean) == (0, 0, 0, 0, 0)

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

    # 两类同时存在时都要报出来（AI 需要一次看全）
    both = mesh.MeshMetrics(solid_parts=3, stray_parts=2, cavity_islands=[(5.0, 1)], watertight=True)
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
    assert declared[2] == ("支架高度", 100.0), declared
    assert declared[0] == ("支架宽度", 80.0), declared
    assert 1 not in declared, "用户没指定的方向不该编出一个期望值"
    assert all(label != "壁厚" for label, _ in declared.values()), (
        "壁厚是零件内部尺寸，不能拿去和包围盒比"
    )

    # 实测远小于声明值 → 必须报出来，并带上双方的具体数字
    short = mesh.MeshMetrics(bbox=(80.0, 50.0, 62.0), watertight=True)
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

    assert jobs._integrity_rank(short, spec) == (0, 0, 1, 0, 0)

    # 实测偏大是正常的（模型常多出底座/托边），绝不能误报
    taller = mesh.MeshMetrics(bbox=(80.0, 50.0, 140.0))
    assert jobs._spec_dimension_problems(spec, taller) == [], (
        "实测比要求高是正常的（含底座等结构），不能当成错误"
    )
    # 阈值：低于声明值 10% 以内算做到（模型有圆角/倒角时允许一点出入）
    borderline = mesh.MeshMetrics(bbox=(80.0, 50.0, 92.0))
    assert jobs._spec_dimension_problems(spec, borderline) == []

    # 采纳条件：先比散件、再比点名尺寸、再比穿腔、最后比封闭性；平手必须判为「没有更好」
    assert jobs._integrity_rank(both) == (0, 2, 0, 1, 0)
    assert jobs._integrity_rank(
        mesh.MeshMetrics(stray_parts=2, cavity_islands=[], watertight=True)
    ) < jobs._integrity_rank(both)
    assert not (
        jobs._integrity_rank(mesh.MeshMetrics(stray_parts=2, cavity_islands=[(5.0, 1)], watertight=True))
        < jobs._integrity_rank(both)
    ), "完全相同的结果不能算改善，否则会白白换掉用户已经在看的模型"
    # 优先级：用户说过的数字做不到（rank 更大=更差）比腔内有孤岛更严重，
    # 因为前者意味着「根本没听懂需求」，后者是内部结构瑕疵。
    # 两个模型都用同一个 spec 比较，保证是同一把尺子。
    island_only = mesh.MeshMetrics(bbox=(80.0, 50.0, 100.0), cavity_islands=[(5.0, 1)], watertight=True)
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
        boundary_edges=0, non_manifold_edges=0,
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
    ) < jobs._integrity_rank(mesh.MeshMetrics(stray_parts=1, watertight=True)), (
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
    # 采纳只发生在 `_integrity_rank(retry_metrics, spec) < best_rank` 这一个条件里。
    assert body.count("best_rank = _integrity_rank(retry_metrics, spec)") == 1, (
        "采纳新版本的路径必须唯一，否则很容易漏掉「平手也不算改善」这条规矩"
    )
    assert "retry_metrics.watertight and _integrity_rank(retry_metrics, spec) < best_rank" in body or (
        "watertight_not_worse and _integrity_rank(retry_metrics, spec) < best_rank" in body
    ), "只有封闭性不变差且严格更好的版本才允许采纳"
    # 封门条件必须写成「不比原版更差」，而不是「候选必须封闭」。
    # 后者会让「原版本来就不封闭」时的有效修复永远无法采纳
    # （把 3 个散件修成一整件，仍因不封闭被拒，用户只能继续拿差的那版）。
    assert "watertight_not_worse = retry_metrics.watertight or not best_metrics.watertight" in body, (
        "采纳判据必须是「封闭性不变差」，不能是「候选必须封闭」"
    )
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

    # 判读方向：只有明确说「有问题」才算有问题。
    # 把「没结论」当成「没问题」会静默放过缺陷，这是最危险的失败方向。
    assert llm.parse_snapshot_verdict("结论：有问题\n桶口有两个多余凸起") is True
    assert llm.parse_snapshot_verdict("结论：无明显问题") is False
    assert llm.parse_snapshot_verdict("结论：**有问题**") is True
    assert llm.parse_snapshot_verdict("") is False, "空回复必须算「没有结论」"
    assert llm.parse_snapshot_verdict("我看不太清楚，可能有吧") is False, "含糊的答复不算数"

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
    assert "VISION_MAX_TOKENS_RETRY" in review_body, "额度用尽后必须放大额度重试一次"

    runner = jobs.GenerationRunner
    assert hasattr(runner, "_review_snapshot"), "生成流程缺少外观复核环节"
    finish_body = inspect.getsource(runner._finish)
    # 外观复核必须在几何修复之前：它的结论要作为修复证据的一部分
    assert finish_body.index("_review_snapshot") < finish_body.index("_repair_geometry_integrity"), (
        "外观复核必须排在几何修复之前，否则它发现的程序测不出来的问题进不了修复证据"
    )

    return (
        f"渲染 {len(shots)} 视角（非背景占比 "
        f"{min(non_background_ratio(s.path) for s in shots):.0%}）、确定性、"
        "判读门槛、模型与额度约束 全部正确"
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
    finally:
        shutil.rmtree(project.dir, ignore_errors=True)
    return f"创建/读写/历史/重命名/删除/回收站/名字安全/往返不丢字段 全部正确（{store.root}）"


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
    return f"{report['ok_count']}/{report['total']} 项通过（overall={report['overall']}）"


def t_http_api() -> str:
    try:
        from fastapi.testclient import TestClient
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 httpx/starlette 的 TestClient 支持") from exc

    from aiforge.app import create_app

    # 基址必须是回环地址：TestClient 默认用 "testserver"，会被本机来源保护正确拦截
    # raise_server_exceptions=False：让 500 也走我们自己的错误处理，验证错误结构真的可用
    client = TestClient(create_app(), base_url="http://127.0.0.1:8760", raise_server_exceptions=False)

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
    assert detect.json()["found"] is True, detect.json()

    cli = client.post("/api/openscad/test")
    assert cli.status_code == 200, cli.text
    assert cli.json()["ok"] is True, cli.json()

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

    return "health/version/report/diagnostics/openscad/settings/安全拦截 全部通过"


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
    assert broken["score"] == 70 and broken["verdict"] == "warn", broken

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


def t_shortcuts_readonly() -> str:
    from aiforge import shortcuts

    info = shortcuts.describe_shortcuts()
    assert "executable" in info and info["executable"], info
    return f"启动命令解析正常（{Path(info['executable']).name}）"


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

    return "薄壁/最小尺寸/成型范围三类判定均随机型与喷嘴正确变化"


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

    # 切换机型
    target = next(p for p in body["printers"] if p["id"] == "prusa-mini-04")
    switched = client.post("/api/printers/select", json={"printer_id": "prusa-mini-04", "material": "PLA"})
    assert switched.status_code == 200, switched.text
    assert switched.json()["printer"]["id"] == "prusa-mini-04"
    assert client.get("/api/settings").json()["printer_id"] == "prusa-mini-04", "选择必须写进设置"

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
    return "回退有原因说明、有退出指引、有界面退出通道"


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


def t_design_mechanism() -> str:
    """运行「设计规格与改动校验」的专项自检（确定性，不调用 AI）。

    这套机制单独放在 selfcheck_design.py 里便于针对性运行；
    这里把它纳入构建流程，确保「只改该改的」在每次构建时都被验证过 ——
    它直接决定「改完会不会变差」，是产品体验的核心。
    """
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
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        tail = "\n".join(output.strip().splitlines()[-12:])
        raise AssertionError("设计规格与改动校验自检未通过：\n" + tail)
    return f"{output.count('[OK]')} 项设计机制检查全部通过"


def t_installer_metadata() -> str:
    """安装器必须带上品牌图标与版本元数据（否则在系统里就是"无名软件"）。"""
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
    assert icon.is_file(), f"安装器引用的图标不存在：{icon}"
    return "图标/发布者/卸载项/许可证/默认目录 均已声明"


def t_paths() -> str:
    from aiforge.paths import paths

    files = paths(refresh=True).ensure()
    for directory in (files.data_dir, files.projects_dir, files.log_dir, files.runtime_dir):
        assert directory.exists(), directory
    ok, message = files.is_writable(files.projects_dir)
    assert ok, message
    return f"数据目录与项目目录均可用"


def _shipped_text_files() -> "list[Path]":
    """仓库里「会进版本库 / 源码包」的文本文件。

    口径必须和 `.gitignore`、`scripts\\export_source.ps1` 一致：
    build/（venv、缓存、自检临时数据、打包产物）、node_modules、dist、
    界面截图与体检报告都是**生成物**，既不该被扫描，也本来就不会被提交。
    """
    unshipped_dirs = {
        "build", "node_modules", "dist", "dist-ssr", ".git", ".vscode", ".idea",
        ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
        "Output", "shots", ".deleted",
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
    return sorted(files)


def t_source_encoding() -> str:
    """提交集里的文本文件不能有 BOM、替换字符或 GBK 乱码。

    为什么必须自动化：编码损坏是**静默**的 —— 代码照样能跑、测试照样能过，
    只有用户看到乱码报错时才暴露。实测踩到两次：
      * 用 PowerShell 的 `Get-Content | Set-Content` 往返一个 .py 文件，
        PS 5.1 按 ANSI 代码页读 UTF-8，中文全变乱码，而且行尾的中文字节被吃掉、
        把 16 行**合并**了 7 处（306 行变 290 行）—— 代码语义已经错了；
      * 文档里一行示例路径被写成了 U+FFFD（原字节不可恢复）。
    两次都是「看着像成功、实际已损坏」，只有扫描才能挡住。
    """
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
    return f"{scanned} 个提交文本文件：无 BOM、无 U+FFFD、无乱码字符"


def t_no_personal_paths() -> str:
    """提交集里不能出现本机绝对路径（开源的硬性要求）。

    为什么必须自动化：这类泄漏只有「别人 clone 之后」才暴露，
    而肉眼很难在 1800 行文档里发现一行本机路径 —— 实测就漏了三处
    （搭建命令、踩坑示例、目录树根）外加前端 mock 里 7 处真实账号名。
    规则**不能硬编码本机路径**（那样规则自己就是泄漏），
    所以用运行时推导：当前用户目录 + 当前仓库根目录。
    """
    home = Path.home()
    needles: list[tuple[str, str]] = [(str(ROOT), "本机仓库绝对路径")]
    if home.name and home.name.lower() not in {"public", "default", "all users"}:
        needles.append((str(home), "本机用户目录"))

    # JS/JSON 里的反斜杠是转义的（C:\\Users\\x），正斜杠写法也很常见，都要覆盖
    variants: list[tuple[str, str]] = []
    for needle, label in needles:
        variants.append((needle, label))
        variants.append((needle.replace("\\", "/"), label))

    problems: list[str] = []
    scanned = 0
    for path in _shipped_text_files():
        rel = path.relative_to(ROOT).as_posix()
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        flattened = text.replace("\\\\", "\\")
        for needle, label in variants:
            if needle in text or needle in flattened:
                problems.append(f"{rel}: 含{label}（{needle}）")

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
    check("路径与目录", t_paths)
    check("配置与密钥", t_config_and_secrets)

    section("安装与运行环境")
    check("WebView2 运行时检测", t_webview_runtime)
    check("应用图标资源", t_icon_assets)
    check("首个启动准备流程", t_first_run_setup)
    check("打包依赖完整性", t_frozen_webview_deps)
    check("浏览器回退体验", t_browser_fallback_notice)
    check("构建脚本纯 ASCII", t_ps1_are_ascii)
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
    check("助手调用作用域", t_helper_job_scope)
    check("子进程不变量守护", t_child_process_guard)
    check("守护接线（启动即生效）", t_child_guard_wiring)
    check("安装失败不破坏已有环境", t_install_no_destroy)
    check("换安装目录可回滚", t_install_swap_rollback)
    check("验收脚本不覆盖用户配置", t_install_test_keeps_config)
    check("SCAD → STL 编译", t_compiler)

    section("业务层")
    check("设计规格与改动机制", t_design_mechanism)
    check("几何完整性自动修复", t_geometry_repair)
    check("固定零件库可编译", t_parts_library)
    check("螺纹识别（螺旋/同心环）", t_thread_detection)
    check("模型外观复核", t_visual_review)
    check("模块引用静态检查", t_module_aliases)
    check("依赖声明完整性", t_declared_dependencies)
    check("项目持久化", t_projects)
    check("环境检测报告", t_envcheck)
    check("作业与事件流", t_sse_contract)
    check("进度可信度", t_progress_accuracy)
    check("网络失败可读且可重试", t_network_errors)
    check("接口响应形状", t_contract_shapes)
    check("并发作业守卫", t_busy_guard)
    check("提示词与解析（离线）", t_llm_offline)
    check("快捷方式解析", t_shortcuts_readonly)

    section("打印机知识库")
    check("知识库结构与参数推导", t_printer_knowledge)
    check("自定义机型增删改", t_printer_custom)
    check("机型影响几何判定", t_printer_affects_analysis)
    check("约束注入 AI 提示词", t_printer_prompt_injection)
    check("打印机 HTTP 接口", t_printer_http)

    section("HTTP 接口")
    check("关键路由与安全拦截", t_http_api)

    section("开源与提交卫生")
    check("开源文件完整性", t_oss_files)
    check("敏感信息扫描", t_no_secrets)
    check("源码编码完好", t_source_encoding)
    check("无本机路径泄漏", t_no_personal_paths)

    print("\n" + "=" * 70)
    if SKIPPED:
        print(f"通过 {len(PASSED)} 项，跳过 {len(SKIPPED)} 项，失败 {len(FAILED)} 项")
        print("-" * 70)
        print("  跳过的项（离线模式；它们**没有**被验证过）：")
        for name, reason in SKIPPED:
            print(f"  ○ {name} —— {reason}")
        print("  发布前请在不带 --offline 的环境跑完整版。")
    else:
        print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("-" * 70)
        for name, error in FAILED:
            print(f"  ✗ {name}: {error}")
    print("=" * 70)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
