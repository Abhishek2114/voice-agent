import { z } from "zod";

export const loginSchema = z.object({
  email: z.string().trim().email("Enter a valid email address"),
  password: z.string().min(1, "Enter your password"),
});
export type LoginValues = z.infer<typeof loginSchema>;

export const setupSchema = z
  .object({
    name: z.string().trim().min(1, "Enter your name").max(120),
    email: z.string().trim().email("Enter a valid email address"),
    password: z.string().min(10, "Use at least 10 characters").max(256),
    confirm: z.string(),
    workspace_name: z.string().trim().min(1, "Enter a business name").max(120),
  })
  .refine((v) => v.password === v.confirm, { message: "Passwords don’t match", path: ["confirm"] });
export type SetupValues = z.infer<typeof setupSchema>;

const hhmm = z.string().regex(/^([01]\d|2[0-3]):[0-5]\d$/, "Use 24-hour HH:MM");

export const TOOL_NAMES = [
  "end_call",
  "request_callback",
  "request_appointment",
  "record_opt_out",
  "search_knowledge",
  "record_interest",
  "capture_lead",
] as const;

export const agentSchema = z.object({
  name: z.string().trim().min(1, "Give the agent a name").max(120),
  description: z.string().max(2000),
  personality: z.string().max(2000),
  system_instructions: z.string().max(20_000),
  greeting: z.string().max(1000),
  objectives: z.array(z.string().max(300)).max(20),
  business_knowledge: z.string().max(20_000),
  language: z.string().regex(/^([a-z]{2}(-[A-Z]{2})?|auto)$/, "Use a code like en or en-IN"),
  llm: z.object({
    provider: z.literal("ollama"),
    model: z.string().min(1, "Choose a model"),
    temperature: z.number().min(0).max(1.5),
    max_tokens: z.number().int().min(16).max(1024),
  }),
  stt: z.object({ language: z.string().regex(/^[a-z]{2}$/), end_silence_ms: z.number().int().min(300).max(3000).nullable() }),
  tts: z.object({
    provider: z.enum(["piper", "macos_say"]),
    voice: z.string().max(120).nullable(),
    speaker: z.string().max(60).nullable(),
    speed: z.number().min(0.5).max(2),
  }),
  max_duration_seconds: z.number().int().min(30, "At least 30 seconds").max(3600, "At most 1 hour"),
  silence_timeout_seconds: z.number().int().min(5).max(120),
  end_rules: z.object({ end_phrases: z.array(z.string().max(60)).max(30), max_turns: z.number().int().min(2).max(200) }),
  tools: z.array(z.enum(TOOL_NAMES)),
  fallback_responses: z.record(z.string(), z.string().max(500)),
  use_knowledge_base: z.boolean(),
  is_active: z.boolean(),
  version_note: z.string().max(300),
});
export type AgentValues = z.infer<typeof agentSchema>;

export const contactSchema = z.object({
  first_name: z.string().trim().max(120),
  last_name: z.string().trim().max(120),
  phone: z.string().trim().max(40),
  email: z.union([z.literal(""), z.string().trim().email("Enter a valid email")]),
  company: z.string().trim().max(160),
  tags: z.string().max(600),
  notes: z.string().max(5000),
  status: z.string(),
}).refine((v) => v.first_name || v.last_name || v.phone || v.email, {
  message: "Add at least a name, phone or email",
  path: ["first_name"],
});
export type ContactValues = z.infer<typeof contactSchema>;

export const campaignSchema = z
  .object({
    name: z.string().trim().min(1, "Name the campaign").max(160),
    description: z.string().max(4000),
    agent_id: z.string().min(1, "Choose an agent"),
    objective: z.string().max(2000),
    purpose: z.enum(["promotional", "service", "transactional", "inquiry_followup"]),
    window_start: hhmm,
    window_end: hhmm,
    calling_days: z.array(z.number().int().min(0).max(6)).min(1, "Pick at least one day"),
    max_attempts: z.number().int().min(1).max(10),
    retry_delay_minutes: z.number().int().min(1).max(10_080),
    max_concurrent_calls: z.number().int().min(1).max(20),
    schedule_start_at: z.string(),
    contact_tag: z.string(),
    contact_status: z.string(),
    callee_mode: z.enum(["scripted", "llm"]),
    answer_rate: z.number().min(0).max(1),
    ignore_calling_window: z.boolean(),
  })
  .refine((v) => v.window_start < v.window_end, { message: "Start must be before end", path: ["window_end"] });
export type CampaignValues = z.infer<typeof campaignSchema>;
