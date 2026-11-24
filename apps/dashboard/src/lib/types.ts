export type Role = "owner" | "admin" | "editor" | "viewer";
export type CallMode = "WEBRTC" | "SIMULATION" | "PSTN";

export interface User {
  id: string;
  email: string;
  name: string;
  is_instance_admin: boolean;
}

export interface WorkspaceSummary {
  id: string;
  name: string;
  slug: string;
  role: Role;
  brand_color: string;
}

export interface Me {
  user: User;
  csrf_token: string;
  workspaces: WorkspaceSummary[];
}

export interface Workspace {
  id: string;
  name: string;
  slug: string;
  description: string;
  brand_color: string;
  contact_email: string | null;
  contact_phone: string | null;
  website: string | null;
  address: string | null;
  timezone: string;
  default_country: string;
  has_logo: boolean;
  role: Role;
}

export interface Agent {
  id: string;
  name: string;
  description: string;
  personality: string;
  system_instructions: string;
  greeting: string;
  objectives: string[];
  business_knowledge: string;
  language: string;
  llm: { provider?: string; model?: string; temperature?: number; max_tokens?: number };
  stt: { language?: string; end_silence_ms?: number | null };
  tts: { provider?: string; voice?: string | null; speaker?: string | null; speed?: number };
  max_duration_seconds: number;
  silence_timeout_seconds: number;
  end_rules: { end_phrases?: string[]; max_turns?: number };
  fallback_responses: Record<string, string>;
  use_knowledge_base: boolean;
  is_active: boolean;
  public_link_enabled: boolean;
  version: number;
  tools: string[];
  created_at: string;
  updated_at: string;
  stats: { calls?: number };
}

export interface Contact {
  id: string;
  first_name: string;
  last_name: string;
  full_name: string;
  phone_raw: string | null;
  phone_e164: string | null;
  phone_valid: boolean;
  email: string | null;
  company: string | null;
  tags: string[];
  notes: string;
  status: string;
  source: string;
  dnd_status: string;
  dnd_checked_at: string | null;
  is_fictional: boolean;
  created_at: string;
  updated_at: string;
  custom_fields: Record<string, string>;
}

export interface Page<T> {
  items: T[];
  total: number;
  page?: number;
  page_size?: number;
}

export interface Call {
  id: string;
  mode: CallMode;
  direction: "inbound" | "outbound";
  channel: string;
  status: string;
  agent_id: string | null;
  agent_name: string | null;
  contact_id: string | null;
  contact_name: string | null;
  campaign_id: string | null;
  campaign_name: string | null;
  to_number: string | null;
  from_number: string | null;
  started_at: string | null;
  answered_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  outcome: string | null;
  summary: string | null;
  sentiment: string | null;
  end_reason: string | null;
  error_code: string | null;
  error_detail: string | null;
  analysis_error: string | null;
  llm_model: string | null;
  simulation_persona: string | null;
  is_simulated: boolean;
}

export interface Message {
  seq: number;
  role: "assistant" | "user" | "tool" | "system";
  content: string;
  interrupted: boolean;
  metrics: Record<string, number | string | boolean | null>;
  created_at: string;
}

export interface CallDetail extends Call {
  metrics: Record<string, unknown>;
  messages: Message[];
  tool_log: { tool: string; arguments: Record<string, unknown>; ok: boolean; message: string }[];
  effects: string[];
  recording: { duration_seconds: number; size_bytes: number } | null;
  callbacks: { id: string; kind: string; preferred_time: string; status: string; notes: string }[];
}

export interface Campaign {
  id: string;
  name: string;
  description: string;
  agent_id: string;
  agent_name: string | null;
  objective: string;
  purpose: string;
  mode: "SIMULATION" | "PSTN";
  status: string;
  status_reason: string;
  schedule_start_at: string | null;
  schedule_end_at: string | null;
  timezone: string;
  window_start: string;
  window_end: string;
  calling_days: number[];
  max_attempts: number;
  retry_delay_minutes: number;
  max_concurrent_calls: number;
  simulation: {
    answer_rate: number;
    busy_rate: number;
    fail_rate: number;
    callee_mode: "scripted" | "llm";
    persona_weights: Record<string, number>;
    ignore_calling_window: boolean;
  };
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  progress: Record<string, number>;
  window?: { start: string; end: string; days: number[]; open_now: boolean; next_open: string | null; ignored: boolean };
}

export interface ProviderHealth {
  provider: string;
  status: "ok" | "degraded" | "unavailable" | "disabled";
  detail: string;
  latency_ms: number | null;
  is_local: boolean;
  extra: Record<string, unknown>;
}

export interface SystemHealth {
  database: { status: string; latency_ms?: number; detail?: string };
  scheduler: { status: string; detail: string };
  llm: ProviderHealth;
  stt: ProviderHealth;
  tts: ProviderHealth;
  vad: ProviderHealth;
  telephony: ProviderHealth;
  voice_sessions: { active: number; max: number };
  resources: { memory_total_gb: number; memory_used_percent: number; disk_free_gb: number; cpu_percent: number };
  uptime_seconds: number;
  real_telephony_enabled: boolean;
}

export interface KnowledgeDoc {
  id: string;
  title: string;
  kind: string;
  source_filename: string | null;
  size_bytes: number;
  status: string;
  error: string;
  chunk_count: number;
  created_at: string;
  updated_at: string;
  preview: string;
  content?: string;
}

export interface ModelInfo {
  name: string;
  size_bytes: number | null;
  family: string | null;
  parameter_size: string | null;
  quantization: string | null;
  is_local: boolean;
  remote_host: string | null;
  capabilities: string[];
  fits_memory: boolean;
}
