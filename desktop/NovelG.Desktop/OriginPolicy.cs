namespace NovelG.Desktop;

public static class OriginPolicy
{
    public static bool IsLoopbackHttp(Uri uri) => uri.Scheme == "http" && uri.Host == "127.0.0.1"
        && string.IsNullOrEmpty(uri.UserInfo) && uri.Port >= 1024 && uri.Port <= 65535;
    public static bool IsWorkspace(Uri uri, Uri origin) => IsLoopbackHttp(uri)
        && uri.Scheme == origin.Scheme && uri.Host == origin.Host && uri.Port == origin.Port;
    public static bool CanOpenExternal(Uri uri) => (uri.Scheme is "http" or "https")
        && !string.IsNullOrEmpty(uri.Host) && string.IsNullOrEmpty(uri.UserInfo);
}
