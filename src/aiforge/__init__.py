"""AI 3D Forge —— 用一句话生成可 3D 打印的模型。

包结构（按职责分层，便于测试与替换）：
    paths            目录与运行环境解析（用户数据不进 Program Files）
    version          版本与构建信息
    config           用户配置 + Windows 凭据管理器存 API Key
    logging_setup    日志（自动给密钥打码）
    single_instance  单实例保护
    envcheck         环境检测与系统诊断中心
    openscad         OpenSCAD 检测 / 定位 / 官方源下载校验 / 自动安装 / CLI 测试
    scad             SCAD 源码安全校验、参数提取与改写
    mesh             STL 解析与几何度量（封闭性、体积、包围盒）
    compiler         SCAD → STL 编译与诊断解析
    llm              DeepSeek 客户端与提示词
    jobs             生成作业流水线（含自动修复与进度广播）
    projects         项目持久化（原子写、历史归档、导出）
    shortcuts        Windows 快捷方式
    server           本机服务 + WebView2 窗口
    app              FastAPI 应用装配
    api/*            HTTP 路由
"""

from .version import APP_NAME, APP_VERSION, CHANNEL_LABEL

__all__ = ["APP_NAME", "APP_VERSION", "CHANNEL_LABEL", "__version__"]

__version__ = APP_VERSION
