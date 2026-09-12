using System.Globalization;
using System.IO;

namespace NovelG.Desktop;

public sealed record DesktopOptions(string RuntimeDirectory, string DataDirectory, string Language,
    bool AutoStart, int? DebugPort)
{
    public static DesktopOptions Parse(string[] args)
    {
        string root = Path.GetFullPath(AppContext.BaseDirectory);
        string data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Novel-G", "Data");
        string language = CultureInfo.CurrentUICulture.TwoLetterISOLanguageName == "zh" ? "zh" : "en";
        bool start = false;
        int? debugPort = null;
        for (int i = 0; i < args.Length; i++)
        {
            string Value() => ++i < args.Length ? args[i] : throw new ArgumentException("Missing option value");
            switch (args[i])
            {
                case "--runtime-root": root = Path.GetFullPath(Value()); break;
                case "--data-dir": data = Path.GetFullPath(Value()); break;
                case "--lang": language = Value(); break;
                case "--auto-start": start = true; break;
                case "--debug-port":
                    if (Environment.GetEnvironmentVariable("NOVEL_G_DESKTOP_TESTING") != "1")
                        throw new ArgumentException("Remote debugging is available only in an explicit test run");
                    debugPort = int.Parse(Value(), CultureInfo.InvariantCulture);
                    if (debugPort < 1024 || debugPort > 65535) throw new ArgumentException("Invalid debug port");
                    break;
                default: throw new ArgumentException("Unknown desktop option");
            }
        }
        if (language is not ("zh" or "en")) throw new ArgumentException("Unsupported language");
        data = Path.GetFullPath(data);
        var relative = Path.GetRelativePath(root, data);
        if (relative == "." || (!relative.StartsWith(".." + Path.DirectorySeparatorChar) && !Path.IsPathRooted(relative)))
            throw new ArgumentException("The persistent data directory must be outside the program directory");
        return new(root, data, language, start, debugPort);
    }
}
