# OpenSCAD 官方来源与完整性校验

本文件记录 AI 3D Forge 获取 OpenSCAD 的确切来源与校验值，供安全审计与后续维护。

## 一、原则

1. **只用官方渠道**。下载地址固定为 `https://files.openscad.org/`，
   这是 OpenSCAD 官方项目在其官网 [openscad.org](https://openscad.org/downloads.html)
   上链接的发布镜像。
2. **不打包、不分发 OpenSCAD 二进制**。本项目的安装包与发布包中**不含**任何
   OpenSCAD 可执行文件。程序在用户确认后按需下载。
3. **下载后必须校验**。每个文件都钉死 SHA-256，校验不通过立即删除并中止安装，
   同时向用户说明原因（可能是下载中断或网络中间人篡改）。
4. **绝不静默失败**。任何失败都转换为用户可读的中文说明 + 官方手动安装入口。

## 二、当前钉定的版本

| 项目 | 值 |
|---|---|
| 版本 | `2021.01`（OpenSCAD 当前的正式稳定版） |
| 平台 | Windows x86-64 |
| 许可证 | GPL-2.0-or-later |

### 免安装包（默认安装方式，不需要管理员权限）

```
URL    https://files.openscad.org/OpenSCAD-2021.01-x86-64.zip
文件    OpenSCAD-2021.01-x86-64.zip
大小    21,884,613 字节
SHA-256 fb0caabf5bbc89f8f2f80c10b79ae64d697aaff6efd58b2756f5d6270edb7ba7
```

解压后结构（`find_in_directory` 按此结构定位可执行文件）：

```
openscad-2021.01/
├── openscad.exe      ← CLI 主程序（-o out.stl in.scad）
├── openscad.com
├── libraries/
├── examples/
├── fonts/
├── locale/
└── color-schemes/
```

### 官方安装程序（备用方式，需要管理员权限）

```
URL    https://files.openscad.org/OpenSCAD-2021.01-x86-64-Installer.exe
文件    OpenSCAD-2021.01-x86-64-Installer.exe
大小    21,876,824 字节
SHA-256 fbe1e590e1af2af863f5d92bd263eb4644d0ee29352e3ab566366e87a8f6ed12
类型    NSIS 安装程序（支持 /S 静默安装、/D= 指定目录）
```

### 数字签名情况（实测，如实记录）

执行 `Get-AuthenticodeSignature` 的结果是 **NotSigned**。
即 OpenSCAD 官方 2021.01 的 Windows 安装包**没有提供数字签名**。

因此本项目的做法是：
- **不**向用户宣称「已验证数字签名」；
- 改用 SHA-256 完整性校验，并在界面上如实说明；
- 同时提供官网链接，让用户可以完全绕开本软件的自动安装，自行从官网安装。

> 这一点落在 `openscad.verify_download()`：它只会说「已校验文件完整性（SHA-256 与官方
> 发布值一致）」，官方没给校验值时则如实说「未提供校验值，已记录本机 SHA-256」，
> 不掺进任何「签名已验证」的说法。
>
> （这里曾经写着一个叫 authenticate_signature 的函数：它根本没人调用，只让文档看上去
> 多了一层并不存在的签名检查能力 —— 本轮当作死代码删掉了。约定：文档里写成
> `xxx()` 就是声称它现在存在，自检会挨个核对；想记历史就写成不带括号的名字。）

## 三、更新到新版本时要做的事

OpenSCAD 发布新版本后，按以下步骤更新（`src/aiforge/openscad.py`）：

1. 从 `https://files.openscad.org/` 下载新的 zip 与 Installer。
2. 计算并核对 SHA-256：
   ```powershell
   Get-FileHash .\OpenSCAD-<版本>-x86-64.zip -Algorithm SHA256
   ```
3. 更新 `ASSET_ZIP`、`ASSET_INSTALLER` 与 `PINNED_SHA256`（版本号就写在两个资产
   文件名里，没有单独的版本常量）。
4. **实测验证**（关键，不能只看文件下载成功）：
   ```powershell
   .\build\venv\Scripts\python.exe main.py --install-openscad
   .\build\venv\Scripts\python.exe scripts\selfcheck.py
   ```
   必须看到「OpenSCAD 自动安装」与「OpenSCAD 检测与 CLI」两项通过。
5. 检查新版本的输出编码是否仍为系统 ANSI 代码页（见下）。
6. 如果新版本的 zip 内部目录结构变了，更新 `find_in_directory` 的搜索深度与
   `search_directories()` 的候选路径，并同步更新 `installer/AI-3D-Forge.iss`
   里的 `FindOpenScad()` 候选列表。
7. 更新本文件与 `README-开发者.md` 的「踩过的坑」。

## 四、已实测确认的 OpenSCAD 2021.01 行为

这些是打包与自动安装逻辑必须依赖的事实，已在本机实测：

| 行为 | 实测结果 | 对应代码 |
|---|---|---|
| 版本输出 | `openscad --version` 输出到 **stderr**：`OpenSCAD version 2021.01` | `openscad.read_version()` |
| 控制台编码 | 按**系统 ANSI 代码页**输出。中文 Windows 下是 GBK：路径里的汉字每个占 2 字节，按 UTF-8 解码就成了乱码 | `openscad.console_encoding()` / `output_of()` |
| 文件编码 | **不解析带 BOM 的 .scad 文件**，报 `Parser error: syntax error in file ..., line 1` | `scad.ensure_no_bom()` |
| 中文路径 | **可用**。含中文的目录（例如 `D:\项目\模型\`）能正常编译出 STL | 无需特殊处理，但输出解码必须正确 |
| STL 格式 | 默认输出**二进制** STL，且前 80 字节头部以 `solid ...` 开头 | `mesh._looks_binary()` 用长度自校验而非前缀判断 |
| 空几何 | 渲染出 0 体积几何时退出码仍可能为 0，但 STL 为空或体积为 0 | `compiler` 检查文件存在且非空；`mesh` 检查体积 > 0 |
| 免安装包可用性 | zip 解压后 `openscad.exe` **可直接运行**，无需安装、无需管理员权限，CLI 编译 10mm 立方体耗时约 0.3–0.4 秒 | `openscad.install_portable()` |

## 五、安装后的验证流程

自动安装完成后，程序一定会执行（`openscad._finalize()`）：

1. **再次检测**：`detect()` 重新扫描并读取版本号
2. **CLI 冒烟测试**：真实编译 `cube([10,10,10], center=true);` 并确认生成非空 STL
3. **写入配置**：把 `openscad.exe` 路径与版本写入 `%APPDATA%\AI-3D-Forge\config.json`
4. **汇报结论**：界面上显示「OpenSCAD 已安装并配置完成。」

任何一步失败都会以带原因的中文错误结束，并提供官方手动安装入口。

## 六、用户可以完全绕开自动安装

用户随时可以：

1. 访问 <https://openscad.org/downloads.html>
2. 下载 Windows 安装包并双击安装
3. 回到软件点击「重新检测」

程序会搜索以下位置，**不假设 OpenSCAD 只装在 `C:\Program Files\OpenSCAD\`**：

- 用户配置中记录的位置
- `PATH` 环境变量
- 注册表卸载信息（HKLM / HKLM\WOW6432Node / HKCU）
- `OPENSCAD_PATH` / `OPENSCAD_HOME` / `OPENSCAD_DIR` 环境变量
- 各盘符（C/D/E/F 及程序所在盘）下的 `Program Files\OpenSCAD`、
  `Program Files (x86)\OpenSCAD`、`OpenSCAD`、`Programs\OpenSCAD`、`Software\OpenSCAD`
- `%LOCALAPPDATA%\Programs\OpenSCAD`、`%LOCALAPPDATA%\OpenSCAD`
- `%LOCALAPPDATA%\Microsoft\WinGet\Packages`（winget）
- `%ProgramData%\chocolatey\bin` 与 `lib\openscad\tools`（Chocolatey）
- `~\scoop\apps\openscad\current`（Scoop）
- 本程序自己的运行目录 `%LOCALAPPDATA%\AI-3D-Forge\runtime\openscad\`

并支持在界面上手动指定 `openscad.exe`（`POST /api/openscad/locate`），
指定时会真实执行 `--version` 校验，不接受无效路径。
