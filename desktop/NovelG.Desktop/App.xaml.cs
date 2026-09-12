using System.IO;
using System.Windows;

namespace NovelG.Desktop;

public partial class App : Application
{
    private FileStream? instanceLock;
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        try
        {
            var options = DesktopOptions.Parse(e.Args);
            Directory.CreateDirectory(Path.Combine(options.DataDirectory, "reports"));
            try
            {
                instanceLock = new FileStream(Path.Combine(options.DataDirectory, "reports", "desktop-window.lock"),
                    FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
            }
            catch (IOException)
            {
                MessageBox.Show("Novel-G 桌面版已在运行，请回到已有窗口。\nNovel-G is already running. Return to its existing window.", "Novel-G");
                Shutdown(2);
                return;
            }
            DispatcherUnhandledException += (_, error) =>
            {
                Directory.CreateDirectory(Path.Combine(options.DataDirectory, "logs"));
                File.AppendAllText(Path.Combine(options.DataDirectory, "logs", "desktop-host.log"),
                    $"{DateTimeOffset.UtcNow:O} {error.Exception.GetType().Name}\n");
                MessageBox.Show("桌面窗口遇到错误，请查看日志后重试。\nThe desktop window encountered an error. Check its logs and retry.", "Novel-G");
                error.Handled = true;
            };
            MainWindow = new MainWindow(options);
            MainWindow.Show();
        }
        catch (Exception error)
        {
            MessageBox.Show($"无法启动 Novel-G：{error.Message}\nUnable to start Novel-G.", "Novel-G");
            Shutdown(1);
        }
    }
    protected override void OnExit(ExitEventArgs e)
    {
        instanceLock?.Dispose();
        base.OnExit(e);
    }
}
