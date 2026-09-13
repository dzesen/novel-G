# Windows 桌面预览版

版本 `0.1.0-preview.2`，发布日期 2026-09-14。采用 WPF + WebView2，包含 Python 后端、Node.js 前端和独立 MongoDB 数据库。

## 本次更新

- 修复书库更新时间显示为 `Invalid Date`，恢复按更新时间排序。
- 创意定向默认输出上限从 4,096 调整到 16,384 token；手动指定的上限仍优先。
- 蓝图结构化输出修复失败时提供可操作的错误说明；不会自动扩大用户已授权的调用范围。

## 下载与安装

从 [GitHub Release](https://github.com/dzesen/novel-G/releases/tag/desktop-v0.1.0-preview.2) 下载 `Novel-G-Desktop-0.1.0-preview.2-win-x64-setup.exe`。安装包内置 Python、Node.js、MongoDB、.NET 和 WebView2。系统须预先安装 Microsoft Visual C++ x64 运行库 14.51.36247 或更新版本；本包不额外捆绑 VC++，也不安装 Visual Studio。缺少该依赖时，安装器会提示并可打开[微软官方下载说明](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist)，安装运行库后重新运行本安装包。已具备运行库的电脑可离线安装 Novel-G。`SHA256SUMS.txt` 用于核对附件完整性。

运行安装包，完成后打开 Novel-G，点击“全部启动”。首次进入工作台创建本机管理员。手动建书和写作不需要模型 API Key；AI 功能和 ComfyUI 由用户自行配置，模型权重不包含在包内。

这是未签名的早期预览版。已在 Windows 10 Pro 22H2 x64（19045）验证安装、连续启停、建书、重启持久化、升级和卸载保留数据。Windows 11 实机、干净普通账户及断网虚拟机的完整验收尚未完成；不支持 Windows 7/8 或 ARM64 原生运行。安装器最低版本为 Windows 10 22H2。

## 数据与升级

程序默认安装到 `%LOCALAPPDATA%\Programs\Novel-G`，数据独立保存在 `%LOCALAPPDATA%\Novel-G\Data`。数据库仅监听本机回环地址并启用认证，端口与凭据由程序管理。配置、小说、数据库、日志和备份不会写入程序目录。

升级前暂停或结束生成任务，完成应用备份并关闭桌面程序，然后运行新版安装包。升级保留数据；卸载只移除程序，默认保留数据目录。退出窗口沿用现有生成任务中断与恢复语义，不承诺等待整本生成完成。请勿在程序运行时直接复制 MongoDB 数据文件。

桌面版不会自动导入或接管源码版数据库。迁移可以使用设置页的备份与恢复功能，图片、配置和上传大小限制见[已知局限](known-limitations.zh-CN.md)。暂未提供自动更新、整机迁移向导和完整断电恢复保证。

## 源码与构建

Release 的源码附件及对应 tag 提供此版本的应用源码与构建脚本。桌面版版本号独立于内部工作台的 `VERSION` 标识。项目源码遵循 [AGPL-3.0-only](../LICENSE)，上游署名与第三方权利见[第三方声明](third-party-notices.zh-CN.md)。

在 Windows x64 构建机安装 Python 3.12，创建 `.venv` 并安装 `requirements.txt`，然后运行：

```powershell
powershell -ExecutionPolicy Bypass -File desktop/build.ps1
```

构建脚本联网下载固定版本并验证摘要，在 `reports/` 内生成独立构建目录与安装包。桌面宿主源码在 `desktop/NovelG.Desktop/`；打包器只收录公开应用模块及默认配置，排除本机配置、数据、密钥、内部验收模块和开发历史。可自行修改源码、重新构建并运行，无签名或专用密钥限制。

## 随附组件

| 组件 | 固定版本 | 许可与来源 |
| --- | --- | --- |
| MongoDB Community | 8.0.32 | SSPL v1，保留包内 LICENSE-Community.txt 与 THIRD-PARTY-NOTICES |
| Python | 3.12.14 | PSF，包内 licenses/Python-LICENSE.txt |
| Node.js | 24.21.0 | MIT 及第三方通知，包内 node/LICENSE |
| .NET Runtime | 10.0.12 | MIT 及第三方通知，包内 licenses/ |
| WebView2 Fixed Runtime | 153.0.4234.32 | Microsoft 软件许可及内置第三方通知 |
| Visual C++ x64 Runtime | 系统前置依赖，最低 14.51.36247 | 用户直接从 Microsoft 安装，不额外随包分发 |

MongoDB 使用未经修改的官方程序，版本对应源码为 [mongodb/mongo 官方 r8.0.32 标签](https://github.com/mongodb/mongo/tree/r8.0.32)。同一 Release 提供其源码归档下载，含构建脚本和第三方源码。MongoDB 的 SSPL 和其他随附组件许可分别适用于各自组件，不因安装包采用 AGPL 而改变。

应用源码、MongoDB 对应源码与校验文件均可免费获取。依赖的下载地址和摘要保存在 `desktop/runtime-lock.json`。随包固定运行时随桌面包更新，不随系统自动升级；系统 VC++ 运行库通过微软更新。第三方原始运行时自带的组件与通知保持原样。

Windows 10 兼容性结论仅来自上述实测。MongoDB 8.0 当前官方支持列表未列 Windows 10，.NET 10 对 Windows 10 的支持也有版本限制，见 [MongoDB 平台说明](https://www.mongodb.com/docs/community-platform-support/)及 [.NET Windows 支持表](https://learn.microsoft.com/en-us/dotnet/core/install/windows)。
