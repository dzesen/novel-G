const CONFIGURED_API_BASE = process.env.NEXT_PUBLIC_API_BASE?.replace(/\/+$/, "");

declare global {
  interface Window {
    __NOVEL_G_DESKTOP__?: Readonly<{ apiBase: string }>;
  }
}

export function getDesktopApiBase(): string | null {
  if (typeof window === "undefined" || window.location.protocol !== "http:" || window.location.hostname !== "127.0.0.1") return null;
  const value = window.__NOVEL_G_DESKTOP__?.apiBase;
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    const port = Number(url.port);
    if (url.protocol === "http:" && url.hostname === "127.0.0.1" && port >= 1024 && port <= 65535 && !url.username && !url.password && url.pathname === "/" && !url.search && !url.hash) return url.origin;
  } catch { /* An invalid host hint must not change API routing. */ }
  return null;
}

interface ApiAuthHooks {
  getCsrfToken: () => string | null;
  onUnauthorized: () => void;
}

let authHooks: ApiAuthHooks = {
  getCsrfToken: () => null,
  onUnauthorized: () => {},
};

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(message: string, status: number, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export function configureApiAuth(hooks: ApiAuthHooks): void {
  authHooks = hooks;
}

export function getApiBase(): string {
  const desktopBase = getDesktopApiBase();
  if (desktopBase) return desktopBase;
  if (CONFIGURED_API_BASE) return CONFIGURED_API_BASE;
  if (typeof window !== "undefined") {
    return `${window.location.protocol}//${window.location.hostname}:8000`;
  }
  return "http://localhost:8000";
}

function isUnsafeMethod(method: string): boolean {
  return !["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase());
}

async function authorizedFetch(path: string, init: RequestInit): Promise<Response> {
  const method = (init.method || "GET").toUpperCase();
  const headers = {
    ...(init.headers as Record<string, string> | undefined),
  };
  if (isUnsafeMethod(method)) {
    const csrfToken = authHooks.getCsrfToken();
    if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
  }

  const response = await fetch(`${getApiBase()}${path}`, {
    ...init,
    method,
    headers,
    credentials: "include",
  });
  if (response.status === 401) {
    authHooks.onUnauthorized();
  }
  return response;
}

async function responseError(response: Response): Promise<ApiError> {
  const body = await response.json().catch(() => null);
  const detail = body?.detail;
  let message = `Request failed: ${response.status}`;
  if (typeof detail === "string" && detail.trim()) {
    message = detail;
  } else if (Array.isArray(detail)) {
    const validationMessages = detail
      .map((item) => {
        if (!item || typeof item !== "object") return "";
        const record = item as { loc?: unknown; msg?: unknown };
        const location = Array.isArray(record.loc)
          ? record.loc
              .filter((part) => part !== "body")
              .reduce<string>((path, part) => {
                if (typeof part === "number") return `${path}[${part}]`;
                return path ? `${path}.${String(part)}` : String(part);
              }, "")
          : "";
        const issue = typeof record.msg === "string" ? record.msg : "";
        if (!location) return issue;
        return issue ? `${location}: ${issue}` : location;
      })
      .filter(Boolean);
    if (validationMessages.length) message = validationMessages.join("；");
  } else if (detail && typeof detail === "object") {
    const structuredMessage = (detail as { message?: unknown }).message;
    if (typeof structuredMessage === "string" && structuredMessage.trim()) {
      message = structuredMessage;
    }
  }
  return new ApiError(message, response.status, detail);
}

export async function apiRequest<T = unknown>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await authorizedFetch(path, init);
  if (!response.ok) throw await responseError(response);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function getImageUrl(url: string | null | undefined): string {
  if (!url) return "";
  if (url.startsWith("http") || url.startsWith("data:")) return url;
  return `${getApiBase()}${url}`;
}

export async function apiGet<T = unknown>(
  path: string,
  options: Pick<RequestInit, "signal"> = {},
): Promise<T> {
  return apiRequest<T>(path, {
    method: "GET",
    headers: { "Content-Type": "application/json" },
    // 批量生成会在服务端改写章节内容，客户端若命中浏览器缓存会读到旧副本
    // （检查点复核里"点击跳转查看"会显示空章）——API 数据始终要最新。
    cache: "no-store",
    signal: options.signal,
  });
}

export async function apiPut<T = unknown>(
  path: string,
  data: unknown
): Promise<T> {
  return apiRequest<T>(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function apiPatch<T = unknown>(
  path: string,
  data: unknown
): Promise<T> {
  return apiRequest<T>(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function apiPost<T = unknown>(
  path: string,
  data: unknown,
  options: Pick<RequestInit, "signal"> = {},
): Promise<T> {
  return apiRequest<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
    signal: options.signal,
  });
}

export async function apiPostForm<T = unknown>(
  path: string,
  formData: FormData
): Promise<T> {
  const res = await authorizedFetch(path, {
    method: "POST",
    body: formData,
  });
  if (!res.ok) {
    throw await responseError(res);
  }
  return res.json();
}

export async function apiPostRaw<T = unknown>(
  path: string,
  body: Blob,
  contentType: "application/json" | "image/png" | "image/jpeg" | "image/webp",
): Promise<T> {
  const res = await authorizedFetch(path, {
    method: "POST",
    headers: { "Content-Type": contentType },
    body,
  });
  if (!res.ok) {
    throw await responseError(res);
  }
  return res.json();
}

export async function apiDelete<T = unknown>(path: string): Promise<T> {
  return apiRequest<T>(path, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
  });
}

export async function apiDownload(path: string, fallbackFilename = "download"): Promise<void> {
  const res = await authorizedFetch(path, { method: "GET" });
  if (!res.ok) {
    throw await responseError(res);
  }

  const disposition = res.headers.get("Content-Disposition") || "";
  const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const simpleName = disposition.match(/filename="([^"]+)"/i)?.[1];
  let filename = fallbackFilename;
  try {
    filename = encodedName ? decodeURIComponent(encodedName) : simpleName || fallbackFilename;
  } catch {
    filename = simpleName || fallbackFilename;
  }

  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export type SSEErrorCode = "interrupted" | "invalidEvent";

export class SSEError extends Error {
  readonly code: SSEErrorCode;

  constructor(code: SSEErrorCode, cause?: unknown) {
    super(code === "interrupted"
      ? "The stream ended before a complete result arrived."
      : "The stream returned an invalid event.", { cause });
    this.name = "SSEError";
    this.code = code;
  }
}

export interface SSECompletionContract {
  terminalEvent: string;
  validateTerminal: (data: Record<string, unknown>) => boolean;
}

/** Each business stream declares which event proves that its result arrived. */
export async function apiPostSSE(
  path: string,
  data: unknown,
  onEvent: (event: string, data: Record<string, unknown>) => void,
  options: SSECompletionContract & { signal?: AbortSignal; idleTimeoutMs?: number },
): Promise<void> {
  const { idleTimeoutMs, signal } = options;
  if (idleTimeoutMs === undefined) return consumePostSSE(path, data, onEvent, options);
  if (!Number.isFinite(idleTimeoutMs) || idleTimeoutMs <= 0) {
    throw new RangeError("SSE idle timeout must be positive");
  }
  const idle = new AbortController();
  const combinedSignal = signal ? AbortSignal.any([signal, idle.signal]) : idle.signal;
  let timer: ReturnType<typeof setTimeout>;
  const renew = () => {
    clearTimeout(timer);
    timer = setTimeout(() => idle.abort(new SSEError("interrupted")), idleTimeoutMs);
  };
  renew();
  try {
    await consumePostSSE(path, data, onEvent, { ...options, signal: combinedSignal }, renew);
  } finally {
    clearTimeout(timer!);
  }
}

async function consumePostSSE(
  path: string,
  data: unknown,
  onEvent: (event: string, data: Record<string, unknown>) => void,
  options: SSECompletionContract & { signal?: AbortSignal },
  onActivity: () => void = () => {},
): Promise<void> {
  const { signal, terminalEvent, validateTerminal } = options;
  signal?.throwIfAborted();
  const res = await authorizedFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(data),
    signal,
  });
  if (!res.ok) {
    throw await responseError(res);
  }
  onActivity();
  const reader = res.body?.getReader();
  if (!reader) throw new SSEError("interrupted");

  const decoder = new TextDecoder();
  let buffer = "";
  let eventType = "message";
  let dataLines: string[] = [];

  const acceptLine = (line: string): boolean => {
    if (line === "") {
      const type = eventType || "message";
      const parts = dataLines;
      eventType = "message";
      dataLines = [];
      if (parts.length === 0) return false;
      let value: unknown;
      try {
        value = JSON.parse(parts.join("\n"));
      } catch (cause) {
        throw new SSEError("invalidEvent", cause);
      }
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new SSEError("invalidEvent");
      }
      const payload = value as Record<string, unknown>;
      const terminal = type === terminalEvent;
      if (terminal && !validateTerminal(payload)) throw new SSEError("invalidEvent");
      signal?.throwIfAborted();
      // Consumer failures must propagate; they are not JSON decoding failures.
      onEvent(type, payload);
      return terminal;
    }
    if (line.startsWith(":")) return false;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    let value = separator < 0 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") eventType = value;
    if (field === "data") dataLines.push(value);
    return false;
  };

  const abortReader = () => { void reader.cancel(signal?.reason).catch(() => {}); };
  signal?.addEventListener("abort", abortReader, { once: true });
  try {
    while (true) {
      signal?.throwIfAborted();
      const { done, value } = await reader.read().catch((cause) => {
        signal?.throwIfAborted();
        throw new SSEError("interrupted", cause);
      });
      signal?.throwIfAborted();
      if (value?.byteLength) onActivity();
      buffer += decoder.decode(value, { stream: !done });
      while (true) {
        const boundary = buffer.search(/[\r\n]/);
        if (boundary < 0) break;
        // A CR at the end of a chunk may be the first half of a CRLF.
        if (buffer[boundary] === "\r" && boundary === buffer.length - 1 && !done) break;
        const width = buffer.slice(boundary, boundary + 2) === "\r\n" ? 2 : 1;
        const line = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + width);
        signal?.throwIfAborted();
        if (acceptLine(line)) return;
      }
      // SSE dispatch requires a blank line. A partial last frame is not a result.
      if (done) throw new SSEError("interrupted");
    }
  } finally {
    signal?.removeEventListener("abort", abortReader);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
