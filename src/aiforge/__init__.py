"""AI 3D Forge —— 用一句话生成可 3D 打印的模型。

包结构（按职责分层，便于测试与替换）。这份清单由自检的「代码不养闲人」用例
与真实目录逐条比对，多一行、少一行都会红 —— 别再手写一个不存在的模块。
    paths            目录与运行环境解析（用户数据不进 Program Files）
    stdio            控制台输出兜底（GBK 编码、无控制台句柄）
    version          版本与构建信息（单一事实来源）
    config           用户配置 + Windows 凭据管理器存 API Key
    logging_setup    日志（自动给密钥打码）
    runtime_state    进程级运行状态（退出、单实例等跨模块协作）
    single_instance  单实例保护
    procguard        子进程守护（退出时不留孤儿进程）
    dpi              高分屏（DPI）适配
    envcheck         环境检测与系统诊断中心
    openscad         OpenSCAD 检测 / 定位 / 官方源下载校验 / 自动安装 / CLI 测试
    scad             SCAD 源码安全校验、参数提取与改写
    mesh             STL 解析与几何度量（封闭性、体积、包围盒、孔径判据）
    render           把 STL 渲染成图（纯标准库，不引入图像依赖）
    compiler         SCAD → STL 编译与诊断解析
    threemf          STL → 3MF 导出（3MF 1.0 核心规范）
    reqtests         需求可执行测试（把设计契约编译成能跑在实测网格上的断言）
    changes          变更契约与改动校验
    design           设计规格（DesignSpec）
    printers         打印机特性知识库
    slicers          切片软件检测与「送到切片软件」
    llm              DeepSeek 客户端与提示词
    jobs             生成作业流水线（含自动修复与进度广播）
    projects         项目持久化（原子写、历史归档、导出）
    parts            标准件库（OpenSCAD 片段 + 参数目录）
    webview_runtime  WebView2 运行时解析
    server           本机服务 + WebView2 窗口
    app              FastAPI 应用装配
    api/*            HTTP 路由
    __main__         `python -m aiforge` 入口
"""

from .version import APP_NAME, APP_VERSION, CHANNEL_LABEL

__all__ = ["APP_NAME", "APP_VERSION", "CHANNEL_LABEL", "__version__"]

__version__ = APP_VERSION
