import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, errorMessage, get, post, readCookie, withQuery } from "@/lib/api";
import { jsonResponse } from "@/test/utils";

describe("withQuery", () => {
  it("returns the path unchanged without a query", () => {
    expect(withQuery("/api/x")).toBe("/api/x");
    expect(withQuery("/api/x", {})).toBe("/api/x");
  });

  it("drops null, undefined and empty-string values", () => {
    expect(withQuery("/api/x", { a: 1, b: null, c: undefined, d: "", e: false, f: "y z" })).toBe(
      "/api/x?a=1&e=false&f=y+z",
    );
  });
});

describe("readCookie", () => {
  it("reads and decodes a cookie by name", () => {
    document.cookie = "other=1";
    document.cookie = "va_csrf=tok%2Fen";
    expect(readCookie("va_csrf")).toBe("tok/en");
    expect(readCookie("missing")).toBeNull();
  });
});

describe("api()", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const lastInit = () => fetchMock.mock.calls.at(-1)?.[1] as RequestInit & { headers: Record<string, string> };

  it("parses JSON responses and appends the query string", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));
    await expect(get("/api/things", { page: 2, q: "" })).resolves.toEqual({ ok: true });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/things?page=2");
    expect(lastInit().method).toBe("GET");
    expect(lastInit().credentials).toBe("same-origin");
  });

  it("returns text for non-JSON bodies and undefined for 204", async () => {
    fetchMock.mockResolvedValueOnce(new Response("hello", { headers: { "content-type": "text/plain" } }));
    await expect(get("/api/text")).resolves.toBe("hello");
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await expect(api("DELETE", "/api/x")).resolves.toBeUndefined();
  });

  it("sends the CSRF token from va_csrf on POST with a JSON body", async () => {
    document.cookie = "va_csrf=abc123";
    fetchMock.mockResolvedValue(jsonResponse({ id: 1 }, 201));
    await post("/api/items", { name: "x" });
    const init = lastInit();
    expect(init.method).toBe("POST");
    expect(init.headers["x-csrf-token"]).toBe("abc123");
    expect(init.headers["content-type"]).toBe("application/json");
    expect(init.body).toBe(JSON.stringify({ name: "x" }));
  });

  it("does not send the CSRF token on GET", async () => {
    document.cookie = "va_csrf=abc123";
    fetchMock.mockResolvedValue(jsonResponse([]));
    await get("/api/items");
    expect(lastInit().headers).not.toHaveProperty("x-csrf-token");
  });

  it("omits the CSRF header on POST when the cookie is absent", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));
    await post("/api/items", {});
    expect(lastInit().headers).not.toHaveProperty("x-csrf-token");
  });

  it("maps a 401 envelope to ApiError with code and message", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ error: { code: "not_authenticated", message: "Sign in to continue" } }, 401),
    );
    const err = await get("/api/auth/me").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 401, code: "not_authenticated", message: "Sign in to continue" });
  });

  it("maps a 422 validation envelope and keeps details", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        { error: { code: "validation_error", message: "Invalid input", fields: { email: "bad" } } },
        422,
      ),
    );
    const err = (await post("/api/x", {}).catch((e: unknown) => e)) as ApiError;
    expect(err.status).toBe(422);
    expect(err.code).toBe("validation_error");
    expect(err.message).toBe("Invalid input");
    expect(err.details).toMatchObject({ fields: { email: "bad" } });
  });

  it("uses FastAPI's string detail when there is no envelope", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "Not Found" }, 404));
    await expect(get("/api/missing")).rejects.toMatchObject({ status: 404, code: "http_404", message: "Not Found" });
  });

  it("falls back to a generic message for non-JSON error bodies", async () => {
    fetchMock.mockResolvedValue(new Response("<html>oops</html>", { status: 502 }));
    await expect(get("/api/x")).rejects.toMatchObject({ status: 502, code: "http_502", message: "Request failed (502)" });
  });

  it("turns a network failure into a network_error ApiError", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const err = (await get("/api/x").catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(0);
    expect(err.code).toBe("network_error");
    expect(err.message).toMatch(/Cannot reach the voice-agent API/);
  });

  it("rethrows aborts untouched", async () => {
    const abort = new DOMException("aborted", "AbortError");
    fetchMock.mockRejectedValue(abort);
    await expect(get("/api/x")).rejects.toBe(abort);
  });
});

describe("errorMessage", () => {
  it("extracts messages from errors and falls back for unknown values", () => {
    expect(errorMessage(new ApiError(500, "x", "Boom"))).toBe("Boom");
    expect(errorMessage(new Error("plain"))).toBe("plain");
    expect(errorMessage("nope")).toBe("Something went wrong");
  });
});
