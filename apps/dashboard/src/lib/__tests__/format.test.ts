import { describe, expect, it } from "vitest";

import {
  formatBytes,
  formatDateTime,
  formatDuration,
  formatPercent,
  formatPhone,
  formatRelative,
  humanize,
  outcomeLabel,
} from "@/lib/format";

describe("formatDuration", () => {
  it.each([
    [null, "—"],
    [undefined, "—"],
    [Number.NaN, "—"],
    [0, "0s"],
    [42.4, "42s"],
    [59.6, "1m"],
    [60, "1m"],
    [95, "1m 35s"],
    [3599, "59m 59s"],
    [3600, "1h 0m"],
    [3 * 3600 + 25 * 60 + 10, "3h 25m"],
  ])("formatDuration(%s) -> %s", (input, expected) => {
    expect(formatDuration(input as number | null | undefined)).toBe(expected);
  });
});

describe("formatPhone", () => {
  it("groups Indian E.164 numbers as 5+5", () => {
    expect(formatPhone("+919812345678")).toBe("+91 98123 45678");
  });

  it("leaves other numbers unchanged", () => {
    expect(formatPhone("+14155550123")).toBe("+14155550123");
    expect(formatPhone("+9198123")).toBe("+9198123");
  });

  it("renders a dash for empty values", () => {
    expect(formatPhone(null)).toBe("—");
    expect(formatPhone(undefined)).toBe("—");
    expect(formatPhone("")).toBe("—");
  });
});

describe("formatRelative", () => {
  const now = new Date("2026-06-15T12:00:00Z");
  const ago = (seconds: number) => new Date(now.getTime() - seconds * 1000).toISOString();

  it("handles missing and invalid input", () => {
    expect(formatRelative(null, now)).toBe("—");
    expect(formatRelative("not a date", now)).toBe("—");
  });

  it("buckets recent times", () => {
    expect(formatRelative(ago(10), now)).toBe("just now");
    expect(formatRelative(ago(5 * 60), now)).toBe("5 min ago");
    expect(formatRelative(ago(3 * 3600), now)).toBe("3 h ago");
    expect(formatRelative(ago(2 * 86_400), now)).toBe("2 d ago");
  });

  it("falls back to an absolute date after a week", () => {
    const out = formatRelative(ago(30 * 86_400), now);
    expect(out).not.toMatch(/ago/);
    expect(out).toMatch(/2026/);
  });
});

describe("formatDateTime", () => {
  it("returns a dash for missing or invalid dates", () => {
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime("garbage")).toBe("—");
  });

  it("formats a valid ISO timestamp", () => {
    expect(formatDateTime("2026-06-15T12:00:00Z")).not.toBe("—");
  });
});

describe("formatBytes / formatPercent", () => {
  it("scales bytes", () => {
    expect(formatBytes(0)).toBe("—");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(50 * 1024 * 1024)).toBe("50 MB");
  });

  it("formats ratios as percentages", () => {
    expect(formatPercent(null)).toBe("—");
    expect(formatPercent(0.256)).toBe("26%");
    expect(formatPercent(0.256, 1)).toBe("25.6%");
  });
});

describe("outcomeLabel", () => {
  it("uses the known label table", () => {
    expect(outcomeLabel("interested")).toBe("Interested");
    expect(outcomeLabel("callback_requested")).toBe("Callback requested");
    expect(outcomeLabel("opted_out")).toBe("Opted out");
  });

  it("humanizes unknown outcomes", () => {
    expect(outcomeLabel("left_voice-mail")).toBe("Left voice mail");
  });

  it("labels missing outcomes as unclassified", () => {
    expect(outcomeLabel(null)).toBe("Unclassified");
    expect(outcomeLabel("")).toBe("Unclassified");
  });

  it("humanize capitalises and strips separators", () => {
    expect(humanize("in_progress")).toBe("In progress");
  });
});
