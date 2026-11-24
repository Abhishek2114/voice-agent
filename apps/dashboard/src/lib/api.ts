/**
 * Thin fetch wrapper for the voice-agent API.
 * - Same-origin requests through the Next.js /api proxy (cookies stay first-party).
 * - Sends the CSRF token (from the readable `va_csrf` cookie) on every mutation.
 * - Normalises errors into ApiError with the backend's stable `code`.
 */

export class ApiError extends Error {
  status: number;
  code: string;
  details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.split("; ").find((c) => c.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.slice(name.length + 1)) : null;
}

type Query = Record<string, string | number | boolean | null | undefined>;

export function withQuery(path: string, query?: Query): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

async function parseError(res: Response): Promise<ApiError> {
  let body: { detail?: unknown; error?: { code?: string; message?: string } & Record<string, unknown> } = {};
  try {
    body = await res.json();
  } catch {
    /* non-JSON error body */
  }
  const message =
    body.error?.message ??
    (typeof body.detail === "string" ? body.detail : null) ??
    (res.status === 0 ? "Network error" : `Request failed (${res.status})`);
  return new ApiError(res.status, body.error?.code ?? `http_${res.status}`, message, body.error ?? {});
}

export async function api<T = unknown>(
  method: string,
  path: string,
  options: { json?: unknown; form?: FormData; query?: Query; raw?: boolean; signal?: AbortSignal } = {},
): Promise<T> {
  const headers: Record<string, string> = {};
  let body: BodyInit | undefined;
  if (options.json !== undefined) {
    headers["content-type"] = "application/json";
    body = JSON.stringify(options.json);
  } else if (options.form) {
    body = options.form;
  }
  if (!["GET", "HEAD"].includes(method)) {
    const csrf = readCookie("va_csrf");
    if (csrf) headers["x-csrf-token"] = csrf;
  }
  let res: Response;
  try {
    res = await fetch(withQuery(path, options.query), {
      method,
      headers,
      body,
      credentials: "same-origin",
      signal: options.signal,
    });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError(0, "network_error", "Cannot reach the voice-agent API. Is the backend running?");
  }
  if (!res.ok) throw await parseError(res);
  if (options.raw) return res as unknown as T;
  if (res.status === 204) return undefined as T;
  const type = res.headers.get("content-type") ?? "";
  return (type.includes("application/json") ? res.json() : res.text()) as Promise<T>;
}

export const get = <T>(path: string, query?: Query) => api<T>("GET", path, { query });
export const post = <T>(path: string, json?: unknown, query?: Query) => api<T>("POST", path, { json, query });
export const put = <T>(path: string, json?: unknown) => api<T>("PUT", path, { json });
export const patch = <T>(path: string, json?: unknown) => api<T>("PATCH", path, { json });
export const del = <T>(path: string, query?: Query) => api<T>("DELETE", path, { query });
export const upload = <T>(path: string, form: FormData) => api<T>("POST", path, { form });

export async function download(path: string, filename: string, query?: Query) {
  const res = await api<Response>("GET", path, { query, raw: true });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return "Something went wrong";
}
