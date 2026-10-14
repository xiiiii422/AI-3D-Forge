# 贡献指南（Contributing）

感谢你有兴趣改进 AI 3D Forge。这份文档说明**怎么改、怎么测、怎么提交**。

> 第一次接触这个项目？先看 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) 把环境跑起来。

---

## 一、可以贡献什么

| 类型 | 入口 |
|---|---|
| 缺陷报告 | [新建 Issue](../../issues/new?template=bug_report.md) —— 请附日志、复现步骤、版本 |
| 功能建议 | [新建 Issue](../../issues/new?template=feature_request.md) |
| 代码 / 文档 PR | 见下面「三、提交 Pull Request」 |
| 新打印机型号 | 修改 `src/aiforge/printers.py` 的机型表，并在 PR 里附**官方规格来源链接** |
| 安全漏洞 | **不要开公开 Issue**，见 [SECURITY.md](SECURITY.md) |

## 二、开发流程

```powershell
git clone <仓库地址> AI-3D-Forge
cd AI-3D-Forge
git checkout -b fix/short-description
```

环境搭建、运行方式、测试与构建命令都在 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。

### 提交前必须跑

```powershell
# 1) 完整后端自检（94 项）
& build\venv\Scripts\python.exe scripts\selfcheck.py

# 2) 设计规格与改动机制（147 项子检查，不消耗 AI）
& build\venv\Scripts\python.exe scripts\selfcheck_design.py

# 3) 密钥与隐私扫描（含 Git 历史）
& build\venv\Scripts\python.exe scripts\check_no_secrets.py --history

# 4) 改过前端就跑类型检查与构建
cd webui; npm run build; cd ..
```

改了这些地方时，还要额外跑：

| 改动 | 额外要求 |
|---|---|
| 后端接口 / 响应结构 | 同步更新 [docs/API-CONTRACT.md](docs/API-CONTRACT.md) |
| 界面 | `node webui\scripts\layout-audit.mjs http://localhost:5273` 与 `spec-card-audit.mjs` 必须 0 缺陷 |
| 生成/修改链路 | 跑端到端：`scripts/e2e_test.py`（需要你自己的 `DEEPSEEK_API_KEY`，**不要**把 Key 提交进仓库或写进 CI） |
| 安装器 / 打包 | `.\scripts\test_install.ps1`（46 项，会真实安装与卸载） |
| 依赖 | 重跑 `scripts\list_dependencies.py`，同步 [docs/LICENSE-THIRD-PARTY.txt](docs/LICENSE-THIRD-PARTY.txt) |

## 三、提交 Pull Request

1. **保持改动聚焦**：一个 PR 解决一件事。格式化、重命名这类"顺手改"请单独开 PR。
2. **不要重写核心功能**：这个项目的行为大多是被实测"钉"住的，
   想改行为请先在 Issue 里说明现象与证据。
3. **不要为了让测试变绿而放宽断言**：要么修代码，要么把判据改得更准确，
   并在 PR 描述里说明为什么旧判据是错的。
4. **PR 描述请包含**：
   - 问题是什么（现象 + 复现方式）；
   - 改了什么（文件/函数级别）；
   - 你怎么验证的（命令 + 真实输出，**不要贴臆测的结果**）；
   - 有没有副作用 / 兼容性影响。
5. **CI 必须全绿**：`.github/workflows/ci.yml` 会跑开源文件完整性、密钥扫描、
   Python 语法与离线自检、前端构建。CI **不会**调用真实 DeepSeek API。

## 四、代码规范

| 面向 | 约定 |
|---|---|
| Python | 3.10+ 语法；类型标注尽量补齐；`from __future__ import annotations`；模块内部用 `log = logging.getLogger(__name__)`；**中文注释写"为什么"**（踩过的坑要写清楚） |
| 错误信息 | 面向用户的中文人话 + 明确的下一步操作；**绝不**把 HTTP 状态码或堆栈直接甩给用户 |
| 前端 | TypeScript 严格模式；样式只用 `webui/src/styles/tokens.css` 里的设计令牌；不引入组件库 |
| 脚本 | `.ps1` **必须纯 ASCII**（Windows PowerShell 5.1 会按 ANSI 解码，中文会坏）；需要中文输出就交给 Python |
| 提交信息 | 第一行说清"改了什么"，正文说"为什么"；不要写"fix bug"这类无信息量的描述 |
| 文档 | 用户能看懂的写进 `README*.md` / `docs/USER_GUIDE.md`；实现细节与踩坑写进 `README-开发者.md` |

## 五、几条硬性红线

1. **绝不提交密钥**：API Key / Token / Cookie / 密码 / `.env` 一律不进仓库。
   提交前跑 `scripts/check_no_secrets.py`。
2. **绝不提交本机信息**：个人绝对路径、用户名、机器名、真实项目数据。
3. **不提交生成物**：`build/`、`webui/dist/`、`node_modules/`、安装包、
   日志、缓存、截图产物（`.gitignore` 已覆盖，请勿绕过）。
4. **第三方许可证不能改**：新增依赖要在 PR 里说明其许可证，并更新
   [docs/LICENSE-THIRD-PARTY.txt](docs/LICENSE-THIRD-PARTY.txt)。
5. **不要用真实 API Key 跑 CI**。

## 六、行为准则

参与本项目即表示你同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
