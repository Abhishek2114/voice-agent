import { describe, expect, it } from "vitest";
import type { z } from "zod";

import {
  agentSchema,
  campaignSchema,
  contactSchema,
  loginSchema,
  setupSchema,
  type AgentValues,
  type CampaignValues,
  type ContactValues,
} from "@/lib/schemas";

function issues(result: z.ZodSafeParseResult<unknown>): Record<string, string> {
  if (result.success) return {};
  return Object.fromEntries(result.error.issues.map((i) => [i.path.join("."), i.message]));
}

describe("loginSchema", () => {
  it("accepts a valid login and trims the email", () => {
    const r = loginSchema.safeParse({ email: "  owner@example.com ", password: "x" });
    expect(r.success).toBe(true);
    expect(r.data?.email).toBe("owner@example.com");
  });

  it("rejects an empty form with friendly messages", () => {
    expect(issues(loginSchema.safeParse({ email: "", password: "" }))).toEqual({
      email: "Enter a valid email address",
      password: "Enter your password",
    });
  });
});

describe("setupSchema", () => {
  const valid = {
    name: "Asha",
    email: "asha@example.com",
    password: "correct-horse",
    confirm: "correct-horse",
    workspace_name: "Asha Dental",
  };

  it("accepts a valid setup", () => {
    expect(setupSchema.safeParse(valid).success).toBe(true);
  });

  it("flags mismatched passwords on the confirm field", () => {
    expect(issues(setupSchema.safeParse({ ...valid, confirm: "different-pass" }))).toEqual({
      confirm: "Passwords don’t match",
    });
  });

  it("requires a 10-character password and the other fields", () => {
    const r = issues(setupSchema.safeParse({ name: " ", email: "x", password: "short", confirm: "short", workspace_name: "" }));
    expect(r.password).toBe("Use at least 10 characters");
    expect(r.name).toBe("Enter your name");
    expect(r.email).toBe("Enter a valid email address");
    expect(r.workspace_name).toBe("Enter a business name");
  });
});

const baseAgent: AgentValues = {
  name: "Receptionist",
  description: "",
  personality: "",
  system_instructions: "",
  greeting: "Hello",
  objectives: [],
  business_knowledge: "",
  language: "en-IN",
  llm: { provider: "ollama", model: "llama3.2", temperature: 0.4, max_tokens: 200 },
  stt: { language: "en", end_silence_ms: null },
  tts: { provider: "piper", voice: null, speaker: null, speed: 1 },
  max_duration_seconds: 300,
  silence_timeout_seconds: 15,
  end_rules: { end_phrases: [], max_turns: 40 },
  tools: ["end_call"],
  fallback_responses: {},
  use_knowledge_base: true,
  is_active: true,
  version_note: "",
};

describe("agentSchema", () => {
  it("accepts a valid agent", () => {
    expect(agentSchema.safeParse(baseAgent).success).toBe(true);
  });

  it("enforces max duration bounds", () => {
    expect(issues(agentSchema.safeParse({ ...baseAgent, max_duration_seconds: 29 }))).toEqual({
      max_duration_seconds: "At least 30 seconds",
    });
    expect(issues(agentSchema.safeParse({ ...baseAgent, max_duration_seconds: 3601 }))).toEqual({
      max_duration_seconds: "At most 1 hour",
    });
    expect(agentSchema.safeParse({ ...baseAgent, max_duration_seconds: 30 }).success).toBe(true);
    expect(agentSchema.safeParse({ ...baseAgent, max_duration_seconds: 3600 }).success).toBe(true);
  });

  it("enforces LLM, TTS and silence bounds", () => {
    const bad = issues(
      agentSchema.safeParse({
        ...baseAgent,
        llm: { ...baseAgent.llm, temperature: 1.6, max_tokens: 8 },
        tts: { ...baseAgent.tts, speed: 2.5 },
        silence_timeout_seconds: 4,
        stt: { language: "en", end_silence_ms: 100 },
      }),
    );
    expect(Object.keys(bad).sort()).toEqual(
      ["llm.max_tokens", "llm.temperature", "silence_timeout_seconds", "stt.end_silence_ms", "tts.speed"].sort(),
    );
  });

  it("validates the language code, name and tool names", () => {
    const bad = issues(agentSchema.safeParse({ ...baseAgent, language: "english", name: "  " }));
    expect(bad.language).toBe("Use a code like en or en-IN");
    expect(bad.name).toBe("Give the agent a name");
    expect(agentSchema.safeParse({ ...baseAgent, language: "auto" }).success).toBe(true);
    expect(agentSchema.safeParse({ ...baseAgent, tools: ["rm_rf"] }).success).toBe(false);
  });
});

const baseContact: ContactValues = {
  first_name: "",
  last_name: "",
  phone: "",
  email: "",
  company: "",
  tags: "",
  notes: "",
  status: "active",
};

describe("contactSchema", () => {
  it("requires at least a name, phone or email", () => {
    expect(issues(contactSchema.safeParse(baseContact))).toEqual({
      first_name: "Add at least a name, phone or email",
    });
  });

  it.each([{ first_name: "Ravi" }, { last_name: "Kumar" }, { phone: "+919812345678" }, { email: "r@example.com" }])(
    "accepts a contact with %o",
    (patch) => {
      expect(contactSchema.safeParse({ ...baseContact, ...patch }).success).toBe(true);
    },
  );

  it("allows an empty email but rejects a malformed one", () => {
    expect(issues(contactSchema.safeParse({ ...baseContact, first_name: "R", email: "nope" }))).toEqual({
      email: "Enter a valid email",
    });
  });
});

const baseCampaign: CampaignValues = {
  name: "Spring follow-up",
  description: "",
  agent_id: "agent-1",
  objective: "",
  purpose: "service",
  window_start: "09:00",
  window_end: "18:00",
  calling_days: [0, 1, 2, 3, 4],
  max_attempts: 3,
  retry_delay_minutes: 60,
  max_concurrent_calls: 2,
  schedule_start_at: "",
  contact_tag: "",
  contact_status: "",
  callee_mode: "scripted",
  answer_rate: 0.7,
  ignore_calling_window: false,
};

describe("campaignSchema", () => {
  it("accepts a valid campaign", () => {
    expect(campaignSchema.safeParse(baseCampaign).success).toBe(true);
  });

  it("requires the window start to be before the end", () => {
    expect(issues(campaignSchema.safeParse({ ...baseCampaign, window_start: "18:00", window_end: "09:00" }))).toEqual({
      window_end: "Start must be before end",
    });
    expect(campaignSchema.safeParse({ ...baseCampaign, window_start: "10:00", window_end: "10:00" }).success).toBe(false);
  });

  it("rejects malformed times and empty calling days", () => {
    const bad = issues(campaignSchema.safeParse({ ...baseCampaign, window_start: "9:00", calling_days: [] }));
    expect(bad.window_start).toBe("Use 24-hour HH:MM");
    expect(bad.calling_days).toBe("Pick at least one day");
  });

  it("requires a name and an agent", () => {
    const bad = issues(campaignSchema.safeParse({ ...baseCampaign, name: "", agent_id: "" }));
    expect(bad.name).toBe("Name the campaign");
    expect(bad.agent_id).toBe("Choose an agent");
  });
});
