using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;
using Microsoft.Web.WebView2.Core;

namespace NovelG.Desktop;

public partial class MainWindow : Window
{
    private readonly DesktopOptions options;
    private readonly DesktopController controller = new();
    private string language;
    private string state = "idle";
    private string detail = "idle_detail";
    private bool closing;
    private bool allowClose;
    private Uri? origin;
    private bool webviewReady;
    private string? initializationScript;

    public MainWindow(DesktopOptions options)
    {
        this.options = options;
        language = options.Language;
        InitializeComponent();
        controller.Message += message => Dispatcher.InvokeAsync(() => HandleMessage(message));
        controller.Exited += () => Dispatcher.InvokeAsync(() =>
        {
            if (closing) { allowClose = true; Close(); }
            else SetState(state is "idle" or "failed" ? state : "failed", state is "idle" or "failed" ? detail : "service_failed");
        });
        Closing += OnClosing;
        Closed += (_, _) => { Browser.Dispose(); controller.Dispose(); };
        RenderText();
        if (options.AutoStart) Loaded += (_, _) => Start_Click(this, new RoutedEventArgs());
    }

    private string T(string key) => UiText.Get(key, language);
    private void RenderText()
    {
        FileMenu.Header = T("file"); ViewMenu.Header = T("view");
        StartMenu.Header = StartButton.Content = T("start"); StopMenu.Header = T("stop");
        ExitMenu.Header = T("exit"); WorkspaceMenu.Header = ReturnButton.Content = T("workspace");
        StatusMenu.Header = T("status"); LogsMenu.Header = LogButton.Content = T("logs");
        DataMenu.Header = T("data"); IntroText.Text = T("intro"); LocalText.Text = T("local");
        StatusTitle.Text = T(state); StatusDetail.Text = T(detail);
        VersionText.Text = "Desktop preview 0.1.0 · Windows x64";
    }

    private void SetState(string next, string message)
    {
        state = next; detail = message;
        bool running = state == "running";
        bool busy = state is "starting" or "stopping";
        StartMenu.IsEnabled = StartButton.IsEnabled = !busy && !controller.IsAlive;
        StopMenu.IsEnabled = controller.IsAlive && state != "stopping";
        WorkspaceMenu.IsEnabled = running && webviewReady;
        ReturnButton.Visibility = running && webviewReady ? Visibility.Visible : Visibility.Collapsed;
        Busy.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        if (!running) { Browser.Visibility = Visibility.Collapsed; StartupView.Visibility = Visibility.Visible; }
        RenderText();
    }

    private async void HandleMessage(JsonElement message)
    {
        if (!message.TryGetProperty("state", out var status)) return;
        string next = status.GetString() ?? "failed";
        string code = message.TryGetProperty("detail", out var value) ? value.GetString() ?? "failed_detail" : next + "_detail";
        if (code == "stop_blocked") closing = false;
        if (next == "running")
        {
            try
            {
                var workspace = new Uri(message.GetProperty("frontend").GetString()!);
                var api = new Uri(message.GetProperty("backend").GetString()!);
                if (!OriginPolicy.IsLoopbackHttp(workspace) || !OriginPolicy.IsLoopbackHttp(api))
                    throw new InvalidDataException("Invalid desktop origin");
                await OpenWorkspace(workspace, api);
            }
            catch (Exception error)
            {
                Log(error); SetState("failed", "webview_failed"); return;
            }
        }
        SetState(next, code);
        if (next == "running") Workspace_Click(this, new RoutedEventArgs());
    }

    private async Task OpenWorkspace(Uri workspace, Uri api)
    {
        origin = workspace;
        if (!webviewReady)
        {
            var manifest = RuntimeManifest.Load(options.RuntimeDirectory);
            var environmentOptions = new CoreWebView2EnvironmentOptions();
            if (options.DebugPort is { } port) environmentOptions.AdditionalBrowserArguments = $"--remote-debugging-port={port}";
            var environment = await CoreWebView2Environment.CreateAsync(
                RuntimeManifest.Resolve(options.RuntimeDirectory, manifest.WebView2),
                Path.Combine(options.DataDirectory, "webview2"), environmentOptions);
            await Browser.EnsureCoreWebView2Async(environment);
            Browser.CoreWebView2.Settings.AreDevToolsEnabled = options.DebugPort.HasValue;
            Browser.CoreWebView2.Settings.AreHostObjectsAllowed = false;
            Browser.CoreWebView2.Settings.IsWebMessageEnabled = false;
            Browser.CoreWebView2.PermissionRequested += (_, e) => e.State = CoreWebView2PermissionState.Deny;
            Browser.CoreWebView2.NavigationStarting += (_, e) =>
            {
                if (Uri.TryCreate(e.Uri, UriKind.Absolute, out var uri) && origin is not null && OriginPolicy.IsWorkspace(uri, origin)) return;
                e.Cancel = true;
                if (e.IsUserInitiated) OpenExternal(e.Uri);
            };
            Browser.CoreWebView2.NewWindowRequested += (_, e) => { e.Handled = true; if (e.IsUserInitiated) OpenExternal(e.Uri); };
            Browser.CoreWebView2.ProcessFailed += (_, _) => SetState("failed", "webview_failed");
            webviewReady = true;
        }
        if (initializationScript is not null) Browser.CoreWebView2.RemoveScriptToExecuteOnDocumentCreated(initializationScript);
        string allowed = JsonSerializer.Serialize(workspace.GetLeftPart(UriPartial.Authority));
        string apiBase = JsonSerializer.Serialize(api.GetLeftPart(UriPartial.Authority));
        initializationScript = await Browser.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync(
            $"if(location.origin === {allowed}) Object.defineProperty(window, '__NOVEL_G_DESKTOP__', {{value: Object.freeze({{apiBase:{apiBase}}}), writable:false, configurable:false}});");
        Browser.CoreWebView2.Navigate(new Uri(workspace, "/" + language).AbsoluteUri);
    }

    private void OpenExternal(string address)
    {
        if (Uri.TryCreate(address, UriKind.Absolute, out var uri) && OriginPolicy.CanOpenExternal(uri))
            try { Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true }); }
            catch (Exception error) { Log(error); }
    }
    private void Start_Click(object sender, RoutedEventArgs e)
    {
        if (controller.IsAlive) return;
        try { controller.Start(options, RuntimeManifest.Load(options.RuntimeDirectory)); SetState("starting", "database"); }
        catch (Exception error) { Log(error); SetState("failed", "runtime_missing"); }
    }
    private async void Stop_Click(object sender, RoutedEventArgs e)
    {
        SetState("stopping", "stopping_detail");
        await controller.SendAsync("stop");
    }
    private void Workspace_Click(object sender, RoutedEventArgs e)
    {
        if (!webviewReady || state != "running") return;
        StartupView.Visibility = Visibility.Collapsed; Browser.Visibility = Visibility.Visible;
    }
    private void Status_Click(object sender, RoutedEventArgs e)
    { Browser.Visibility = Visibility.Collapsed; StartupView.Visibility = Visibility.Visible; }
    private void Folder(string path)
    { Directory.CreateDirectory(path); Process.Start(new ProcessStartInfo(path) { UseShellExecute = true }); }
    private void Logs_Click(object sender, RoutedEventArgs e) => Folder(Path.Combine(options.DataDirectory, "logs"));
    private void Data_Click(object sender, RoutedEventArgs e) => Folder(options.DataDirectory);
    private void Chinese_Click(object sender, RoutedEventArgs e) => ChangeLanguage("zh");
    private void English_Click(object sender, RoutedEventArgs e) => ChangeLanguage("en");
    private void ChangeLanguage(string value)
    { language = value; RenderText(); if (webviewReady && origin is not null && state == "running") Browser.CoreWebView2.Navigate(new Uri(origin, "/" + language).AbsoluteUri); }
    private void Exit_Click(object sender, RoutedEventArgs e) => Close();
    private async void OnClosing(object? sender, CancelEventArgs e)
    {
        if (allowClose || !controller.IsAlive) return;
        e.Cancel = true;
        if (closing) return;
        closing = true; SetState("stopping", "stopping_detail");
        await controller.SendAsync("stop");
    }
    private void Log(Exception error)
    {
        string directory = Path.Combine(options.DataDirectory, "logs"); Directory.CreateDirectory(directory);
        File.AppendAllText(Path.Combine(directory, "desktop-host.log"), $"{DateTimeOffset.UtcNow:O} {error.GetType().Name}: {error.Message}\n");
    }
}
