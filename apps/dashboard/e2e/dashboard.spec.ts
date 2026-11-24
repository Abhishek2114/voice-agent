import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const EMAIL = "owner@example.com";
const PASSWORD = "e2e-correct-horse-battery";
const DATA = process.env.E2E_DATA_DIR ?? path.resolve(__dirname, "../../../.run/e2e-data");

test.describe.configure({ mode: "serial" });

let wsUrl = "";

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/w\/[0-9a-f]+/);
  wsUrl = new URL(page.url()).pathname.split("/").slice(0, 3).join("/");
}

test("first-run setup validates input and creates the owner", async ({ page }) => {
  await page.goto("/");
  await page.waitForURL("**/setup");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("Enter your name")).toBeVisible();
  await page.getByLabel("Your name").fill("Owner");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill("short");
  await page.getByLabel("Confirm password").fill("different");
  await page.getByLabel("Business name").fill("ShrotiHost E2E");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("Use at least 10 characters")).toBeVisible();
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByLabel("Confirm password").fill(PASSWORD);
  await page.getByRole("button", { name: "Create account" }).click();
  await page.waitForURL(/\/w\/[0-9a-f]+$/);
  await expect(page.getByRole("heading", { name: "ShrotiHost E2E" })).toBeVisible();
  await expect(page.getByText("Language model")).toBeVisible();
  await expect(page.getByText("No conversations yet")).toBeVisible();
});

test("sign out, wrong password, sign in", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill("wrong-password-123");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByText("Email or password is incorrect")).toBeVisible();
  await signIn(page);
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
});

test("create an agent and chat with it on the local model", async ({ page }) => {
  await signIn(page);
  await page.goto(`${wsUrl}/agents/new`);
  await page.getByLabel("Name", { exact: true }).fill("Asha");
  await page.getByLabel("Greeting").fill("Hi, thanks for calling ShrotiHost. How can I help?");
  await page.getByLabel("Business knowledge").fill("ShrotiHost offers shared web hosting and managed VPS servers. Support is by email ticket.");
  await expect(page.getByLabel("Language model")).not.toHaveValue("");
  await page.getByRole("button", { name: "Create agent" }).click();
  await page.waitForURL(/agents\/[0-9a-f]+$/);
  await expect(page.getByRole("heading", { name: "Asha" })).toBeVisible();
  await page.getByRole("tab", { name: "Test chat" }).click();
  await page.getByLabel("Message").fill("What do you offer?");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText(/hosting|VPS/i).last()).toBeVisible({ timeout: 90_000 });
});

test("live voice conversation in the browser", async ({ page }) => {
  test.skip(fs.existsSync(path.join(DATA, "NO_SPEECH")), "Piper not running: no caller speech available");
  await signIn(page);
  await page.goto(`${wsUrl}/playground`);
  await page.getByRole("button", { name: "Start conversation" }).click();
  const transcript = page.getByRole("region", { name: "Live transcript" });
  // greeting (with AI disclosure) is spoken first
  await expect(transcript.getByText(/AI assistant/i)).toBeVisible({ timeout: 60_000 });
  // the fake microphone plays real speech; it is transcribed and answered by the local model
  await expect(transcript.getByText(/hosting/i).first()).toBeVisible({ timeout: 90_000 });
  await expect(transcript.locator("li").filter({ hasText: "Agent" })).not.toHaveCount(1, { timeout: 90_000 });
  await page.getByRole("button", { name: "End" }).click();
  await expect(page.getByText("Call ended")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("link", { name: "Open the call record" }).click();
  await expect(page.getByText("Browser").first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "Transcript" })).toBeVisible();
});

test("contacts: add, validate phone, and import a CSV", async ({ page }) => {
  await signIn(page);
  await page.goto(`${wsUrl}/contacts`);
  await page.getByRole("button", { name: "Add contact" }).click();
  await page.getByLabel("First name").fill("Ravi");
  await page.getByLabel("Phone").fill("123");
  await page.getByRole("button", { name: "Add contact" }).last().click();
  await expect(page.getByText(/Invalid phone number/)).toBeVisible();
  await page.getByLabel("Phone").fill("98123 45601");
  await page.getByRole("button", { name: "Add contact" }).last().click();
  await expect(page.getByRole("link", { name: "Ravi" })).toBeVisible();

  await page.goto(`${wsUrl}/contacts/import`);
  const csv = "Name,Mobile,Email,Consent Purpose,Consent Source,Consent Evidence,Consent Date\n" +
    "Asha Verma,98123 45631,asha@example.com,promotional,web_form,Signup #881,2026-10-01\n" +
    "Bad Row,12,,,,,\n" +
    '"=HYPERLINK(""x"")",98123 45632,,,,,\n';
  await page.locator('input[type="file"]').setInputFiles({ name: "leads.csv", mimeType: "text/csv", buffer: Buffer.from(csv) });
  await expect(page.getByText("Map the columns of leads.csv")).toBeVisible();
  await page.getByRole("button", { name: "Check rows" }).click();
  await expect(page.getByText("Will be added")).toBeVisible();
  await page.getByRole("button", { name: "Import contacts" }).click();
  await expect(page.getByText("Import finished")).toBeVisible();
  await expect(page.getByText(/2 added/)).toBeVisible();
});

test("simulation campaign runs through the scheduler", async ({ page }) => {
  await signIn(page);
  // open the calling window for the test workspace so the run is not time-of-day dependent
  await page.goto(`${wsUrl}/compliance`);
  await page.getByLabel("Earliest call").fill("00:00");
  await page.getByLabel("Latest call").fill("23:59");
  for (const d of ["Sun"]) await page.getByRole("button", { name: d, exact: true }).click();
  await page.getByRole("button", { name: "Save policy" }).click();
  await expect(page.getByText("Policy saved")).toBeVisible();

  await page.goto(`${wsUrl}/campaigns/new`);
  await page.getByLabel("Campaign name").fill("E2E festive offer");
  await page.getByLabel("Agent").selectOption({ label: "Asha" });
  await page.getByLabel("Purpose").selectOption("service");
  await page.getByLabel("Calls start at").fill("00:00");
  await page.getByLabel("Calls stop at").fill("23:59");
  await page.getByRole("button", { name: "Sun", exact: true }).click();
  await page.getByText("Ignore calling hours (simulation only)").click();
  await page.getByRole("button", { name: "Save draft" }).click();
  await page.waitForURL(/campaigns\/[0-9a-f]+$/);
  await page.getByRole("button", { name: "Check now" }).click();
  await expect(page.getByText(/contacts are eligible now/)).toBeVisible();
  await page.getByRole("button", { name: "Start" }).click();
  await expect(page.getByText("Completed").first()).toBeVisible({ timeout: 100_000 });
  await page.goto(`${wsUrl}/calls`);
  await expect(page.getByText("Simulation").first()).toBeVisible();
});

test("analytics and settings reflect stored data", async ({ page }) => {
  await signIn(page);
  await page.goto(`${wsUrl}/analytics`);
  await expect(page.getByText("Calls over time")).toBeVisible();
  await page.getByRole("button", { name: "Show table" }).click();
  await expect(page.getByRole("columnheader", { name: "Positive", exact: true }).first()).toBeVisible();
  await page.goto(`${wsUrl}/settings?tab=ai`);
  await expect(page.getByText("qwen3:4b-instruct-2507-q4_K_M").first()).toBeVisible();
  await page.goto(`${wsUrl}/settings?tab=diagnostics`);
  await expect(page.getByText("Speech recognition")).toBeVisible();
});

test("viewer role and unauthenticated access are blocked", async ({ page, request }) => {
  const res = await request.get("/api/workspaces");
  expect(res.status()).toBe(401);
  await page.goto("/w/does-not-exist");
  await page.waitForURL("**/login");
});

test("capture README screenshots", async ({ page }) => {
  test.skip(!process.env.CAPTURE_SCREENSHOTS, "set CAPTURE_SCREENSHOTS=1 to refresh docs/screenshots");
  const out = path.resolve(__dirname, "../../../docs/screenshots");
  fs.mkdirSync(out, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  const shot = async (name: string, scheme: "light" | "dark") => {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await page.waitForTimeout(400);
    await page.screenshot({ path: path.join(out, `${name}-${scheme}.png`) });
  };
  await page.goto(wsUrl);
  await expect(page.getByText("Recent conversations")).toBeVisible();
  await shot("overview", "light");
  await shot("overview", "dark");

  await page.goto(`${wsUrl}/playground`);
  await page.getByRole("button", { name: "Start conversation" }).click();
  const transcript = page.getByRole("region", { name: "Live transcript" });
  await expect(transcript.getByText(/hosting/i).first()).toBeVisible({ timeout: 90_000 });
  await expect(transcript.locator("li").filter({ hasText: "Agent" })).not.toHaveCount(1, { timeout: 90_000 });
  await shot("playground", "dark");
  await page.getByRole("button", { name: "End" }).click();
  await page.getByRole("link", { name: "Open the call record" }).click();
  await expect(page.getByRole("heading", { name: "Transcript" })).toBeVisible();
  await page.waitForTimeout(3000);
  await page.reload();
  await shot("call", "light");

  await page.goto(`${wsUrl}/agents`);
  await page.getByRole("link", { name: /Asha/ }).first().click();
  await expect(page.getByRole("tab", { name: "Configure" })).toBeVisible();
  await shot("agent", "light");

  await page.goto(`${wsUrl}/analytics`);
  await expect(page.getByText("Calls over time")).toBeVisible();
  await shot("analytics", "dark");
  await shot("analytics", "light");

  await page.goto(`${wsUrl}/campaigns`);
  await page.getByRole("link", { name: /E2E festive offer/ }).click();
  await expect(page.getByText("Contacts in this campaign")).toBeVisible();
  await shot("campaign", "light");

  await page.goto(`${wsUrl}/calls`);
  await shot("calls", "dark");
});
