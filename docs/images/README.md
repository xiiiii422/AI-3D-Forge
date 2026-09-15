# 界面截图

这些图用于 README 与文档，来自**假后端（`webui/mock-server.mjs`）的固定样例数据**，
不是任何真实用户的机器。生成脚本：`webui/scripts/interface-shots.mjs` 与
`webui/scripts/screenshot.ps1`。

| 文件 | 内容 |
|---|---|
| `welcome.png` | 欢迎页：用一句话描述，得到可直接打印的 3D 模型 |
| `studio-generating.png` | 生成中：阶段进度（真实百分比）与设计对话 |
| `studio-result.png` | 结果页：3D 预览、实测尺寸、打印性评分、下载入口 |

**提交新截图前请确认**：

1. 没有出现 API Key（哪怕是掩码）—— 用「系统诊断 / 设置」页的截图时尤其注意；
2. 没有出现本机绝对路径、用户名、真实项目名；
3. 图片里不要带内部测试标记。

`scripts/check_no_secrets.py` 会自动扫仓库里的文本文件；图片不在它的扫描范围内，
所以这一步靠人工确认（新截图请照上面三条检查一遍）。
