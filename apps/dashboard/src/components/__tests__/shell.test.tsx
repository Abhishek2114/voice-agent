import { screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "@/components/shell";
import type { Me } from "@/lib/types";
import { jsonResponse, renderWithClient } from "@/test/utils";

const nav = vi.hoisted(() => ({
  pathname: "/w/ws-1/agents",
  params: { ws: "ws-1" } as Record<string, string>,
  router: { replace: vi.fn(), push: vi.fn(), refresh: vi.fn(), back: vi.fn(), forward: vi.fn(), prefetch: vi.fn() },
}));

vi.mock("next/navigation", () => ({
  useRouter: () => nav.router,
  usePathname: () => nav.pathname,
  useParams: () => nav.params,
  useSearchParams: () => new URLSearchParams(),
}));

const ME: Me = {
  user: { id: "u1", email: "owner@example.com", name: "Asha", is_instance_admin: true },
  csrf_token: "tok",
  workspaces: [{ id: "ws-1", name: "Asha Dental", slug: "asha-dental", role: "owner", brand_color: "#336699" }],
};

const EXPECTED_LINKS: [string, string][] = [
  ["Overview", "/w/ws-1"],
  ["Voice playground", "/w/ws-1/playground"],
  ["Agents", "/w/ws-1/agents"],
  ["Knowledge", "/w/ws-1/knowledge"],
  ["Contacts", "/w/ws-1/contacts"],
  ["Campaigns", "/w/ws-1/campaigns"],
  ["Calls", "/w/ws-1/calls"],
  ["Analytics", "/w/ws-1/analytics"],
  ["Compliance", "/w/ws-1/compliance"],
  ["Settings", "/w/ws-1/settings"],
];

function mainNav() {
  // The rail renders once in the desktop sidebar (the mobile sheet is closed).
  const navs = screen.getAllByRole("navigation", { name: "Main" });
  expect(navs).toHaveLength(1);
  return navs[0];
}

describe("AppShell navigation", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    nav.router.replace.mockReset();
    nav.params = { ws: "ws-1" };
    fetchMock = vi.fn(async (url: string) => {
      if (url === "/api/auth/me") return jsonResponse(ME);
      return jsonResponse({ error: { code: "not_found", message: "Not found" } }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  it("renders workspace-scoped links with the correct hrefs", async () => {
    nav.pathname = "/w/ws-1/agents";
    renderWithClient(
      <AppShell>
        <p>page body</p>
      </AppShell>,
    );
    expect(screen.getByText("page body")).toBeInTheDocument();
    const links = within(mainNav()).getAllByRole("link");
    expect(links.map((a) => [a.textContent, a.getAttribute("href")])).toEqual(EXPECTED_LINKS);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/auth/me", expect.anything()));
    expect(await screen.findByText("Asha Dental")).toBeInTheDocument();
  });

  it("marks only the current section with aria-current=page", async () => {
    nav.pathname = "/w/ws-1/campaigns/c-9";
    renderWithClient(<AppShell>body</AppShell>);
    const current = within(mainNav()).getAllByRole("link").filter((a) => a.getAttribute("aria-current") === "page");
    expect(current.map((a) => a.textContent)).toEqual(["Campaigns"]);
    await screen.findByText("Asha Dental");
  });

  it("marks Overview only on the workspace root", async () => {
    nav.pathname = "/w/ws-1";
    renderWithClient(<AppShell>body</AppShell>);
    const links = within(mainNav()).getAllByRole("link");
    expect(links.find((a) => a.textContent === "Overview")).toHaveAttribute("aria-current", "page");
    expect(links.filter((a) => a.hasAttribute("aria-current"))).toHaveLength(1);
    await screen.findByText("Asha Dental");
  });

  it("redirects to /login when the session is gone", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ error: { code: "not_authenticated", message: "Sign in" } }, 401));
    renderWithClient(<AppShell>body</AppShell>);
    await waitFor(() => expect(nav.router.replace).toHaveBeenCalledWith("/login"));
  });

  it("leaves a workspace the user does not belong to", async () => {
    nav.params = { ws: "ws-other" };
    nav.pathname = "/w/ws-other";
    renderWithClient(<AppShell>body</AppShell>);
    await waitFor(() => expect(nav.router.replace).toHaveBeenCalledWith("/"));
  });
});
