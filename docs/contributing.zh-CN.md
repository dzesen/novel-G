# 参与 Novel-G

Novel-G 是面向长篇小说的本地创作工作台。问题与建议通过 [GitHub Issues](https://github.com/dzesen/novel-G/issues) 讨论。提交较大的功能或重构前，请说明实际写作场景、当前行为和预期结果。

## 开发环境

准备 Windows 10/11、64 位 Python 3.11 或 3.12、Node.js 22.13+ 或 24.x，以及 MongoDB 7/8。运行 `setup.bat` 安装依赖，运行 `start.bat` 启动应用。无 API Key 也能开发手动写作功能。

公开仓库包含应用源码、安装入口、用户文档及构建检查。内部规格、开发过程记录和维护者的完整回归用例不在公开范围内。后端代码位于 `backend/`，前端代码位于 `frontend/src/`。

## 提交前检查

在已安装依赖的项目目录中执行：

```powershell
.\.venv\Scripts\python.exe -m compileall -q backend scripts main.py launcher.py
npm run lint --prefix frontend
npm run build --prefix frontend
.\.venv\Scripts\python.exe -m backend.preflight
```

运行预检需要 MongoDB 可连接。这些检查验证源码可编译、前端可构建及运行环境就绪，不会调用付费模型。请另外实际操作本次修改涉及的功能，并在提交说明中记录步骤、结果和使用的合成数据。界面变化需要同时检查桌面和 375px 窄屏。

公开仓库的 CI 还会检查文件与历史的公开范围、文档链接、许可证元数据、依赖安全公告、凭据泄漏和源码打包。完整业务回归由维护者在开发环境中执行；构建检查不能代替业务回归。

## 修改约定

- 每个提交只处理一个可独立验证的问题。提交信息使用 `type(scope): summary` 英文格式，用户文档使用中文。
- 生成上下文只能使用章节大纲声明的正式资料 ID；外部名称和占位符不能自动变成内部 ID。
- 付费调用须由用户显式授权，自动任务保持在既定范围和预算内。未完成正文不能自动进入正式正文和状态链。
- 修改人物、地点等影响生成上下文的资料时，须同步推进 narrative revision，使上下文缓存失效。
- API Key 只放在被忽略的 `backend/config/config.yaml`。不要提交运行配置、数据库、备份、日志、小说正文、用户图片或真实账号凭据。

问题报告请包含复现步骤、预期与实际结果、系统及运行环境版本，以及脱敏错误信息。真实 Provider 验收会产生费用，须单独明确授权。

## 许可证与贡献

本项目代码采用 [GNU AGPL v3.0](../LICENSE)（`AGPL-3.0-only`），贡献的本项目代码沿用该许可。项目来源见[许可证与来源](license-status.zh-CN.md)，第三方材料见[上游与第三方声明](third-party-notices.zh-CN.md)。提交者须有权贡献相关代码、图片和资料；引入第三方素材时保留来源、许可证与修改说明。
