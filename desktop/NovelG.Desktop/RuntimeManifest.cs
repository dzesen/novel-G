using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace NovelG.Desktop;

public sealed record RuntimeManifest
{
    [JsonPropertyName("schema_version")] public int SchemaVersion { get; init; }
    [JsonPropertyName("version")] public string Version { get; init; } = "";
    [JsonPropertyName("backend")] public string Backend { get; init; } = "";
    [JsonPropertyName("node")] public string Node { get; init; } = "";
    [JsonPropertyName("frontend")] public string Frontend { get; init; } = "";
    [JsonPropertyName("mongodb")] public string MongoDb { get; init; } = "";
    [JsonPropertyName("webview2")] public string WebView2 { get; init; } = "";
    public static RuntimeManifest Load(string root)
    {
        var manifest = JsonSerializer.Deserialize<RuntimeManifest>(File.ReadAllText(Path.Combine(root, "desktop-runtime.json")))
            ?? throw new InvalidDataException("Missing runtime manifest");
        if (manifest.SchemaVersion != 1) throw new InvalidDataException("Unsupported runtime manifest");
        foreach (var path in new[] { manifest.Backend, manifest.Node, manifest.Frontend, manifest.MongoDb })
            if (!File.Exists(Resolve(root, path))) throw new FileNotFoundException("Desktop runtime is incomplete; repair the installation");
        if (!File.Exists(Path.Combine(Resolve(root, manifest.WebView2), "msedgewebview2.exe")))
            throw new FileNotFoundException("WebView2 runtime is missing; repair the installation");
        return manifest;
    }
    public static string Resolve(string root, string relative)
    {
        if (string.IsNullOrWhiteSpace(relative) || Path.IsPathRooted(relative)) throw new InvalidDataException("Invalid runtime path");
        string full = Path.GetFullPath(Path.Combine(root, relative));
        string inside = Path.GetRelativePath(Path.GetFullPath(root), full);
        if (inside == ".." || inside.StartsWith(".." + Path.DirectorySeparatorChar) || Path.IsPathRooted(inside))
            throw new InvalidDataException("Runtime path escapes installation");
        return full;
    }
}
