using System.Diagnostics;
using System.IO;
using System.Text.Json;

namespace NovelG.Desktop;

public sealed class DesktopController : IDisposable
{
    private Process? process;
    private readonly SemaphoreSlim inputLock = new(1, 1);
    public event Action<JsonElement>? Message;
    public event Action? Exited;
    public bool IsAlive => process is { HasExited: false };
    public void Start(DesktopOptions options, RuntimeManifest manifest)
    {
        if (IsAlive) return;
        process?.Dispose();
        var start = new ProcessStartInfo(RuntimeManifest.Resolve(options.RuntimeDirectory, manifest.Backend))
        {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            WorkingDirectory = options.DataDirectory,
            StandardOutputEncoding = System.Text.Encoding.UTF8,
            StandardErrorEncoding = System.Text.Encoding.UTF8,
        };
        foreach (var arg in new[] { "supervise", "--runtime-root", options.RuntimeDirectory,
            "--data-dir", options.DataDirectory, "--lang", options.Language }) start.ArgumentList.Add(arg);
        start.Environment["PYTHONUTF8"] = "1";
        process = new Process { StartInfo = start, EnableRaisingEvents = true };
        process.Exited += (_, _) => Exited?.Invoke();
        process.OutputDataReceived += (_, e) =>
        {
            if (e.Data is not { Length: > 0 and < 65536 }) return;
            try
            {
                using var document = JsonDocument.Parse(e.Data);
                Message?.Invoke(document.RootElement.Clone());
            }
            catch (JsonException) { }
        };
        process.ErrorDataReceived += (_, _) => { /* Engine diagnostics stay in its local log. */ };
        if (!process.Start()) throw new IOException("Unable to start desktop services");
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
    }
    public async Task SendAsync(string command)
    {
        await inputLock.WaitAsync();
        try
        {
            if (process is not { HasExited: false }) return;
            await process.StandardInput.WriteLineAsync(JsonSerializer.Serialize(new { command }));
            await process.StandardInput.FlushAsync();
        }
        finally { inputLock.Release(); }
    }
    public void Dispose()
    {
        // Closing stdin asks the supervisor to shut down normally, never kills MongoDB.
        if (process is { HasExited: false }) process.StandardInput.Close();
        process?.Dispose();
        inputLock.Dispose();
    }
}
