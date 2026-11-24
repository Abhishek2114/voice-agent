import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ErrorState, Field, ModeBadge, OutcomeBadge, Pagination } from "@/components/common";
import { ApiError } from "@/lib/api";

describe("ModeBadge", () => {
  it("marks simulations with the hatch pattern and an explanatory title", () => {
    render(<ModeBadge mode="SIMULATION" />);
    const badge = screen.getByText("Simulation");
    expect(badge).toHaveClass("sim-hatch");
    expect(badge).toHaveAttribute("title", "Simulated call: no real phone call was placed");
  });

  it("labels browser and phone calls without the hatch", () => {
    const { rerender } = render(<ModeBadge mode="WEBRTC" />);
    expect(screen.getByText("Browser")).not.toHaveClass("sim-hatch");
    expect(screen.getByText("Browser")).not.toHaveAttribute("title");
    rerender(<ModeBadge mode="PSTN" />);
    expect(screen.getByText("Phone")).toBeInTheDocument();
  });

  it("falls back to the raw mode for unknown values", () => {
    render(<ModeBadge mode="SIP" />);
    expect(screen.getByText("SIP")).toBeInTheDocument();
  });
});

describe("OutcomeBadge", () => {
  it.each([
    ["interested", "Interested"],
    ["not_interested", "Not interested"],
    ["opted_out", "Opted out"],
    ["voicemail_left", "Voicemail left"],
    [null, "Unclassified"],
  ])("renders %s as %s", (outcome, label) => {
    render(<OutcomeBadge outcome={outcome} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("uses positive and negative tones", () => {
    const { rerender } = render(<OutcomeBadge outcome="interested" />);
    expect(screen.getByText("Interested")).toHaveClass("text-ok");
    rerender(<OutcomeBadge outcome="failed" />);
    expect(screen.getByText("Failed")).toHaveClass("text-destructive");
  });
});

describe("Pagination", () => {
  it("renders nothing when everything fits on one page", () => {
    const { container } = render(<Pagination page={1} pageSize={25} total={25} onPage={() => undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("disables Previous on the first page and moves forward", async () => {
    const onPage = vi.fn();
    render(<Pagination page={1} pageSize={10} total={35} onPage={onPage} />);
    expect(screen.getByRole("navigation", { name: "Pagination" })).toHaveTextContent("1–10 of 35");
    const prev = screen.getByRole("button", { name: "Previous" });
    const next = screen.getByRole("button", { name: "Next" });
    expect(prev).toBeDisabled();
    expect(next).toBeEnabled();
    await userEvent.click(next);
    expect(onPage).toHaveBeenCalledWith(2);
  });

  it("disables Next on the last page and moves back", async () => {
    const onPage = vi.fn();
    render(<Pagination page={4} pageSize={10} total={35} onPage={onPage} />);
    expect(screen.getByRole("navigation", { name: "Pagination" })).toHaveTextContent("31–35 of 35");
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(onPage).toHaveBeenCalledWith(3);
  });

  it("enables both buttons on a middle page", () => {
    render(<Pagination page={2} pageSize={10} total={35} onPage={() => undefined} />);
    expect(screen.getByRole("button", { name: "Previous" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
  });
});

describe("ErrorState", () => {
  it("shows the error message and calls onRetry", async () => {
    const onRetry = vi.fn();
    render(<ErrorState error={new ApiError(503, "unavailable", "Database is locked")} onRetry={onRetry} />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Couldn’t load this");
    expect(alert).toHaveTextContent("Database is locked");
    await userEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("hides the retry button without a handler", () => {
    render(<ErrorState error={new Error("nope")} />);
    expect(screen.getByRole("alert")).toHaveTextContent("nope");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("Field", () => {
  it("associates the label with its control and shows a hint", () => {
    render(
      <Field label="Email" htmlFor="email" hint="We never share it">
        <input id="email" />
      </Field>,
    );
    expect(screen.getByLabelText("Email")).toHaveAttribute("id", "email");
    expect(screen.getByText("We never share it")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the error with role=alert instead of the hint", () => {
    render(
      <Field label="Email" htmlFor="email" hint="We never share it" error="Enter a valid email address">
        <input id="email" />
      </Field>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Enter a valid email address");
    expect(screen.queryByText("We never share it")).not.toBeInTheDocument();
  });
});
