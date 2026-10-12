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
//   male_d = 拧进镜头那端的外螺纹直径；female_d = 接滤镜那端的内螺纹直径。
//   『A 转 B』= male_d=A、female_d=B。既支持降口径（A>B），也支持升口径（A<B）。
//   整件是**一个 polyhedron**，不做布尔运算 —— 这是它能保持封闭的关键。
//
//   ★ 升口径（小转大，如 67 转 77）必须外扩：内螺纹比外螺纹粗，若外表面仍按
//     male_d 走，就会出现「内半径 > 外半径」→ polyhedron 内外翻面，螺纹跑到错误
//     的一侧（外螺纹变孔内、内螺纹变外表面），拿到一个谁都拧不上的废件。
//     修法：外表面按需扩到 r_female+wall 包住内螺纹，并把大口径端翻到底面贴平台，
//     使外壁自下而上收窄 → 免支撑打印。
// ---------------------------------------------------------------------------
module filter_adapter(
    male_d       = 72,      // 外螺纹公称直径 mm（拧进镜头的那一端）
    male_pitch   = 0.75,    // 外螺纹螺距 mm（滤镜螺纹固定 0.75）
    female_d     = 67,      // 内螺纹公称直径 mm（拧上滤镜的那一端）
    female_pitch = 0.75,    // 内螺纹螺距 mm
    height       = 8.4,     // 总高 mm（滤镜螺纹建议咬合 3~5mm，太短拧不牢）
    shoulder_t   = 1.4,     // 挡肩厚度 mm（降口径时靠近底部的挡肩；升口径时自动计算）
    clearance    = 0.35,    // 内螺纹单边间隙 mm（打印件比金属松一点才拧得进）
    wall         = 2.0,     // 最小壁厚 mm（升口径时决定外扩后外径 = female_d + 2*(clearance+wall)）
    fn           = 64,      // 圆周分段（64 足够；96 会让编译时间翻倍）
    slices_per_turn = 8     // 每圈轴向切片（8 够光滑；调大只增加时间）
) {
    depth_m = male_pitch   * 0.61;            // 60° 牙型理论牙深
    depth_f = female_pitch * 0.61;

    r_male   = male_d / 2;                    // 外螺纹牙顶半径
    r_female = female_d / 2 + clearance;      // 内螺纹牙顶半径（已含装配间隙）

    // 顶部（内螺纹那一端）的外表面半径：必须包住内螺纹并留出壁厚。
    //   降口径：r_male 本就更大 → 外径不外扩（= r_male），沿用既有已验证布局；
    //   升口径：外径必须扩到 r_female + wall，否则内半径 > 外半径 → 几何内外翻转。
    r_out_top  = max(r_male, r_female + wall);
    need_flare = r_out_top > r_male + 0.001;

    // 中心通孔（光路）半径：既给外螺纹留出壁厚，又要小于内螺纹根径以形成挡肩。
    r_bore = max(3, min(r_male - depth_m - wall, r_female - depth_f - wall));

    // 螺纹导入倒角：把两端的「第一牙」削小（外螺纹削牙顶、内螺纹扩孔口），
    // 装配时好起步、不刮手；打印时端部也不会留下易碰掉的薄牙尖。
    lead   = 0.9;                 // 导入倒角轴向长度 mm
    cham_m = depth_m + 0.3;       // 外螺纹导入处牙顶下削量（削到牙底以下）
    cham_f = depth_f + 0.3;       // 内螺纹导入处孔口扩大量

    // 轴向切片：按较细的螺距取密度，保证螺纹相位一圈内被采样足够多次。
    pitch_min = min(male_pitch, female_pitch);
    nz = max(8, ceil(height / pitch_min * slices_per_turn));
    dz = height / nz;
    na = max(24, fn);

    // 轴向布局（先以「外螺纹端在 z=0」建模，最后按需整体翻转）：
    //   female_engage 内螺纹咬合高度；ramp 外扩台肩过渡高度；z_sh 挡肩高度。
    female_engage = need_flare ? min(max(height * 0.45, 2.6), height * 0.6) : 0;
    ramp          = need_flare ? min(max(height * 0.16, 0.9), 1.8)          : 0;
    z_sh_target   = need_flare ? max(height - female_engage, ramp + 2.6)    : shoulder_t;
    i_sh = max(1, min(nz - 1, round(z_sh_target / dz)));
    i_m  = need_flare ? max(1, min(i_sh - 1, round((z_sh_target - ramp) / dz))) : nz;

    // 外表面半径（含外螺纹调制）：底部外螺纹 → 台肩外扩 → 顶部直筒。
    // 外螺纹入口（z=0）用 min() 叠一个锥面做导入倒角：近端锥面低于牙顶 → 削出倒角。
    r_out = function(i, t) let (z = i * dz)
        (i <= i_m)  ? min(threaded_r(r_male, male_pitch, depth_m, t, z),
                          r_male - cham_m + cham_m * min(1, z / lead))
      : (i >= i_sh) ? r_out_top
      : r_male + (r_out_top - r_male) * (i - i_m) / (i_sh - i_m);

    // 内表面半径（含内螺纹调制）：挡肩以下为通孔，挡肩以上为内螺纹。
    // 内螺纹入口（z=height）用 max() 叠一个锥面做孔口扩大（countersink）导入倒角。
    r_in = function(i, t) let (z = i * dz)
        (i < i_sh) ? r_bore
      : max(threaded_r(r_female, female_pitch, depth_f, t + 180, z),
            r_female + cham_f - (cham_f + depth_f) * min(1, (height - z) / lead));

    outer = [for (i = [0 : nz]) for (j = [0 : na - 1])
                let (t = j * 360 / na, r = r_out(i, t))
                [r * cos(t), r * sin(t), i * dz]];
    inner = [for (i = [0 : nz]) for (j = [0 : na - 1])
                let (t = j * 360 / na, r = r_in(i, t))
                [r * cos(t), r * sin(t), i * dz]];

    o = function(i, j) i * na + (j % na);
    n_out = (nz + 1) * na;
    inn = function(i, j) n_out + i * na + (j % na);

    faces = concat(
        // 外壁（法向朝外）
        [for (i = [0 : nz - 1]) for (j = [0 : na - 1])
            [o(i, j), o(i, j + 1), o(i + 1, j + 1), o(i + 1, j)]],
        // 内壁（法向朝轴心）
        [for (i = [0 : nz - 1]) for (j = [0 : na - 1])
            [inn(i, j), inn(i + 1, j), inn(i + 1, j + 1), inn(i, j + 1)]],
        // 底面环（z = 0）
        [for (j = [0 : na - 1]) [inn(0, j), inn(0, j + 1), o(0, j + 1), o(0, j)]],
        // 顶面环（z = height）
        [for (j = [0 : na - 1]) [inn(nz, j), o(nz, j), o(nz, j + 1), inn(nz, j + 1)]]
    );

    // 升口径时把「大口径的内螺纹端」翻到底面贴平台：外壁自下而上收窄 → 完全自支撑，
    // 不需要支撑材料（否则台肩底部是一圈好几毫米宽的大悬垂）。
    // rotate+translate 是刚体变换（行列式 +1）：不翻面、不改螺纹旋向、不破坏水密。
    translate([0, 0, need_flare ? height : 0])
        rotate([need_flare ? 180 : 0, 0, 0])
            polyhedron(points = concat(outer, inner), faces = faces, convexity = 10);
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
