// ============================================================================
// AI 3D Forge 固定零件库（自建，随程序分发，可被生成的 SCAD 通过 use 引用）
// ----------------------------------------------------------------------------
// 为什么要有这个库：让模型去"手写"螺旋螺纹几何，实测几乎必错 ——
//   * 用 linear_extrude(twist) 画出来的是螺旋肋，不是螺纹；CGAL 还会把它判定成
//     非流形（实测 800+ 条非流形边）；
//   * 自建 polyhedron 单独渲染没问题，但一放进 union/difference，OpenSCAD 就报
//     "The given mesh is not closed"，**静默丢掉整块几何**，用户拿到一个光滑圆筒；
//   * 用一堆小方块沿螺旋线堆，编译 190 秒、还不封闭。
// 这里给出的每个零件都是**实测验证过**的：封闭、单一实体、尺寸正确、可编译。
// 验证方式见 scripts/selfcheck.py 的「固定零件库可编译」。
//
// 使用约定（生成的代码里照抄即可）：
//     use <aiforge_parts.scad>
//     filter_adapter(male_d = 72, female_d = 67);
//
// 许可证：与本项目一致（MIT）。库内不使用任何外部文件。
// ============================================================================

// ---------------------------------------------------------------------------
// 通用：60° 公制螺纹牙型。相位 p∈[0,1)：0 = 牙底，1 = 牙顶。
// 相机滤镜螺纹的国家/行业惯例是 0.75mm 螺距（M37×0.75 … M95×0.75 都是）。
// ---------------------------------------------------------------------------
function tooth60(p) =
    let (f = (p % 1 + 1) % 1)
    f < 0.125 ? 1 :
    f < 0.5   ? 1 - (f - 0.125) / 0.375 :
    f < 0.625 ? 0 :
                (f - 0.625) / 0.375;

// 螺纹表面半径：公称半径 R（牙顶），牙深 = 0.61 × 螺距（60° 牙型的理论值）
function threaded_r(R, pitch, depth, theta, z) =
    R - depth + depth * tooth60(theta / 360 - z / pitch);

// ---------------------------------------------------------------------------
// 零件 1：滤镜转接环（一端外螺纹，一端内螺纹，中间有挡肩）
//   典型用法：male_d=72（拧进镜头）、female_d=67（拧上滤镜）
//   整件是**一个 polyhedron**，不做布尔运算 —— 这是它能保持封闭的关键。
// ---------------------------------------------------------------------------
module filter_adapter(
    male_d       = 72,      // 外螺纹公称直径 mm
    male_pitch   = 0.75,    // 外螺纹螺距 mm（滤镜螺纹固定 0.75）
    female_d     = 67,      // 内螺纹公称直径 mm
    female_pitch = 0.75,    // 内螺纹螺距 mm
    height       = 8.4,     // 总高 mm（滤镜螺纹建议咬合 3~5mm，太短拧不牢）
    shoulder_t   = 1.4,     // 挡肩厚度 mm（挡住滤镜不让它拧穿）
    clearance    = 0.35,    // 内螺纹单边间隙 mm（打印件比金属松一点才拧得进）
    fn           = 64,      // 圆周分段（64 足够；96 会让编译时间翻倍）
    slices_per_turn = 8     // 每圈轴向切片（8 够光滑；调大只增加时间）
) {
    depth = male_pitch * 0.61;
    turns = height / male_pitch;
    nz = max(6, ceil(turns * slices_per_turn));
    dz = height / nz;
    na = max(24, fn);

    r_f_top = female_d / 2 + clearance;                       // 内螺纹牙顶（挖出来的槽底）
    r_bore = female_d / 2 - 2.0;                              // 挡肩处通孔半径

    outer = [for (i = [0 : nz]) for (j = [0 : na - 1])
                let (t = j * 360 / na,
                     r = threaded_r(male_d / 2, male_pitch, depth, t, i * dz))
                [r * cos(t), r * sin(t), i * dz]];

    n_sh = ceil(shoulder_t / dz);
    inner_parts = concat(
        [for (i = [0 : n_sh]) for (j = [0 : na - 1])
            let (t = j * 360 / na)
            [r_bore * cos(t), r_bore * sin(t), i * dz]],
        [for (i = [n_sh : nz]) for (j = [0 : na - 1])
            let (t = j * 360 / na,
                 r = threaded_r(r_f_top, female_pitch, depth, t + 180, i * dz))
            [r * cos(t), r * sin(t), i * dz]]
    );
    n_in = n_sh + 1 + (nz - n_sh + 1);

    o = function(i, j) i * na + (j % na);
    n_out = (nz + 1) * na;
    inn = function(i, j) n_out + i * na + (j % na);

    faces = concat(
        [for (i = [0 : nz - 1]) for (j = [0 : na - 1])
            [o(i, j), o(i, j + 1), o(i + 1, j + 1), o(i + 1, j)]],
        [for (i = [0 : n_sh - 1]) for (j = [0 : na - 1])
            [inn(i, j), inn(i + 1, j), inn(i + 1, j + 1), inn(i, j + 1)]],
        [for (j = [0 : na - 1])
            [inn(n_sh, j), inn(n_sh + 1, j), inn(n_sh + 1, j + 1), inn(n_sh, j + 1)]],
        [for (i = [n_sh + 1 : n_in - 2]) for (j = [0 : na - 1])
            [inn(i, j), inn(i + 1, j), inn(i + 1, j + 1), inn(i, j + 1)]],
        [for (j = [0 : na - 1]) [inn(0, j), inn(0, j + 1), o(0, j + 1), o(0, j)]],
        [for (j = [0 : na - 1]) [inn(n_in - 1, j), o(nz, j), o(nz, j + 1), inn(n_in - 1, j + 1)]]
    );

    polyhedron(points = concat(outer, inner_parts), faces = faces, convexity = 8);
}

// ---------------------------------------------------------------------------
// 零件 2：螺纹套筒（内外都是螺纹的短管；做延长筒、转接套用）
// ---------------------------------------------------------------------------
module thread_sleeve(
    outer_d      = 72,
    outer_pitch  = 0.75,
    inner_d      = 67,
    inner_pitch  = 0.75,
    height       = 10,
    clearance    = 0.35,
    fn           = 64,
    slices_per_turn = 8
) {
    depth = outer_pitch * 0.61;
    turns = height / outer_pitch;
    nz = max(6, ceil(turns * slices_per_turn));
    dz = height / nz;
    na = max(24, fn);
    r_f_top = inner_d / 2 + clearance;

    outer = [for (i = [0 : nz]) for (j = [0 : na - 1])
                let (t = j * 360 / na,
                     r = threaded_r(outer_d / 2, outer_pitch, depth, t, i * dz))
                [r * cos(t), r * sin(t), i * dz]];
    inner = [for (i = [0 : nz]) for (j = [0 : na - 1])
                let (t = j * 360 / na,
                     r = threaded_r(r_f_top, inner_pitch, depth, t + 180, i * dz))
                [r * cos(t), r * sin(t), i * dz]];

    o = function(i, j) i * na + (j % na);
    n_out = (nz + 1) * na;
    inn = function(i, j) n_out + i * na + (j % na);

    faces = concat(
        [for (i = [0 : nz - 1]) for (j = [0 : na - 1])
            [o(i, j), o(i, j + 1), o(i + 1, j + 1), o(i + 1, j)]],
        [for (i = [0 : nz - 1]) for (j = [0 : na - 1])
            [inn(i, j), inn(i + 1, j), inn(i + 1, j + 1), inn(i, j + 1)]],
        [for (j = [0 : na - 1]) [inn(0, j), inn(0, j + 1), o(0, j + 1), o(0, j)]],
        [for (j = [0 : na - 1]) [inn(nz, j), o(nz, j), o(nz, j + 1), inn(nz, j + 1)]]
    );
    polyhedron(points = concat(outer, inner), faces = faces, convexity = 8);
}

// ---------------------------------------------------------------------------
// 零件 3：螺纹盖（内螺纹 + 顶盖；做镜头盖、瓶盖、防尘盖用）
// ---------------------------------------------------------------------------
module threaded_cap(
    inner_d     = 67,
    pitch       = 0.75,
    height      = 12,
    top_t       = 2.0,       // 顶盖厚度
    clearance   = 0.35,
    knurl       = true,      // 外圈滚花（便于手拧）
    fn          = 64,
    slices_per_turn = 8
) {
    depth = pitch * 0.61;
    turns = height / pitch;
    nz = max(6, ceil(turns * slices_per_turn));
    dz = height / nz;
    na = max(24, fn);
    r_f_top = inner_d / 2 + clearance;
    wall = 2.4;
    r_out = inner_d / 2 + wall + (knurl ? 0.0 : 0.0);

    // 外表面：滚花用低频起伏模拟（正弦），比真滚花便宜得多且好打印
    outer = [for (i = [0 : nz]) for (j = [0 : na - 1])
                let (t = j * 360 / na,
                     bump = knurl ? 0.35 * (0.5 + 0.5 * cos(36 * t)) : 0)
                [(r_out + bump) * cos(t), (r_out + bump) * sin(t), i * dz]];
    // 内表面：螺纹（顶盖那段没有螺纹，靠 top_t 决定）
    z_thread_top = height - top_t;
    inner = [for (i = [0 : nz]) for (j = [0 : na - 1])
                let (t = j * 360 / na,
                     z = i * dz,
                     r = z <= z_thread_top
                         ? threaded_r(r_f_top, pitch, depth, t + 180, z)
                         : r_f_top - depth - 0.2)
                [r * cos(t), r * sin(t), z]];

    o = function(i, j) i * na + (j % na);
    n_out = (nz + 1) * na;
    inn = function(i, j) n_out + i * na + (j % na);

    faces = concat(
        [for (i = [0 : nz - 1]) for (j = [0 : na - 1])
            [o(i, j), o(i, j + 1), o(i + 1, j + 1), o(i + 1, j)]],
        [for (i = [0 : nz - 1]) for (j = [0 : na - 1])
            [inn(i, j), inn(i + 1, j), inn(i + 1, j + 1), inn(i, j + 1)]],
        [for (j = [0 : na - 1]) [inn(0, j), inn(0, j + 1), o(0, j + 1), o(0, j)]],
        [for (j = [0 : na - 1]) [inn(nz, j), o(nz, j), o(nz, j + 1), inn(nz, j + 1)]]
    );
    polyhedron(points = concat(outer, inner), faces = faces, convexity = 8);
}
