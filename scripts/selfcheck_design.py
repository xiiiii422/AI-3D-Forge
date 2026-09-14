"""AI 3D Forge —— 设计规格与改动校验的自检（确定性，不调用 AI）。

这些是「让改动只改该改的」这套机制的守门测试。它们不联网、不调用 AI，
因此可以随时跑，并且能精确指出规则哪一条坏了。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("AIFORGE_DATA_DIR", str(ROOT / "build" / "work" / "dspec-data"))
os.environ.setdefault("AIFORGE_LOCAL_DIR", str(ROOT / "build" / "work" / "testlocal"))
os.environ.setdefault("AIFORGE_PROJECTS_DIR", str(ROOT / "build" / "work" / "dspec-proj"))

from aiforge import changes, design, llm  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [OK]   {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  [FAIL] {name} —— {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------
BASE_SCAD = """// @name 桌面手机支架
// @summary 100mm 高的桌面手机支架
// @param height 100 支架高度 40 300
// @param width 80 底座宽度 50 150
// @param thickness 6 底座厚度 3 12
// @param pad_dia 8 脚垫直径 5 15
height = 100;
width = 80;
thickness = 6;
pad_dia = 8;
$fn = 48;
module base() { cube([width, 60, thickness]); }
module back() { translate([0, 0, thickness]) cube([width, 4, height]); }
base();
back();
"""


def t_spec_parsing() -> None:
    section("规格解析与指纹")

    spec = design.parse_spec_response(
        '{"kind":"桌面手机支架","summary":"放手机用","specs":['
        '{"name":"height","label":"支架高度","value":100,"unit":"mm","user_specified":true},'
        '{"name":"wall","label":"壁厚","value":null,"unit":"mm","user_specified":false}],'
        '"features":["能放手机"],"prohibitions":["不要底座"],'
        '"acceptance":["总高等于100mm"]}'
    )
    check("能解析出规格", spec.kind == "桌面手机支架", spec.kind)
    check("能取到用户指定尺寸", spec.find("height") is not None and spec.find("height").value == 100)
    check("禁止项被记录", spec.prohibitions == ["不要底座"], str(spec.prohibitions))

    # 中文标签与英文名都能查到同一条
    check("中文名可查", spec.find("支架高度") is not None)
    check("英文名可查", spec.find("HEIGHT") is not None)

    # 指纹稳定性：只有「用户指定的尺寸 + 禁止项 + 品类」参与
    fp1 = spec.fingerprint()
    same = design.parse_spec_response(
        '{"kind":"桌面手机支架","summary":"完全不同的措辞","specs":['
        '{"name":"height","label":"高度","value":100,"user_specified":true}],'
        '"prohibitions":["不要底座"]}'
    )
    check("措辞变化不影响指纹（同一设计应稳定）", fp1 == same.fingerprint(), f"{fp1} vs {same.fingerprint()}")

    different = design.parse_spec_response(
        '{"kind":"桌面手机支架","summary":"x","specs":['
        '{"name":"height","label":"高度","value":150,"user_specified":true}],'
        '"prohibitions":["不要底座"]}'
    )
    check("尺寸变化会改变指纹", fp1 != different.fingerprint())

    # 坏输入不能抛异常
    check("坏 JSON 退化为空规格", design.parse_spec_response("这不是 JSON").is_empty())
    check("空输入不崩", design.parse_spec_response("").is_empty())


def t_spec_merge() -> None:
    section("规格累积（说过的话不能忘）")

    base = design.parse_spec_response(
        '{"kind":"桌面手机支架","specs":[{"name":"height","label":"高度","value":100,"user_specified":true}],'
        '"prohibitions":["不要底座"]}'
    )
    # 第二轮用户只说「加个孔」，完全没提高度
    update = design.parse_spec_response(
        '{"kind":"桌面手机支架","specs":[{"name":"hole","label":"孔径","value":8,"user_specified":true}],'
        '"features":["背面有充电孔"]}'
    )
    merged = design.merge_specs(base, update)

    height = merged.find("height")
    check("第一轮指定的高度没有被丢掉", height is not None and height.value == 100, str(height))
    check("仍是「用户指定」状态", bool(height and height.user_specified))
    check("新参数已并入", merged.find("hole") is not None and merged.find("hole").value == 8)
    check("禁止项只增不减", "不要底座" in merged.prohibitions, str(merged.prohibitions))
    check("结构项累积", "背面有充电孔" in merged.features, str(merged.features))

    # 新解析里 value 为 null 时不能抹掉旧值
    nulled = design.parse_spec_response('{"kind":"x","specs":[{"name":"height","label":"高度","value":null}]}')
    kept = design.merge_specs(base, nulled)
    check("新值为空时保留旧值", kept.find("height") is not None and kept.find("height").value == 100)

    # 澄清记录累积
    spec = design.DesignSpec()
    spec.merge_clarification("高度100mm")
    spec.merge_clarification("不要底座")
    spec.merge_clarification("高度100mm")  # 重复不应重复记录
    check("澄清去重累积", len(spec.clarifications) == 2, str(spec.clarifications))


def t_change_contract() -> None:
    section("变更契约识别")

    spec = design.parse_spec_response(
        '{"kind":"桌面手机支架","specs":['
        '{"name":"height","label":"支架高度","value":100,"user_specified":true},'
        '{"name":"width","label":"底座宽度","value":80,"user_specified":true}],'
        '"prohibitions":["不要底座"]}'
    )
    params = {"height": 100.0, "width": 80.0, "thickness": 6.0}

    contract = changes.build_change_contract("高度改成150mm", spec, params)
    check("识别出要改 height", "height" in contract.changed, str(contract.changed))
    check("抽到了目标值 150", contract.target_values.get("height") == 150.0, str(contract.target_values))
    check("height 不在「必须保持不变」里", "height" not in contract.unchanged, str(contract.unchanged))
    check("width 被列为必须不变", "width" in contract.unchanged, str(contract.unchanged))
    check("禁止项带进契约", "不要底座" in contract.prohibited, str(contract.prohibited))

    # 中文表述也要能识别
    contract2 = changes.build_change_contract("把支架高度改到 180 毫米", spec, params)
    check("中文表述可识别", "height" in contract2.changed, str(contract2.changed))
    check("中文表述也能抽数值", contract2.target_values.get("height") == 180.0, str(contract2.target_values))

    # 不点名的结构性修改：不应误判为「要改某个参数」
    contract3 = changes.build_change_contract("在背面加一个充电孔", spec, params)
    check("结构类修改不误伤参数", not contract3.changed or all(c not in ("width",) for c in contract3.changed), str(contract3.changed))
    check("结构类修改下所有参数都应保持", set(contract3.unchanged) >= {"height", "width", "thickness"}, str(contract3.unchanged))


def t_verify_change() -> None:
    section("改动校验（只改该改的）")

    spec = design.parse_spec_response(
        '{"kind":"桌面手机支架","specs":['
        '{"name":"height","label":"支架高度","value":100,"user_specified":true},'
        '{"name":"width","label":"底座宽度","value":80,"user_specified":true}]}'
    )
    params = {"height": 100.0, "width": 80.0, "thickness": 6.0}
    contract = changes.build_change_contract("高度改成150mm", spec, params)

    # 理想情况：只改了 height
    good = BASE_SCAD.replace("height = 100;", "height = 150;")
    verdict = changes.verify_change(BASE_SCAD, good, contract, spec)
    check("只改高度 → 通过", verdict.ok, verdict.describe())
    check("判定请求已生效", verdict.performed)
    check("无未授权改动", not verdict.unauthorized, str(verdict.unauthorized))
    check("规格仍完整", verdict.spec_intact)

    # 该改的没改
    unchanged = BASE_SCAD
    bad1 = changes.verify_change(BASE_SCAD, unchanged, contract, spec)
    check("没改动 → 不通过", not bad1.ok, bad1.describe())
    check("指出请求未生效", not bad1.performed)

    # 顺手改了别的参数（这是「越改越歪」的典型）
    sneaky = BASE_SCAD.replace("height = 100;", "height = 150;").replace("width = 80;", "width = 120;")
    bad2 = changes.verify_change(BASE_SCAD, sneaky, contract, spec)
    check("偷改 width → 不通过", not bad2.ok, bad2.describe())
    check("点名了 width", any("width" in u for u in bad2.unauthorized), str(bad2.unauthorized))

    # 破坏了用户此前指定的 width
    bad3 = BASE_SCAD.replace("height = 100;", "height = 150;").replace("width = 80;", "width = 95;")
    bad3v = changes.verify_change(BASE_SCAD, bad3, contract, spec)
    check("破坏既有规格 → 不通过", not bad3v.ok)
    check("说明是「破坏了你之前指定的指标」", "之前指定" in bad3v.describe(), bad3v.describe())

    # 结构类改动（加了一行几何）应当被接受
    structural = BASE_SCAD.replace(
        "base();",
        "module hole() { translate([width/2, 20, 0]) cylinder(d=8, h=thickness*2); }\nbase();\nhole();",
    )
    contract2 = changes.build_change_contract("在背面加一个充电孔", spec, params)
    v2 = changes.verify_change(BASE_SCAD, structural, contract2, spec)
    check("结构类改动被接受", v2.ok, v2.describe())
    check("结构类改动不报未授权", not v2.unauthorized, str(v2.unauthorized))

    # 参数被删除
    removed = BASE_SCAD.replace("thickness = 6;\n", "")
    v3 = changes.verify_change(BASE_SCAD, removed, contract, spec)
    check("参数被删除 → 不通过", not v3.ok, v3.describe())


def t_fingerprint() -> None:
    section("规格指纹写入与读回")

    spec = design.parse_spec_response(
        '{"kind":"桌面手机支架","specs":[{"name":"height","label":"高度","value":100,"user_specified":true}]}'
    )
    stamped = changes.embed_fingerprint(BASE_SCAD, spec)
    check("指纹已写入", changes.read_fingerprint(stamped) == spec.fingerprint(), changes.read_fingerprint(stamped))

    # 重复写入不应叠加
    twice = changes.embed_fingerprint(stamped, spec)
    check("不会重复写入", twice.count("@spec") == 1, str(twice.count("@spec")))

    check("无指纹时读回 None", changes.read_fingerprint(BASE_SCAD) is None)


def t_prefer_logic() -> None:
    section("两次都不达标时的取舍")

    from aiforge import jobs

    spec = design.parse_spec_response(
        '{"kind":"支架","specs":[{"name":"height","label":"高度","value":100,"user_specified":true},'
        '{"name":"width","label":"宽度","value":80,"user_specified":true}]}'
    )
    params = {"height": 100.0, "width": 80.0}
    contract = changes.build_change_contract("高度改成150mm", spec, params)

    # 候选 A：改对了高度，但顺手把 width 改坏（典型的「越改越歪」）
    harmful = BASE_SCAD.replace("height = 100;", "height = 150;").replace("width = 80;", "width = 200;")
    harmful_verdict = changes.verify_change(BASE_SCAD, harmful, contract, spec)
    check("改坏别处的候选本身不达标", not harmful_verdict.ok, harmful_verdict.describe())

    chosen = jobs._prefer(BASE_SCAD, harmful, contract, spec)  # noqa: SLF001
    check("宁可保留未改动，也不接受改坏别处的版本", chosen == BASE_SCAD, "harmful 被拒绝")

    # 候选 B：改对了高度且没动别的 → 应被接受
    clean = BASE_SCAD.replace("height = 100;", "height = 150;")
    chosen2 = jobs._prefer(BASE_SCAD, clean, contract, spec)  # noqa: SLF001
    check("达标候选被接受", chosen2 == clean)

    # 候选 C：高度改了但目标值不对（140 而非 150），且没破坏别处 → 比原版好，应被接受
    near = BASE_SCAD.replace("height = 100;", "height = 140;")
    chosen3 = jobs._prefer(BASE_SCAD, near, contract, spec)  # noqa: SLF001
    check("部分满足且无破坏的候选优于毫无改动", chosen3 == near)


def t_surgical_target_write() -> None:
    """AI 两次都改歪时，必须有一条确定性的「只改该改的」兜底。

    真实踩到的例子：要求「高度改成150mm」，AI 把 height 改对了，
    却顺手把 pad_dia 从 8 改成 80。旧逻辑在两个不达标版本里挑一个 ——
    用户看到的还是「越改越歪」。正确做法是把目标值直接写回原源码。
    """
    section("改动兜底：把目标值直接写回原源码")

    from aiforge import jobs

    spec = design.parse_spec_response(
        '{"kind":"支架","specs":[{"name":"height","label":"高度","value":100,"user_specified":true},'
        '{"name":"pad_dia","label":"脚垫直径","value":8,"user_specified":true}]}'
    )
    params = {"height": 100.0, "pad_dia": 8.0}
    contract = changes.build_change_contract("高度改成150mm", spec, params)
    check("契约拿到了目标值", contract.target_values.get("height") == 150.0, str(contract.target_values))

    # AI 的版本：高度对了，但顺手把 pad_dia 改坏
    harmful = BASE_SCAD.replace("height = 100;", "height = 150;").replace("pad_dia = 8;", "pad_dia = 80;")
    surgical = jobs._apply_targets(BASE_SCAD, contract)  # noqa: SLF001
    check("能构造出「只改目标值」的版本", surgical is not None and surgical != BASE_SCAD)
    if surgical:
        check(
            "只改了高度，pad_dia 原样",
            "height = 150;" in surgical and "pad_dia = 8;" in surgical,
            "其余参数必须一字未动",
        )
        sverdict = changes.verify_change(BASE_SCAD, surgical, contract, spec)
        check("兜底版本通过契约校验", sverdict.ok, sverdict.describe())

    # 取舍权重：改对一项但改坏另一项，代价必须高于「完全没改」。
    # （早期 2.0/项的权重会让程序宁可接受「改坏别处」的版本 —— 这是实测踩过的坑）
    harmful_verdict = changes.verify_change(BASE_SCAD, harmful, contract, spec)
    baseline_verdict = changes.verify_change(BASE_SCAD, BASE_SCAD, contract, spec)
    check(
        "「改对一处但改坏另一处」的代价高于「完全没改」",
        harmful_verdict.penalty() > baseline_verdict.penalty(),
        f"{harmful_verdict.penalty():.2f} vs {baseline_verdict.penalty():.2f}",
    )

    # 目标参数不是顶层纯数值赋值（例如表达式）时，必须放弃兜底而不是半改
    expr = "design_h = 100;\nheight = design_h * 1.5;\ncube([10, 10, height]);\n"
    check(
        "目标不是顶层纯数值赋值时不硬改（不交半成品）",
        jobs._apply_targets(expr, contract) is None,  # noqa: SLF001
    )


def t_param_name_resolution() -> None:
    """规格里的名字与源码里的名字经常对不上 —— 必须能对上，否则兜底失效。

    实测：规格叫 `height`，AI 生成的源码叫 `stand_height`。
    不解析名字，「把目标值写回源码」这一步就永远找不到参数、直接失效，
    用户看到的还是「AI 两次都没改干净」。
    """
    section("规格名 → 源码参数名的解析")

    from aiforge import jobs

    real = {
        "stand_height": 100.0,
        "stand_width": 90.0,
        "base_thickness": 8.0,
        "rib_thickness": 3.0,
        "tilt_angle": 20.0,
        "wall_thickness": 3.0,
        "pad_inset": 10.0,
        "fillet_r": 4.0,
    }
    check("height → stand_height（口语名对上源码名）", changes.resolve_param_name("height", real) == "stand_height")
    check("wall → wall_thickness", changes.resolve_param_name("wall", real) == "wall_thickness")
    check("angle → tilt_angle", changes.resolve_param_name("angle", real) == "tilt_angle")
    check("同名时原样返回", changes.resolve_param_name("pad_inset", real) == "pad_inset")
    check(
        "中文标签「支架高度」→ stand_height",
        changes.resolve_param_name("支架高度", real) == "stand_height",
        "中文没有词边界，只能靠别名包含匹配",
    )
    check(
        "整体尺寸与局部尺寸不会互相污染",
        changes.resolve_param_name("width", real) == "stand_width"
        and changes.normalize_name("phone_slot_width") != changes.normalize_name("width"),
        "phone_slot_width 与 width 归一化后必须不同，否则核对会取错参数",
    )
    check(
        "复合参数能精确对上自己",
        changes.resolve_param_name("rib_thickness", real) == "rib_thickness"
        and changes.resolve_param_name("base_thickness", real) == "base_thickness",
        "rib_thickness 不能与 base_thickness 混为一谈",
    )
    check(
        "完全不认识的名字返回 None",
        changes.resolve_param_name("logo_size", real) is None,
    )

    # ------------------------------------------------------------------
    # 真实模型里最坑的一种参数表：整体尺寸与局部尺寸**同时**含同一个尺寸词。
    #
    # 实测记录（E2E「高度改成150mm」整条改动被误判为失败）：
    #   stand_height（整体高）与 lip_height（前挡边高）都含 `height`，
    #   分层式「唯一命中才算」的匹配器判定「有歧义」→ 返回 None →
    #   程序报「要求把 height 改成 150，但代码里是未找到该参数」，
    #   而 AI 其实把 stand_height 从 100 改成了 150（完全正确）。
    #   一次正确的改动就这样被丢掉，用户看到的是「没改成功」。
    #
    # 修法：改成**打分排序**（整体语义前缀加分、局部部件前缀减分、
    # 名称里还有别的尺寸词元减分），只在最高分唯一时采用。
    # ------------------------------------------------------------------
    tricky = {
        "stand_height": 100.0,
        "stand_width": 90.0,
        "stand_depth": 70.0,
        "wall_thickness": 3.0,
        "tilt_angle": 20.0,
        "slot_width": 12.0,
        "slot_depth": 5.0,
        "lip_height": 14.0,
        "base_thickness": 6.0,
        "fillet": 2.0,
    }
    check(
        "整体高度与局部高度并存时，height 选整体（stand_height）",
        changes.resolve_param_name("height", tricky) == "stand_height",
        f"lip_height 也很像，但它是局部尺寸；实际={changes.resolve_param_name('height', tricky)}",
    )
    check(
        "整体宽度与槽宽并存时，width 选整体（stand_width）",
        changes.resolve_param_name("width", tricky) == "stand_width",
        f"实际={changes.resolve_param_name('width', tricky)}",
    )
    check(
        "thickness 不会被 height 家族抢走",
        changes.resolve_param_name("thickness", tricky) == "base_thickness",
        f"实际={changes.resolve_param_name('thickness', tricky)}",
    )
    check(
        "打分的解析器仍能精确对上每一个局部尺寸",
        changes.resolve_param_name("lip_height", tricky) == "lip_height"
        and changes.resolve_param_name("slot_width", tricky) == "slot_width"
        and changes.resolve_param_name("base_thickness", tricky) == "base_thickness",
    )
    check(
        "中文标签也在复杂参数表里对得上整体尺寸",
        changes.resolve_param_name("支架高度", tricky) == "stand_height"
        and changes.resolve_param_name("底座宽度", tricky) == "stand_width",
    )
    check(
        "单字母别名不会污染别的尺寸名",
        # 真实踩到的隐蔽 bug：key 展开时用了 `alias in wanted`，
        # 而单字母别名 `h` 是 `thickness` 的子串 → key 集合被掺进 `height`
        # → 「厚度」被解析成整体高度（thickness → stand_height）。
        # 现在只对长度 ≥ 2 的中文别名做展开。
        changes.resolve_param_name("thickness", tricky) == "base_thickness"
        and changes.resolve_param_name("thickness", tricky) != "stand_height",
        f"实际={changes.resolve_param_name('thickness', tricky)}",
    )
    check(
        "确属歧义的名字宁可返回 None（不猜）",
        # 中文「厚度」在别名表里本身是 depth 的同义词，而源码里同时有
        # stand_depth 与 base_thickness —— 用户到底指哪个说不清，交给 AI 改。
        changes.resolve_param_name("厚度", tricky) is None
        or changes.resolve_param_name("厚度", tricky) in ("stand_depth", "base_thickness"),
        "只要不是把厚度安到高度上就合格",
    )

    # 端到端：契约里的目标值必须挂在源码真实存在的参数名上
    spec = design.parse_spec_response(
        '{"kind":"支架","specs":[{"name":"height","label":"高度","value":100,'
        '"user_specified":true}]}'
    )
    contract = changes.build_change_contract("高度改成150mm", spec, real)
    check(
        "契约目标值已解析成源码参数名",
        contract.target_values == {"stand_height": 150.0},
        str(contract.target_values),
    )
    source = "stand_height = 100;\nstand_width = 90;\ncube([stand_width, 20, stand_height]);\n"
    surgical = jobs._apply_targets(source, contract)  # noqa: SLF001
    check(
        "能直接写出只改高度的版本",
        surgical is not None and "stand_height = 150;" in surgical and "stand_width = 90;" in surgical,
        "解析成功后兜底路径才真正可用",
    )

    # 真实场景：契约建立时**还没有参数表**（修改流程里先建契约、后读源码），
    # 目标名会保持规格里的 height。此时兜底必须再对着真实源码解析一次名字，
    # 否则整条兜底链路在生产环境里仍然是失效的（实测踩过）。
    late_contract = changes.build_change_contract("高度改成150mm", spec, {})
    check("没有参数表时契约保留规格名", late_contract.target_values == {"height": 150.0},
          str(late_contract.target_values))
    late_surgical = jobs._apply_targets(source, late_contract)  # noqa: SLF001
    check(
        "兜底会对着真实源码再解析一次参数名",
        late_surgical is not None and "stand_height = 150;" in late_surgical,
        "否则生产的修改流程里兜底永远不生效",
    )
    check(
        "兜底不会动别的参数",
        late_surgical is not None and "stand_width = 90;" in late_surgical,
        "只改目标值",
    )


def t_scaled_source_builder() -> None:
    """缩放兜底：必须只缩一次、语法合法、且对 AI 的各种代码形态都成立。

    这里每一条都来自实测踩过的坑（详见 README-开发者.md 第七节）。
    """
    section("缩放版源码构造（防止「套 scale 壳」与「缩两次」）")

    from aiforge import jobs
    from aiforge.jobs import build_scaled_source

    modular = """// @name 支架
height = 100;
module base() { cube([80, 60, 6]); }
module back() { translate([0,0,6]) cube([80,4,height]); }
base();
back();
"""
    built = build_scaled_source(modular, 0.73, "测试机")
    check("带 module 的源码能构造缩放版", built is not None)

    if built:
        # 尺寸走参数的模型走「参数路径」：直接改写参数值，不套 scale 块。
        # 套了就会缩两次（参数一次、scale 一次），实测 240 → 135 而不是 180。
        check(
            "参数路径改写了参数值",
            "height = 73;" in built,
            built.splitlines()[1] if len(built.splitlines()) > 1 else built,
        )
        check(
            "参数路径没有套 scale 块（否则缩两次）",
            "scale([__aiforge_fit" not in built,
            "参数路径与几何路径只能二选一",
        )
        check(
            "module 定义原样保留（变换块内禁止定义 module，所以不能乱包）",
            "module base() { cube([80, 60, 6]); }" in built
            and "module back()" in built,
            "声明必须留在顶层",
        )
        check(
            "没有重复实例化（避免原尺寸模型仍在）",
            built.count("base();") == 1 and built.count("back();") == 1,
            "调用只应出现一次",
        )

    # 只有声明、没有任何几何 —— 缩放没有意义（哪怕声明里有参数）
    check(
        "只有声明没有几何时返回 None",
        build_scaled_source("L = 240;\nW = 40;\n", 0.7, "测试机") is None,
    )

    # 只有一个 module 却忘了调用（AI 真的这么写过）：补一个缩放后的实例化
    no_call = "L = 240;\nmodule part() { cube([L,40,20]); }\n"
    rescued = build_scaled_source(no_call, 0.75, "测试机")
    check("只有一个 module 且没调用时补上实例化", rescued is not None)
    if rescued:
        check(
            "补的是缩放后的实例化",
            "scale([__aiforge_fit, __aiforge_fit, __aiforge_fit]) part();" in rescued,
            "否则这份源码在 OpenSCAD 眼里是空模型",
        )

    # 多个 module 都没调用：猜哪个是模型本体是不安全的，必须放弃
    check(
        "多个 module 都没调用时返回 None（不猜）",
        build_scaled_source(
            "module a(){cube([240,40,20]);}\nmodule b(){cube([1,1,1]);}\n", 0.75, "测试机"
        )
        is None,
    )

    # 缩放有两条互斥的路子，绝不能同时用（同时用就缩两次：240 → 135 而非 180）：
    #   参数路径 —— 改写顶层参数值，尺寸走参数的模型（AI 生成的绝大多数）
    #   几何路径 —— 把几何包进 scale 块，尺寸写死在几何里的模型
    param_path = jobs._scale_param_values("length = 240;\ncube([length, 40, 20]);\n", 0.75)  # noqa: SLF001
    check("参数路径改写了顶层参数值", param_path is not None and "length = 180;" in param_path)
    check(
        "参数路径不再套 scale 块（否则缩两次）",
        param_path is not None and "scale([__aiforge_fit" not in param_path,
        "二选一，不能叠加",
    )
    check(
        "没有参数时参数路径返回 None",
        jobs._scale_param_values("cube([240, 40, 20]);\n", 0.75) is None,  # noqa: SLF001
    )
    geometry_path = jobs._scale_geometry("cube([240, 40, 20]);\n", 0.75, "测试机")  # noqa: SLF001
    check(
        "几何路径把尺寸写死的几何包进 scale 块",
        geometry_path is not None
        and "scale([__aiforge_fit, __aiforge_fit, __aiforge_fit]) {" in geometry_path,
    )
    check(
        "参数路径与几何路径不会叠加",
        param_path is not None and "scale([__aiforge_fit" not in param_path,
        "叠加会导致缩两次",
    )


def t_confirmation_gate() -> None:
    """确认关卡的状态机：等确认不能算结束，取消必须能唤醒等待。

    这里的两个坑都是「用户点不动」级别的：
      * 把 awaiting_confirmation 当成终态 → 前端把确认卡片丢掉，用户再也没有确认入口；
      * cancel() 只认 running → 用户在确认卡片上点取消，作业仍卡在 wait_for_spec()。
    两条都必须在没有 AI、没有网络的情况下可验证。
    """
    section("规格确认关卡的状态机")

    import asyncio

    from aiforge import jobs

    spec = design.parse_spec_response(
        '{"kind":"支架","specs":[{"name":"height","label":"高度","value":100,"user_specified":true}]}'
    )

    async def confirm_flow() -> tuple[str, object, dict]:
        job = jobs.Job(job_id="t-confirm", project_id="p1", revision=0, mode="create", prompt="设计一个支架")
        job.awaiting_spec = spec
        job.status = "awaiting_confirmation"
        job.set_stage("awaiting_confirmation")
        jobs.publish_spec(job, spec)
        snapshot = job.snapshot()

        waiter = asyncio.create_task(job.wait_for_spec())
        await asyncio.sleep(0)
        check("等待确认期间作业未结束", job.status == "awaiting_confirmation", job.status)
        check("等待确认期间确实在挂起（未自行返回）", not waiter.done())
        job.confirm_spec(spec)
        decided = await asyncio.wait_for(waiter, timeout=2)
        return job.status, decided, snapshot

    status, decided, snapshot = asyncio.run(confirm_flow())
    check("确认后唤醒等待并拿到规格", decided is spec, status)
    check("快照里带着规格卡片数据", isinstance(snapshot.get("spec"), dict), str(type(snapshot.get("spec"))))
    check(
        "快照明确标注等待确认",
        bool(snapshot.get("spec", {}).get("awaiting_confirmation")) is True,
        "awaiting_confirmation=True",
    )

    async def cancel_flow() -> object:
        job = jobs.Job(job_id="t-cancel", project_id="p1", revision=0, mode="create", prompt="设计一个支架")
        job.awaiting_spec = spec
        job.status = "awaiting_confirmation"
        job.set_stage("awaiting_confirmation")
        waiter = asyncio.create_task(job.wait_for_spec())
        await asyncio.sleep(0)
        job.cancel()
        # cancel() 必须先唤醒等待，否则 wait_for_spec() 永远不返回、任务泄漏
        await asyncio.wait_for(waiter, timeout=2)
        return job.status

    check("等待确认时可以取消", asyncio.run(cancel_flow()) == "cancelled")


def t_ai_review_merge() -> None:
    """AI 复核（第二道校验）与程序化结论的合并规则。

    这是「程序化核对通过、但模型其实被改坏了」的唯一防线：
    参数 diff 看不出「底座改窄到站不稳」「承力处被削薄」这类问题。
    合并方向必须是**不对称**的 —— 宁可因为 AI 误报多花一次重做，
    也不能让一个结构被改坏的版本因为参数全对而蒙混过关。
    """
    section("AI 复核与程序化结论的合并")

    from aiforge import changes, jobs

    clean = changes.ChangeVerdict(ok=True, reasons=["只改动了授权内容"])

    # 1) AI 说没问题 → 保持通过
    v = changes.ChangeVerdict(ok=True)
    jobs._merge_ai_review(v, {"requested_change_applied": True, "functional_ok": True, "summary": "没问题", "concerns": []})  # noqa: SLF001
    check("AI 认可时维持通过", v.ok, v.describe())
    check("AI 结论被记录下来", v.ai_summary == "没问题", str(v.ai_summary))

    # 2) 程序化通过，但 AI 说改动没生效 → 必须判不通过（否则会假装成功）
    v = changes.ChangeVerdict(ok=True)
    jobs._merge_ai_review(v, {"requested_change_applied": False, "functional_ok": True, "summary": "只是改了注释", "concerns": []})  # noqa: SLF001
    check("AI 说改动未生效时判不通过", not v.ok, v.describe())
    check("并标记为未生效", not v.performed, str(v.performed))

    # 3) 程序化通过，但 AI 说结构被改坏 → 必须判不通过
    v = changes.ChangeVerdict(ok=True)
    jobs._merge_ai_review(v, {"requested_change_applied": True, "functional_ok": False, "summary": "底座变窄了", "concerns": ["底座宽度不足以站稳"]})  # noqa: SLF001
    check("AI 说结构被改坏时判不通过", not v.ok, v.describe())
    check("疑点被记录", v.ai_concerns == ["底座宽度不足以站稳"], str(v.ai_concerns))
    check("疑点进入结论文字", "底座宽度不足以站稳" in v.describe(), v.describe())

    # 4) AI 拿不到结论（网络/解析失败）→ 绝不能因此把一个干净的改动判失败
    v = changes.ChangeVerdict(ok=True)
    jobs._merge_ai_review(v, None)  # noqa: SLF001
    check("AI 无结论时不影响程序化判定", v.ok, v.describe())
    v = changes.ChangeVerdict(ok=True)
    jobs._merge_ai_review(v, {"requested_change_applied": None, "functional_ok": None, "summary": "无法判定", "concerns": []})  # noqa: SLF001
    check("三态未知时不误判", v.ok, v.describe())

    # 5) AI 疑点会抬高代价，但没有程序化证据重
    with_concerns = changes.ChangeVerdict(ok=False, ai_concerns=["a", "b"])
    programmatic = changes.ChangeVerdict(ok=False, unauthorized=["x", "y"])
    check(
        "AI 疑点的代价低于同等数量的程序化违规",
        with_concerns.penalty() < programmatic.penalty(),
        f"{with_concerns.penalty():.1f} vs {programmatic.penalty():.1f}",
    )

    # 6) 解析器对坏输入必须给出「无法判定」而不是「没问题」
    parsed = llm.parse_change_review_json("这不是 JSON")
    check(
        "复核 JSON 解析失败时返回无法判定",
        parsed["requested_change_applied"] is None and parsed["functional_ok"] is None,
        str(parsed),
    )
    parsed2 = llm.parse_change_review_json(
        '```json\n{"requested_change_applied":"false","functional_ok":"true",'
        '"summary":"高度没改","concerns":["高度仍是100"]}\n```'
    )
    check(
        "字符串形式的布尔值也能解析",
        parsed2["requested_change_applied"] is False and parsed2["functional_ok"] is True,
        str(parsed2),
    )

    # 7) 复核提示词必须真的把「两份代码 + 契约」都给出去
    contract = changes.build_change_contract(
        "高度改成150mm",
        design.parse_spec_response('{"kind":"支架","specs":[{"name":"height","value":100}]}'),
        {"height": 100.0},
    )
    prompt = llm.change_review_user_prompt(
        "height = 100;\ncube([10,10,height]);\n",
        "height = 150;\ncube([10,10,height]);\n",
        contract,
        None,
    )
    check("复核提示词含改动前的代码", "height = 100;" in prompt, "必须给两份代码才能比对")
    check("复核提示词含改动后的代码", "height = 150;" in prompt)
    check("复核提示词含改动契约", "本次只允许修改" in prompt or "height" in prompt)
    check("复核系统提示词要求判断结构是否被改坏", "functional_ok" in llm.CHANGE_REVIEW_SYSTEM_PROMPT)
    # ⑥ 温度统一调低：这是「同一需求不该每次得到不同结果」的硬保障。
    # 集中成常量才能一眼看全，也能被断言钉住（散在各方法里迟早漂移）。
    temps = {
        "parse_design_spec": llm.TEMPERATURE_SPEC,
        "generate_from_spec": llm.TEMPERATURE_GENERATE,
        "modify_with_contract": llm.TEMPERATURE_MODIFY,
        "repair_change": llm.TEMPERATURE_REPAIR,
        "review_change": llm.TEMPERATURE_REVIEW,
    }
    check(
        "所有温度都 ≤ 0.2（统一调低）",
        all(t <= 0.2 for t in temps.values()),
        str(temps),
    )
    check(
        "改动类温度不高于生成类（改代码最怕漂移）",
        llm.TEMPERATURE_MODIFY < llm.TEMPERATURE_GENERATE
        and llm.TEMPERATURE_REPAIR < llm.TEMPERATURE_MODIFY,
        f"modify={llm.TEMPERATURE_MODIFY} repair={llm.TEMPERATURE_REPAIR} "
        f"generate={llm.TEMPERATURE_GENERATE}",
    )
    check(
        "判定类温度为 0（同一份改动必须同结论）",
        llm.TEMPERATURE_REVIEW == 0.0,
        str(llm.TEMPERATURE_REVIEW),
    )
    # 九个调用点必须全部引用常量，不允许再有裸数字漂进来
    llm_source = _source_of(llm)
    bare = re.findall(r"temperature=(?!TEMPERATURE_)[\d.]+", llm_source)
    check("没有裸写的 temperature 数字", not bare, str(bare))
    check(
        "复核方法用的是复核温度常量",
        "TEMPERATURE_REVIEW" in _source_of(llm.DeepSeekClient.review_change),
    )


def _source_of(func: object) -> str:
    import inspect

    return inspect.getsource(func)


def t_instruction_parsing() -> None:
    """一句口语指令 → 目标值：三处实测踩到的归属错误。

    这些都是「用户说得清清楚楚，程序却理解成另一件事」级别的问题，
    而且后果是直接改错参数 —— 比「没改成功」更糟。
    """
    section("修改指令的数值归属（子句 / 否定 / 相对修改）")

    spec = design.parse_spec_response(
        '{"kind":"支架","specs":['
        '{"name":"height","label":"高度","value":100,"user_specified":true},'
        '{"name":"width","label":"宽度","value":80,"user_specified":true}]}'
    )
    params = {"height": 100.0, "width": 80.0}

    def targets(instruction: str) -> dict[str, float]:
        return changes.build_change_contract(instruction, spec, params).target_values

    # ① 用户明确说了「不变」的那个参数，绝不能被别的数字改掉。
    #    实测：'高度不变，宽度改成90mm' 被解析成 height=90 —— 恰好改的是
    #    用户要求别动的那个，而真正要改的 width 一动不动。
    got = targets("高度不变，宽度改成90mm")
    check(
        "「XX 不变」时数值不会落到 XX 上",
        got == {"width": 90.0},
        f"实际 {got}；期望 {{'width': 90.0}}",
    )
    got = targets("高度保持不变，宽度改成90mm")
    check("换成「保持不变」也一样", got == {"width": 90.0}, str(got))

    # ①b 更严重的一半：用户说「别动」的参数，绝不能出现在「只允许修改」名单里。
    #     实测：height 因为出现在句子里而被算进 changed，等于**授权 AI 去改
    #     用户明确要求冻结的参数** —— 方向恰好相反。现在它进 frozen 与 unchanged。
    c = changes.build_change_contract("高度不变，宽度改成90mm", spec, params)
    check(
        "被要求不变的参数不在授权名单里",
        "height" not in c.changed,
        f"changed={c.changed}",
    )
    check("被要求不变的参数记进 frozen", "height" in c.frozen, f"frozen={c.frozen}")
    check(
        "被要求不变的参数不重复出现在必须保持清单里",
        "height" not in c.unchanged,
        f"unchanged={c.unchanged}（frozen 已经单独说过一次）",
    )
    check(
        "契约说明里明确写出「不要改动」",
        "不要改动" in c.describe(),
        c.describe().splitlines()[1] if len(c.describe().splitlines()) > 1 else "",
    )
    # 只改该改的 → 通过；碰了用户点名别动的 → 必须明确报出来
    src_line = "height = 100;\nwidth = 80;\ncube([width, 60, 6]);\n"
    ok_v = changes.verify_change(src_line, src_line.replace("width = 80;", "width = 90;"), c, spec)
    check("只改被点名的那个 → 通过", ok_v.ok, ok_v.describe())
    bad_v = changes.verify_change(
        src_line,
        src_line.replace("width = 80;", "width = 90;").replace("height = 100;", "height = 120;"),
        c,
        spec,
    )
    check(
        "动了用户点名别动的项 → 明确报出",
        not bad_v.ok and any("不要改动" in r for r in bad_v.reasons),
        bad_v.describe(),
    )

    # ② 一句话改多个参数：每个数字各自归属，不能只认第一个。
    got = targets("高度改成150mm，宽度改成120mm")
    check(
        "一句话改两个参数能各自取到值",
        got == {"height": 150.0, "width": 120.0},
        f"实际 {got}",
    )
    got = targets("高度改成150mm，宽度保持不变")
    check("一个改一个不变时只取改的那个", got == {"height": 150.0}, str(got))

    # ③ 相对修改：数字是增量，不是目标值。
    #    实测：'高度改小 20mm' 被解析成 height=20（把 100 高直接变成 20）。
    got = targets("高度改小 20mm")
    check("「改小 20mm」是相对修改（100−20=80）", got == {"height": 80.0}, f"实际 {got}")
    got = targets("高度增大 30mm")
    check("「增大 30mm」（100+30=130）", got == {"height": 130.0}, f"实际 {got}")
    check(
        "绝对赋值不受相对词影响",
        targets("高度改成150mm") == {"height": 150.0},
        str(targets("高度改成150mm")),
    )

    # ④ 同一句话里出现**相同数字**时，每个参数都要拿到自己的值。
    #    实测：旧实现先取出数值、再用 text.find(str(值)) 回头找位置，
    #    而 find 永远返回第一处 —— 于是「宽度改成90mm，深度改成90mm」
    #    两个 90 都被算成第一个子句的，结果只改了一个参数，
    #    另一个被**静默丢掉**（实测 4/5 个用例出错）。
    #    把盒子改成正方体（宽深都改成同一个数）是极常见的请求。
    got = targets("宽度改成90mm，高度改成90mm")
    check(
        "相同数字分别归属两个参数",
        got == {"width": 90.0, "height": 90.0},
        f"实际 {got}；期望 {{'width': 90.0, 'height': 90.0}}",
    )
    got = targets("高度改成100mm，宽度改成100mm")
    check("两个参数改成同一个值都要生效", got == {"height": 100.0, "width": 100.0}, str(got))
    got = targets("宽度改成80mm，高度改成80mm")
    check("碰巧等于当前值的相同数字也不丢", got == {"width": 80.0, "height": 80.0}, str(got))

    # ⑤ 单位必须换算成毫米。
    #    实测：'高度改成 15cm' 被当成 15mm —— 生成一个**小 10 倍**的模型，
    #    而契约核对还认为「目标已达成」报了成功。
    for instruction, expect, label in (
        ("高度改成 15cm", 150.0, "15cm → 150mm"),
        ("高度改成 15 厘米", 150.0, "15 厘米 → 150mm"),
        ("高度改成 150mm", 150.0, "150mm 保持 150mm"),
        ("高度改成 0.15米", 150.0, "0.15米 → 150mm"),
    ):
        got = targets(instruction)
        check(label, got == {"height": expect}, f"实际 {got}")
    inch = targets("高度改成 6英寸")
    check(
        "6英寸 → 152.4mm",
        abs(inch.get("height", 0) - 152.4) < 0.01,
        str(inch),
    )
    # 单位换算不能误伤「个/度/层」这类非长度单位
    check(
        "「加 3 个孔」的 3 不会被当成 3mm 再乘",
        targets("高度改成150mm，加3个孔").get("height") == 150.0,
        str(targets("高度改成150mm，加3个孔")),
    )
    check(
        "「45 度」不参与长度换算",
        targets("高度改成150mm，角度改成45度").get("height") == 150.0,
        str(targets("高度改成150mm，角度改成45度")),
    )

    # ⑥ 单位换算后，「用户要求的数值」核对也要用换算后的值 ——
    #    否则源码里写着 150，核对却去找 15，会误判成「没做到」。
    unit_spec = design.parse_spec_response(
        '{"kind":"支架","specs":[{"name":"height","label":"高度","value":100,'
        '"user_specified":true}]}'
    )
    unit_contract = changes.build_change_contract("高度改成 15cm", unit_spec, params)
    unit_ok = changes.verify_change(
        "height = 100;\ncube([10,10,height]);\n",
        "height = 150;\ncube([10,10,height]);\n",
        unit_contract,
        unit_spec,
    )
    check(
        "15cm 的目标值与源码里的 150 能对上",
        unit_ok.ok and unit_ok.performed,
        unit_ok.describe(),
    )


def t_verify_honesty() -> None:
    """核对必须「如实」：没证据说生效时，不能报成功。"""
    section("核对如实性（不许假装成功）")

    spec = design.parse_spec_response(
        '{"kind":"支架","specs":['
        '{"name":"height","label":"高度","value":100,"user_specified":true}]}'
    )
    params = {"height": 100.0, "width": 80.0}
    src = "height = 100;\nwidth = 80;\ncube([width, 60, 6]);\n"

    # 契约没点名任何参数（用户只说「加个孔」），而代码一字未改：
    # 以前这里直接判「通过」，等于把「什么都没做」报成成功。
    contract = changes.build_change_contract("在背面加一个充电孔", spec, params)
    verdict = changes.verify_change(src, src, contract, spec)
    check(
        "契约没点名参数 + 代码未变 → 如实标注为未生效",
        not verdict.performed and bool(verdict.reasons),
        verdict.describe(),
    )
    # 但结构类改动确实发生了时要放行，不能误杀
    verdict2 = changes.verify_change(src, src + "cube([5,5,5]);\n", contract, spec)
    check(
        "结构类改动确实发生 → 放行（不误杀）",
        verdict2.ok and verdict2.performed,
        verdict2.describe(),
    )

    # 空指令不该让核对崩溃或谎报
    empty_contract = changes.build_change_contract("", spec, params)
    verdict3 = changes.verify_change(src, src, empty_contract, spec)
    check(
        "空指令 + 代码未变 → 如实标注",
        not verdict3.performed and bool(verdict3.reasons),
        verdict3.describe(),
    )


def t_least_bad_floor() -> None:
    """取舍必须有下限：**绝不挑出比「不改」还差的那一版**。

    实测缺陷：三个候选全都把别处改坏时，`_pick_least_bad` 仍会挑一个出来
    （代价 10.0），而原版代价只有 3.33 —— 用户拿到的是比不改更糟的结果。
    另一处：候选列表为空时直接 `min([])` 抛 ValueError，把整次修改变成
    「生成过程中出现了意外问题」。
    """
    section("不达标版本的取舍下限")

    from aiforge import jobs

    spec = design.parse_spec_response(
        '{"kind":"支架","specs":['
        '{"name":"height","label":"高度","value":100,"user_specified":true},'
        '{"name":"width","label":"宽度","value":80,"user_specified":true}]}'
    )
    params = {"height": 100.0, "width": 80.0, "thickness": 6.0}
    src = "height = 100;\nwidth = 80;\nthickness = 6;\ncube([width, 60, thickness]);\n"
    contract = changes.build_change_contract("高度改成150mm", spec, params)

    harmful = src.replace("height = 100;", "height = 150;").replace("width = 80;", "width = 300;")
    picked = jobs._pick_least_bad(src, [harmful, harmful], contract, spec)  # noqa: SLF001
    check(
        "所有候选都改坏别处 → 退回原版（宁可不变）",
        picked == src,
        f"代价 {changes.verify_change(src, picked, contract, spec).penalty():.2f}",
    )

    try:
        picked = jobs._pick_least_bad(src, [], contract, spec)  # noqa: SLF001
        check("候选列表为空 → 返回原版而不是崩溃", picked == src)
    except Exception as exc:  # noqa: BLE001
        check("候选列表为空 → 返回原版而不是崩溃", False, f"{type(exc).__name__}: {exc}")

    clean = src.replace("height = 100;", "height = 150;")
    check(
        "干净候选仍会被选中（下限不误伤好结果）",
        jobs._pick_least_bad(src, [clean, harmful], contract, spec) == clean,  # noqa: SLF001
    )


def main() -> int:
    print("=" * 70)
    print("设计规格与改动校验自检（不调用 AI）")
    print("=" * 70)

    t_spec_parsing()
    t_spec_merge()
    t_change_contract()
    t_instruction_parsing()
    t_verify_change()
    t_verify_honesty()
    t_fingerprint()
    t_prefer_logic()
    t_least_bad_floor()
    t_surgical_target_write()
    t_param_name_resolution()
    t_scaled_source_builder()
    t_confirmation_gate()
    t_ai_review_merge()

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  ✗ {item}")
        print("=" * 70)
        return 1
    print("全部通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
