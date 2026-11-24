import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import CampaignsPage from "@/app/w/[ws]/campaigns/page";
import { jsonResponse, renderWithClient } from "@/test/utils";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/w/ws-1/campaigns",
  useParams: () => ({ ws: "ws-1" }),
  useSearchParams: () => new URLSearchParams(),
}));

const WORKSPACE = {
  id: "ws-1",
  name: "Asha Dental",
  slug: "asha-dental",
  description: "",
  brand_color: "#336699",
  contact_email: null,
  contact_phone: null,
  website: null,
  address: null,
  timezone: "Asia/Kolkata",
  default_country: "IN",
  has_logo: false,
  role: "viewer",
};

describe("CampaignsPage data states", () => {
  let campaignsResponses: (() => Response)[];

  beforeEach(() => {
    campaignsResponses = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url === "/api/workspaces/ws-1") return jsonResponse(WORKSPACE);
        if (url === "/api/workspaces/ws-1/campaigns") {
          const next = campaignsResponses.shift();
          if (next) return next();
        }
        return jsonResponse({ error: { code: "not_found", message: "Not found" } }, 404);
      }),
    );
  });

  it("shows the API error and recovers on retry", async () => {
    campaignsResponses.push(
      () => jsonResponse({ error: { code: "internal_error", message: "Database is busy, try again" } }, 500),
      () => jsonResponse([]),
    );
    renderWithClient(<CampaignsPage />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn’t load this");
    expect(alert).toHaveTextContent("Database is busy, try again");

    await userEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(await screen.findByText("No campaigns yet")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    // Viewers cannot create campaigns.
    expect(screen.queryByRole("link", { name: /new campaign|plan a campaign/i })).not.toBeInTheDocument();
  });

  it("shows a loading state before data arrives", () => {
    campaignsResponses.push(() => jsonResponse([]));
    renderWithClient(<CampaignsPage />);
    expect(screen.getByLabelText("Loading")).toHaveAttribute("aria-busy", "true");
  });
});
