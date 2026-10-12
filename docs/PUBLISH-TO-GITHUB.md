# 发布到 GitHub

这份文档说明「把这个仓库推到 GitHub 需要做什么、以及哪些东西**不该**推」。

> 面向仓库维护者。普通用户看 [README-用户.md](../README-用户.md)，
> 贡献者看 [CONTRIBUTING.md](../CONTRIBUTING.md)。

---

## 一、仓库在哪

与项目目录**同级**的一个独立文件夹 `AI-3D-Forge-GitHub`
（具体绝对路径见交付说明 —— 文档里刻意不写绝对路径，见下面第七节）。

它是一个**可以直接 `git push` 的完整开源仓库**，包含：

| 内容 | 说明 |
|---|---|
| `src/`、`main.py`、`webui/` | 全部源码 |
| `scripts/` | 构建、打包、自检、端到端、安装验收、开源合规守卫 |
| `docs/` | 用户指南、开发者上手、接口契约、第三方许可清单、OpenSCAD 来源说明、截图 |
| `installer/` | Inno Setup 打包脚本 |
| `LICENSE`、`README*.md`、`SECURITY.md`、`CONTRIBUTING.md`、`CHANGELOG.md`、`CODE_OF_CONDUCT.md` | 开源门面与协作文档 |
| `.github/` | CI 工作流、Issue/PR 模板、Dependabot |
| `.gitignore`、`.gitattributes` | 仓库约定 |

**不含**（都是生成物，被 `.gitignore` 排除）：`build/`、`webui/node_modules/`、
`webui/dist/`、`installer/Output/`、界面截图产物、任何密钥与本机路径。

> 需要改内容时**不要直接在仓库目录里改**：开发树是唯一真源，
> 改完导出并同步（见第八节），否则两边会分叉。

---

## 二、许可证：MIT（已选定）

仓库根目录的 `LICENSE` 是 **MIT** 许可证全文，README 里也已标注。
GitHub 会自动识别并在仓库页显示 "MIT license"。

`docs/LICENSE-THIRD-PARTY.txt` 是**第三方组件清单**（Python 包、前端包、
OpenSCAD 的 GPLv2 说明），`docs/LICENSE-OpenSCAD-GPLv2.txt` 是 OpenSCAD 的
**GPLv2 全文**（因为我们会在用户确认后分发它，必须随包提供）。

---

## 三、推上去

```powershell
cd <仓库目录>

# 1) 远端（换成你自己的仓库地址）
git remote add origin https://github.com/<你的用户名>/AI-3D-Forge.git

# 2) 提交作者目前是一个中性的占位身份，建议改成你自己：
#    （只影响以后的提交，不会改历史）
git config user.name  "你的名字"
git config user.email "你的邮箱"

# 3) 推送（分支已经是 main）
git push -u origin main
```

推送后建议顺手做三件事：

1. **替换 README 里的徽章占位符**：`README.md` / `README.en.md` 顶部的
   `https://github.com/<owner>/<repo>/...` 换成真实地址，徽章才会亮；
2. 在仓库 *Settings → Security* 打开 **Private vulnerability reporting**，
   `SECURITY.md` 里的私密报告入口才真正可用；
3. 建一个 tag 并把安装包作为 Release 附件（见第四节）。tag 号**从
   `src/aiforge/version.py` 的 `APP_VERSION` 取**（`git tag v<版本号>`），
   不要在这份文档里记一个写死的号 —— 它会漂（这里曾经写着 `v1.0.0`，
   而实际已发到 1.0.3，照做就会推一个对不上任何构建的 tag）。

### 仓库页建议填写的内容

**Description**（仓库简介，一句英文更利于检索）：

```
Turn one sentence into a printable 3D model: DeepSeek + OpenSCAD + STL, with real geometry checks. Windows desktop app.
```

**Topics**（标签）：

```
3d-printing  openscad  deepseek  llm  ai  cad  stl  parametric  windows  desktop-app  react  fastapi  threejs
```

**其它建议**：勾选 *Releases* 展示、在 About 里勾上 *Include in the home page* 的
Topics；如果有讨论需求，打开 *Discussions* 并把使用类问题从 Issues 引导过去。

如果你想让**首次提交**也带上自己的身份，可以在推送前重做一次提交：

```powershell
git commit --amend --reset-author --no-edit
```

---

## 四、构建产物属于 Releases，不要提交进仓库

`git push` 之后，把「给普通用户用的东西」作为 **Release 附件**上传
（这样仓库保持轻量，clone 也快）：

| 附件 | 本地位置 | 体积 |
|---|---|---|
| 一键安装包 | `build\release\AI-3D-Forge-Setup.exe` | 约 16 MB |
| 校验值 | `build\release\CHECKSUMS.txt` | 极小 |
| 免安装版（可选） | `build\dist\AI-3D-Forge\` 打包成 zip | 约 40 MB |
| 源码包（可选） | `build\source\AI-3D-Forge-Source.zip` | 约 3.9 MB |

生成方式：

```powershell
.\scripts\build.ps1          # 前端 + PyInstaller + 安装包
.\scripts\export_source.ps1  # 源码包（含密钥安全门禁）
.\scripts\make_release.ps1 -SkipBuild   # 组装 build\release\（含校验值）
```

---

## 五、推送前自查（每条都能自己跑）

```powershell
# 1) 后端自检（94 项，约 1～2 分钟，不消耗 AI 额度；要本机装了 OpenSCAD）
& build\venv\Scripts\python.exe scripts\selfcheck.py

# 2) 开源文件完整性（必需文件 / README 要点 / 交叉链接）
& build\venv\Scripts\python.exe scripts\check_oss_files.py

# 3) 密钥与隐私扫描：当前文件 + **Git 历史**（发布前必须带 --history）
& build\venv\Scripts\python.exe scripts\check_no_secrets.py --history

# 4) 确认没有把生成物带进来
git status --porcelain -uall | Select-String "build/|node_modules|dist/|Output/|shots/"

# 5) 在**项目之外**克隆一份，跑它自己的自检（"别人 clone 下来能用"最直接的验证）
git clone <你的仓库> <克隆目录>
& build\venv\Scripts\python.exe <克隆目录>\scripts\selfcheck.py
```

第 3 条是关键：**只看当前文件是不够的** —— 密钥可能是"提交过又删掉"，
当前工作区看起来干净，但 `git log -p` 里还留着，任何人 clone 都能翻出来。
`--history` 会把完整补丁过一遍。

第 5 条**注意**：全新 clone 里 `webui/dist` 不存在（构建产物不入库），
所以首页会返回 `503` 并提示 `npm run build` —— 这是**预期行为**，
自检会识别这种情况并跳过界面相关断言（见 `README-开发者.md` §67）。

第 5 条还有一个真实的坑：**不要把克隆目录放在 `build/` 下面**。
「源码编码完好」「无本机路径泄漏」两条守卫会跳过路径里带 `build` 的文件
（因为构建产物本来就不该被扫），于是它们会报
`只扫到 0 个提交文本文件，明显不对` —— 这是守卫**故意**的失败
（"一条什么都没检查的守卫比没有守卫更糟"），不是仓库有问题。
把克隆放到项目之外（或者至少不带 `build` 的路径）即可。

---

## 六、别人 clone 之后怎么跑起来

见 [docs/DEVELOPMENT.md](DEVELOPMENT.md) 与 [README.md](../README.md)：
建 venv → `pip install -r requirements-build.txt` → `cd webui; npm ci; npm run build`
→ `python main.py`。

---

## 七、文档里不要写绝对路径

仓库里有一条自动检查（`scripts/selfcheck.py` 的「无本机路径泄漏」）：
它会用 `Path.home()` 和**当前仓库根目录**在运行时推导出本机路径，
再扫一遍提交集里的所有文本文件。所以：

* 写文档时**只写占位符**：`<仓库目录>`、`<克隆目录>`、`C:\Users\<用户名>`；
* 不要出现 `D:\某人的项目\...` 这类真实路径 —— 那既是隐私泄漏，
  也会让别人的自检失败（守卫会在别人的机器上推导出**别人**的路径，
  所以只有"写着本机路径"的文件才会被它抓到）。

这条规则的**反面教材**就是这份仓库自己踩过的：写开发文档时顺手把一个克隆目录
写成了绝对路径，克隆出来的那份自检立刻报「含本机仓库绝对路径」。
详见 `README-开发者.md` §67。

---

## 八、改完之后怎么同步回仓库

开发树（`<项目目录>`）是唯一真源，仓库目录是它的镜像：

```powershell
# 1) 导出源码集（含密钥门禁 + 必需文件检查）
.\scripts\export_source.ps1

# 2) 镜像到仓库（先 dry-run，看清增删改）
& build\venv\Scripts\python.exe scripts\sync_github_repo.py
& build\venv\Scripts\python.exe scripts\sync_github_repo.py --apply

# 3) 提交
cd <仓库目录>
git add -A
git commit -m "……"
```

仓库目录默认取**与项目同级**的 `AI-3D-Forge-GitHub`；不在同级时用
`--repo <目录>` 指过去。这个脚本必须在 `scripts/` 里（而不是某个被
`.gitignore` 排除的临时目录）：否则别人 clone 之后照本节跑，
第一条命令就指向一个不存在的文件 —— 发布工具自己不在发布物里也是文档骗人。

`sync_github_repo.py` 会跳过 `webui/dist/` 与 `webui/scripts/shots/`
（生成物，属于 Releases 或本地产物），并把要删的文件**逐条全量**打印（不截断，
因为这是它唯一会销毁文件的方向）—— 删文件前请确认那是**真的**不该再存在，
而不是“导出脚本漏了”。
