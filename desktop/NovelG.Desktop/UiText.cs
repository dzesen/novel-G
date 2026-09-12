namespace NovelG.Desktop;

public static class UiText
{
    private static readonly Dictionary<string, (string Zh, string En)> Values = new()
    {
        ["file"] = ("文件", "File"), ["view"] = ("查看", "View"),
        ["start"] = ("全部启动", "Start workspace"), ["stop"] = ("停止服务", "Stop services"),
        ["exit"] = ("退出", "Exit"), ["workspace"] = ("回到工作台", "Return to workspace"),
        ["status"] = ("运行状态", "Service status"), ["logs"] = ("打开日志", "Open logs"),
        ["data"] = ("打开数据目录", "Open data folder"),
        ["intro"] = ("从一个故事开始，在自己的电脑上安心写作。", "A quiet place to shape your story, on your own computer."),
        ["idle"] = ("工作台尚未启动", "Your workspace is ready to start"),
        ["idle_detail"] = ("点击“全部启动”即可进入。运行环境已随桌面版提供，无需单独配置。", "Start the workspace to begin. The desktop edition includes its runtime dependencies."),
        ["local"] = ("小说与配置保存在此电脑。AI 功能由你配置并确认后使用。", "Your novels and settings stay on this computer. AI features run only when you configure and authorize them."),
        ["starting"] = ("正在准备工作台", "Preparing your workspace"),
        ["database"] = ("正在启动本地数据库…", "Starting the local database…"),
        ["backend"] = ("正在检查写作服务…", "Checking the writing service…"),
        ["frontend"] = ("正在加载写作界面…", "Loading the writing interface…"),
        ["running"] = ("工作台正在运行", "Workspace is running"),
        ["running_detail"] = ("可以回到工作台继续写作。关闭窗口时会先正常停止本地服务。", "Return to the workspace to write. Closing this window shuts down local services first."),
        ["stopping"] = ("正在安全停止", "Stopping services"),
        ["stopping_detail"] = ("正在等待写入结束并关闭数据库，请稍候。", "Waiting for writes to finish and closing the database. Please wait."),
        ["failed"] = ("工作台未能就绪", "The workspace could not start"),
        ["failed_detail"] = ("请打开日志查看原因，修复后重试。", "Open the logs for details, then retry after resolving the problem."),
        ["unsupported_cpu"] = ("此电脑的处理器不满足数据库所需的 AVX 指令集要求。", "This processor does not support the AVX instructions required by the database."),
        ["runtime_missing"] = ("桌面版运行文件不完整，请重新安装或修复安装。", "Desktop runtime files are incomplete. Reinstall or repair the installation."),
        ["port_busy"] = ("无法分配本地端口，请关闭冲突程序后重试。", "A local port could not be allocated. Close the conflicting application and retry."),
        ["database_failed"] = ("本地数据库未能启动。请检查磁盘空间和日志后重试。", "The local database could not start. Check disk space and the logs, then retry."),
        ["service_failed"] = ("写作服务未能就绪。请打开日志查看原因。", "The writing service did not become ready. Open the logs for details."),
        ["stop_blocked"] = ("服务尚未安全退出，已保留数据库运行。请回到工作台结束任务后重试停止。", "Services have not stopped safely. The database remains running. Finish active work and retry stopping."),
        ["already_running"] = ("此数据目录已有桌面服务运行，请回到原窗口。", "Desktop services already own this data directory. Return to the existing window."),
        ["data_incompatible"] = ("此数据目录由不兼容的桌面版本创建，请使用原版本或恢复备份。", "This data directory belongs to an incompatible desktop version. Use that version or restore a backup."),
        ["webview_failed"] = ("桌面显示组件未能加载，请打开日志或修复安装。", "The desktop rendering component could not load. Check the logs or repair the installation."),
    };
    public static string Get(string key, string language) => Values.TryGetValue(key, out var pair)
        ? (language == "zh" ? pair.Zh : pair.En) : Get("failed_detail", language);
}
