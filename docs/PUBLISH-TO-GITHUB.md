# 发布到 GitHub

这份文档说明「把这个仓库推到 GitHub 需要做什么、以及哪些东西**不该**推」。

---

## 一、仓库在哪

已经为你准备好一个**独立的、可直接推送的仓库**（不在项目目录里面）：
与项目目录**同级**的一个文件夹，名字是 `AI-3D-Forge-GitHub`
（具体绝对路径见交付说明 —— 文档里刻意不写绝对路径，见下面第七节）。

它是一次干净的初始提交（128 个文件），包含：

| 内容 | 说明 |
|---|---|
| `src/`、`main.py`、`webui/` | 全部源码 |
| `scripts/` | 构建、打包、自检、端到端、安装验收脚本 |
| `docs/` | 接口契约、第三方许可清单、OpenSCAD 来源说明 |
| `installer/` | Inno Setup 打包脚本 |
| `README*.md`、`.gitignore`、`.gitattributes` | 文档与仓库约定 |

**不含**（都是生成物，被 `.gitignore` 排除）：`build/`、`webui/node_modules/`、
`webui/dist/`、`installer/Output/`、任何密钥与本机路径。

---

## 二、发布前必须做的一件事：选定开源许可证

仓库里**没有** `LICENSE` 文件。这不是遗漏 —— 选定许可证是**作者本人的决定**，
不能代替你选。当前状态：

* GitHub 会显示 "no license"；
* 按默认版权规则，他人**无权**使用 / 修改 / 再分发你的代码。

选定后把许可证全文存成仓库根目录的 `LICENSE`（GitHub 会自动识别并显示在仓库页）。
常见选择：MIT、Apache-2.0（宽松），或 GPLv2+（与本项目调用的 OpenSCAD 同一体系）。

注意区分：`docs/LICENSE.txt` 是**第三方组件清单与免责声明**（Python 包、前端包、
OpenSCAD 的 GPLv2 文本），不是本项目的授权条款。

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
| 源码包（可选） | `build\source\AI-3D-Forge-Source.zip` | 约 3.7 MB |

生成方式：

```powershell
.\scripts\build.ps1          # 前端 + PyInstaller + 安装包
.\scripts\export_source.ps1  # 源码包（含密钥安全门禁）
.\scripts\make_release.ps1   # 组装 build\release\（含校验值）
```

---

## 五、推送前自查（每条都能自己跑）

```powershell
# 1) 后端自检（41 项，秒级，不消耗 AI 额度）
& build\venv\Scripts\python.exe scripts\selfcheck.py

# 2) 仓库里没有密钥
git grep -nEI "sk-[0-9a-f]{20,}"     # 应当无输出

# 3) 仓库里没有本机路径（自检里的「无本机路径泄漏」会运行时推导本机路径再扫）
& build\venv\Scripts\python.exe scripts\selfcheck.py | Select-String "无本机路径"

# 4) 确认没有把生成物带进来
git status --porcelain -uall | Select-String "build/|node_modules|dist/|Output/"

# 5) 在**项目之外**克隆一份，跑它自己的自检
git clone <你的仓库> <克隆目录>
$env:AIFORGE_LOCAL_DIR = '<已有 OpenSCAD 的本地目录>'
& build\venv\Scripts\python.exe <克隆目录>\scripts\selfcheck.py
```

第 5 条是「别人 clone 下来能用」最直接的验证。
**注意**：全新 clone 里 `webui/dist` 不存在（构建产物不入库），
所以首页会返回 `503` 并提示 `npm run build` —— 这是**预期行为**，
自检会识别这种情况并跳过界面相关断言（见 `README-开发者.md` §67）。

---

## 六、别人 clone 之后怎么跑起来

见 [README.md](../README.md) 的「快速开始 → 开发者」：
建 venv → `pip install -r requirements-build.txt` → `cd webui; npm install; npm run build`
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
