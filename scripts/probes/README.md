# 探针脚本（诊断用，不是产品代码）

这些脚本是**排查具体问题时写的一次性诊断工具**：它们不含产品逻辑，
只负责"把某个怀疑摆到台面上量一量"。保留它们是因为**每一个都对应一个真实缺陷**，
下次遇到同类症状时可以直接改路径复用，不必从零再写。

## 与自检的关系（重要）

能固化成断言的部分**已经全部搬进** `scripts/selfcheck.py` 了 —— 那些是长期守卫，
每次 `--offline` 自检都会跑，不依赖任何外部数据：

| 探针 | 之前抓到的问题 | 现在的长期守卫 |
|---|---|---|
| `probe_bridge.py` | 规格叫 `height`、源码叫 `total_h` 时，一次正确的改动被判"没改到"+"越权" | `t_name_bridge` |
| `probe_contract_matrix.py` | "加个孔"被听成"外径加 10mm"；"底座加厚"什么都没匹配上 | `t_contract_intent` |
| `probe_scale.py` | "整体缩小到 80%" 静默无操作 | `t_user_scale` |
| `probe_hole_measure.py` | 射线法量孔径（自检夹具用不到真实 STL） | 见 `scripts/verify_modify_matrix.py` |
| `probe_ai_credit.py` | 区分"网络错"与"账户余额不足" | 人工排查用 |
| ~~`probe_free_names.py`~~（已删，见下一节） | `install_window_runtime` 用了 `asyncio.to_thread`，可模块里没有 `import asyncio` —— 「一键安装」一点就是 500 | `t_no_undefined_names` |
| ~~`probe_install_endpoint.py`~~（已删，同上） | 同上：只有真发一次 HTTP 请求才能发现这条接口是死的 | `t_browser_fallback_notice`（里面跑了一次 TestClient 往返） |

上表最后两行的文件已经不在仓库里（结论已是常驻守卫），列在这里是为了记下
「这两条守卫当初是被什么坑出来的」—— 要看当时的排查过程得去 `git log`。

**优先用自检，其次用这里的探针。** 探针依赖 `build\work\` 下由真实作业生成的项目数据，
数据被清掉后会跑不起来（这是刻意的：不把 300MB 生成物带进交接包）。

## 每个脚本要什么、证明什么

| 脚本 | 输入 | 证明什么 |
|---|---|---|
| `probe_bridge.py` | `build\work\probe-height\` 里的真实项目 | 改名桥（注释标签 + 结尾缩写）能把 `height` 接到 `total_h`；阴性对照保证绿灯不是放松判据换来的 |
| `probe_contract_matrix.py` | `build\work\modify-matrix\` 里的真实项目 | 12 条指令 → 授权范围是否正确（含"加孔不许授权外径"这条正对照） |
| `probe_scale.py` | 同上，取某一版源码 | 契约授权到参数名 + 确定性缩放真的缩小 + 数量类参数不变 |
| `probe_hole_measure.py` | 归档版本的 STL | 射线法量孔径（Ø10→9.8、Ø8→7.8），并暴露过"量成半径"的错 |
| `probe_hole_contract.py` | 真实项目某一版 | "加一个直径10mm的圆孔"这条指令在契约里的授权内容 |
| `probe_verify.py` | 真实项目 + 真实 AI | 把一次修改的每一道校验摊开（契约 / 首版判据 / 重做版 / 兜底 / 取舍） |
| `probe_height.py` | 真实 AI + OpenSCAD | 打印作业的**全部阶段消息**，定位改动在哪一步被丢掉 |
| `probe_stl_measure.py` | 任意 STL | 直接解析面片量包围盒，判断"模型真有那么大"还是"程序读错了" |
| `thread_matrix.py` / `twist_matrix.py` | OpenSCAD | 螺纹写法对比实验（结论已进 `parts/` 零件库与 README-开发者.md §71） |
| `probe_ai_credit.py` | 真实 API Key | 一次最小真实调用，区分"余额不足/鉴权失败/网络不通" |
| `negctl_oss.py` | 无（只临时改 `README.md` / `LICENSE` / `docs/USER_GUIDE.md`，跑完逐条还原） | **负向对照**：证明开源合规那几支守卫在真出问题时会变红。六种违规逐个制造（API Key 写进文档、本机绝对路径、删 LICENSE、README 断链、`.env` 混进提交集、`.gitignore` 生效性反证），最后确认还原后重新全绿。它从 `build\work\` 提到这里的理由就是本文件上面那句：铁证不能写在忽略目录里 |
| `probe_slicer_roundtrip.py` | 本机真装的 Bambu Studio / OrcaSlicer + 一个隔离后端 | 送到切片软件的**真机往返**：进程表里确有 pid、命令行里带着我们的 3MF、`detached: true` 问得出来、快照没被自己的清理删掉。自检验不了这一段（它只能验“杀父进程后子进程还在”），所以这个探针不能只留在 `build\work\` 里（那是忽略目录，新 clone 的人拿不到）—— 它就是“送到切片软件”那一段取证在仓库里的唯一落点 |
| `make_audit_project.py` | OpenSCAD（不依赖 AI、不花额度） | 给真后端界面审计造一个**真的有模型**的项目：`audit-interface.mjs` 的 `tabular-nums` 一项只有在工作台加载了真实模型时才有候选，空工作台永远报「0 个候选」，看着全清其实一步未查。数据全写在 `build\work\audit-*` 沙箱里，绝不碰用户真实项目（跑法见 `docs/HANDOVER.md` §7 第 4 条） |

## 探针的退场（删掉的那些）

一次性诊断工具用完就该走：它们不含产品逻辑，判据又已全部固化成守卫，
留着只会让人以为这条链路还需要手工摸一遍，还会误导后人去改一个已经钉住的脚本。
已删：`probe_resolve.py` / `probe_resolve2.py` / `probe_trace.py` / `probe_trace2.py` /
`probe_trace3.py` / `probe_label_debug.py` / `probe_norm.py`（名字解析链路的逐层打印，
现由 `t_name_bridge` 每次自检盯住），以及 `webui/scripts/debug-*.mjs` 五个同类前端定点
调试脚本（结论全部在 `verify-flow.mjs` 的步骤里）。
本轮另外删了 `probe_free_names.py` 与 `probe_install_endpoint.py`：它们的结论已经是
常驻守卫（见上表），而且不依赖任何外部数据 —— 这种探针没理由继续存在。

同一个理由也适用于本轮为「子进程输出不会被解码吃掉」写的那两个临时探针
（一个试 `text=True` 在 `-X utf8` 下能不能读到输出、一个做真实投毒）：结论已进
常驻守卫（含真跑一遍的对照），探针当场删掉，不在 `build\work\` 里留一个“看上去像证据”
却没人再跑过的脚本。

1.0.3 收尾时另删了一批同样的一次性产物（`si_repro.py` / `run_one_par.py` /
`poison_premise.py` 与它们的 `*.txt` 输出）：它们是在验单实例锁那三个结构缺陷时写的，
结论与能钉的数字全部进了文档（`README-开发者.md` §75 / §76），守卫 `t_single_instance` 每次自检
都会重跑一遍 —— 不删的代价本轮真实付过一次：终端回滚里一条 17:20 的旧红被当成了刚发生的
故障，顺着它又查了一遍（虽然那个“旧红”后来反倒把真缺陷第 8 条抖了出来）。

紧接 `README-开发者.md` §76 那一轮（取证“错读一位数字→唤走别人窗口”与“`os.kill(pid, 0)` 到底是什么”）又删了一批：
`probe_pidlive_90117.py`（旧布局错读 + 会不会打死目标）、`probe_hijack_90118.py`（拿一个活的外来
进程当诱饵跑完整一次唤窗）、`probe_guiwin_90119.py`（哪个系统 GUI 枚举得到窗口 —— notepad 在 Win11
是打包应用，枚举不到）、`probe_imgpath_90120.py` 与 `probe_sysexec_90121.py` / `probe_sibling_90122.py`
（镜像名对已退出 pid 返回什么、同一个 exe 为何两个名字）、`probe_ctrlc_runner/drive/ctrlc2_9014x.py`
（控制台归属）、`probe_readscope_90153.py`（4 种读法 × 4 个锁位）、`lock_holder_90150.py` 与
`verify_lockclaim_90151.ps1`（用 AST 从真脚本里拉函数跑七个形状）、`run_one_si_90130.py`（只跑
两个单实例用例的驱动器），以及它们的全部 `*.txt` / `*.marks` 输出与 `probe-hijack-*` /
`probe-pidlive-*` 目录。结论全部落在 `README-开发者.md` §76 与两条常驻守卫（`t_single_instance` /
`t_child_process_guard`）里，不依赖任何外部数据，没理由继续存在。

这批里有一条代价值得留下：那个验证装机预检的驱动器**第一次跑是 3 过 10 红**，红的原因不是
产品坏了，而是驱动器自己把 scriptblock 字面量当函数装了（被“调用”的那个函数返回了自己的
源码）。如果那份输出留在 `build\work\` 里，下一个读到它的人会去查一个**不存在的**产品缺陷 ——
就是 `README-开发者.md` §75 那条的形状。一次性产物的输出不比它的代码更容易误导人。

同一个理由收掉本轮为「构建脚本自己被 pip 的一句警告打死」写的那批：`probe_pip_eap_90170.ps1`
（八种形状×两种 `$ErrorActionPreference`）、`probe_childexit_90171.ps1`（子脚本写了错误记录
会不会让 `-File` 返回 1 —— 答案是不会，所以那条疑心不成立，没改）、`probe_lastexitcode_90174.ps1`
（报错之后那个退出码到底还能不能用）、`verify_invoke_native_90173.ps1` 与 `noise_child_90173.py`
（用 AST 从真 `scripts\build.ps1` 里拉出 `Invoke-Native` 跑六个断言）、`poison_ps1_guard_90172.py`
（新守卫 `t_ps1_native_stderr` 的四格双向投毒，还原后字节一致）。结论已经分别是
`scripts/build.ps1` 的一段实测注释和一条每次自检都跑的守卫，探针没理由留着。

顺手记一条：那个驱动器第一次跑 6 过 1 红，红的又是驱动器自己 —— PowerShell 5.1 把
`python -c "..."` 里的双引号吃掉了一半，子进程收到的是一句语法错的代码。换成一个真的
`.py` 子文件才对齐。写验证的时候，先验证验证工具自己。

回填发包口径时用一个只读数的小探针（`probe_repo_size_90185.py`：待提交文件数与体积、
截图占比）量完即删：这类数字不值得养一个常驻脚本，但**也不能沿用旧数**——旧行写的
166 个 / 3.1 MB 已经飘到 185 个 / 3.3 MB。同一轮还删了 `build\work\` 里的
`sc-off-*.txt` / `build-90183.log` / `install-90184.log` / `export-90186.log` / `docref-90182.txt`
等一次性输出（`build/` 整目录本来就忽略，但留着会让人把旧跑动的红当成新的，付过一次代价）。

## 约定

* 探针一律**只读**产品数据、把临时产物写到 `build\work\` 下，绝不碰用户真实项目目录
  （`%USERPROFILE%\Documents\AI-3D-Forge`）。
* 隔离测试数据用 `AIFORGE_DATA_DIR` / `AIFORGE_PROJECTS_DIR`，
  **不要**覆盖 `AIFORGE_LOCAL_DIR` —— 那里面放着本机已装好的 OpenSCAD，覆盖后链路会直接报"没有找到 OpenSCAD"。
* 探针的判据必须是**实测几何/实测值**，不接受"AI 说自己改好了"。
