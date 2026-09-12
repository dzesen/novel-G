# 上游与第三方声明

## 项目来源

Novel-G 基于 [YILING0013/AI_NovelGenerator](https://github.com/YILING0013/AI_NovelGenerator) 的 [dev 分支](https://github.com/YILING0013/AI_NovelGenerator/tree/dev) 开发，实际继承快照为 [`9f8504f2833102bb65b1b7cf72c6a499fe989930`](https://github.com/YILING0013/AI_NovelGenerator/tree/9f8504f2833102bb65b1b7cf72c6a499fe989930)（2026-05-17）。感谢原项目作者 YILING0013 及所有贡献者，保留原有署名、版权与提交归属。

当前维护仓库为 [dzesen/novel-G](https://github.com/dzesen/novel-G)。Novel-G 在此基础上持续调整生成授权与恢复、资料卡互操作、人物状态和插图工作流，后续改动可通过 Git 历史追溯；这些修改不代表上游作者背书。

Novel-G 采用 [GNU AGPL v3.0](../LICENSE)（`AGPL-3.0-only`）。LICENSE 文本原样沿用上游 main 分支，实际代码来源及历史许可记录见[许可证与来源](license-status.zh-CN.md)。原作者和第三方材料的权利不会因本项目的后续修改而改变。

## 测试材料的分发范围

外部测试样本保留在私有开发库，不随 rc.3 起的用户安装包和对外源码导出分发。完整开发库保留这些样本的原始来源、许可和归属文件；单独取用时仍应遵循各自许可。

## 安装时获取的依赖

源码包不捆绑 Python、Node.js、MongoDB、Python site-packages 或 npm node_modules。依赖在安装时从相应软件源获取，继续受各自许可约束。

主要 Python 依赖包括 FastAPI / Pydantic / PyYAML / AnyIO / pytest（MIT）、Uvicorn / httpx（BSD-3-Clause）、OpenAI SDK / Google GenAI SDK / PyMongo / python-multipart（Apache-2.0）、CustomTkinter（其包内许可）等；确切版本见 `requirements.txt`。前端依赖及各包声明的许可记录在 `frontend/package-lock.json`。

源码包不捆绑运行环境。[Windows 桌面预览版](windows-desktop.zh-CN.md)另行随附运行时及第三方通知，并在同一 Release 提供应用与 MongoDB 对应源码；各组件继续遵循各自许可。
