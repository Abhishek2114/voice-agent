"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery } from "@tanstack/react-query";
import { Plus, Volume2, X } from "lucide-react";
import { useState } from "react";
import { useForm, type UseFormReturn } from "react-hook-form";
import { toast } from "sonner";

import { Field, NativeSelect, Panel, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { api, errorMessage, get } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import { useWsApi } from "@/lib/hooks";
import { agentSchema, type AgentValues } from "@/lib/schemas";
import type { Agent, ModelInfo } from "@/lib/types";

const DEFAULT_TOOLS: AgentValues["tools"] = [
  "end_call",
  "request_callback",
  "record_opt_out",
  "search_knowledge",
  "record_interest",
  "capture_lead",
];

export function agentToValues(a?: Agent, defaultModel?: string): AgentValues {
  return {
    name: a?.name ?? "",
    description: a?.description ?? "",
    personality: a?.personality ?? "Warm, concise and professional. Speaks plainly and never pressures people.",
    system_instructions: a?.system_instructions ?? "",
    greeting: a?.greeting ?? "",
    objectives: a?.objectives ?? [],
    business_knowledge: a?.business_knowledge ?? "",
    language: a?.language ?? "en",
    llm: {
      provider: "ollama",
      model: a?.llm.model ?? defaultModel ?? "",
      temperature: a?.llm.temperature ?? 0.6,
      max_tokens: a?.llm.max_tokens ?? 200,
    },
    stt: { language: a?.stt.language ?? "en", end_silence_ms: a?.stt.end_silence_ms ?? null },
    tts: {
      provider: (a?.tts.provider as "piper" | "macos_say") ?? "piper",
      voice: a?.tts.voice ?? null,
      speaker: a?.tts.speaker ?? null,
      speed: a?.tts.speed ?? 1,
    },
    max_duration_seconds: a?.max_duration_seconds ?? 600,
    silence_timeout_seconds: a?.silence_timeout_seconds ?? 20,
    end_rules: {
      end_phrases: a?.end_rules.end_phrases ?? ["goodbye", "bye", "hang up", "that's all"],
      max_turns: a?.end_rules.max_turns ?? 40,
    },
    tools: (a?.tools as AgentValues["tools"]) ?? DEFAULT_TOOLS,
    fallback_responses: a?.fallback_responses ?? {},
    use_knowledge_base: a?.use_knowledge_base ?? true,
    is_active: a?.is_active ?? true,
    version_note: "",
  };
}

const num = { valueAsNumber: true } as const;

export function AgentForm({
  initial,
  onSubmit,
  submitLabel,
  readOnly,
}: {
  initial: AgentValues;
  onSubmit: (v: AgentValues) => Promise<void>;
  submitLabel: string;
  readOnly?: boolean;
}) {
  const form = useForm<AgentValues>({ resolver: zodResolver(agentSchema), defaultValues: initial });
  const { register, formState } = form;
  const e = formState.errors;
  const submit = form.handleSubmit(async (v) => {
    try {
      await onSubmit(v);
      form.reset(v);
    } catch (err) {
      toast.error(errorMessage(err));
    }
  });

  return (
    <form onSubmit={submit} className="grid gap-5" noValidate>
      <fieldset disabled={readOnly} className="grid gap-5">
        <Panel title="Identity">
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Name" htmlFor="name" error={e.name?.message}>
              <Input id="name" placeholder="e.g. Asha, support desk" {...register("name")} />
            </Field>
            <Field label="Language" htmlFor="language" error={e.language?.message} hint="Language the agent replies in.">
              <NativeSelect id="language" {...register("language")}>
                <option value="en">English</option>
                <option value="en-IN">Indian English</option>
                <option value="hi">Hindi (needs a multilingual speech model)</option>
                <option value="auto">Auto: English or Hindi, follows the caller</option>
              </NativeSelect>
            </Field>
            <Field label="Description" htmlFor="description" className="md:col-span-2" hint="For your team; the agent does not see this.">
              <Input id="description" {...register("description")} />
            </Field>
            <label className="flex items-center gap-2.5 text-sm md:col-span-2">
              <Switch checked={form.watch("is_active")} onCheckedChange={(v) => form.setValue("is_active", v, { shouldDirty: true })} />
              Active (can take calls and be used by campaigns)
            </label>
          </div>
        </Panel>

        <Panel title="Conversation" description="What the agent says and knows. Prices and policies must come from here or the knowledge base.">
          <div className="grid gap-4">
            <Field
              label="Greeting"
              htmlFor="greeting"
              hint="An AI-identity disclosure is added automatically when the workspace requires it."
              error={e.greeting?.message}
            >
              <Textarea id="greeting" rows={2} placeholder="Hi! Thanks for calling. How can I help today?" {...register("greeting")} />
            </Field>
            <Field label="Personality and tone" htmlFor="personality">
              <Textarea id="personality" rows={2} {...register("personality")} />
            </Field>
            <Field label="Instructions" htmlFor="system_instructions" hint="How to handle the conversation, what to ask, what to avoid.">
              <Textarea id="system_instructions" rows={6} {...register("system_instructions")} />
            </Field>
            <ObjectivesEditor form={form} />
            <Field label="Business knowledge" htmlFor="business_knowledge" hint="Short facts always available to the agent. Longer material belongs in Knowledge.">
              <Textarea id="business_knowledge" rows={5} {...register("business_knowledge")} />
            </Field>
            <label className="flex items-center gap-2.5 text-sm">
              <Switch
                checked={form.watch("use_knowledge_base")}
                onCheckedChange={(v) => form.setValue("use_knowledge_base", v, { shouldDirty: true })}
              />
              Look up this workspace’s knowledge base during calls
            </label>
          </div>
        </Panel>

        <ModelAndVoice form={form} />

        <Panel title="Call rules">
          <div className="grid gap-4 md:grid-cols-3">
            <Field label="Maximum call length (seconds)" htmlFor="maxdur" error={e.max_duration_seconds?.message}>
              <Input id="maxdur" type="number" min={30} max={3600} {...register("max_duration_seconds", num)} />
            </Field>
            <Field label="Silence timeout (seconds)" htmlFor="silence" error={e.silence_timeout_seconds?.message} hint="Asks “are you still there?”, then ends.">
              <Input id="silence" type="number" min={5} max={120} {...register("silence_timeout_seconds", num)} />
            </Field>
            <Field label="Maximum turns" htmlFor="maxturns" error={e.end_rules?.max_turns?.message}>
              <Input id="maxturns" type="number" min={2} max={200} {...register("end_rules.max_turns", num)} />
            </Field>
            <Field label="End the call when the caller says" htmlFor="endphrases" className="md:col-span-3" hint="Comma separated.">
              <Input
                id="endphrases"
                defaultValue={initial.end_rules.end_phrases?.join(", ")}
                onChange={(ev) =>
                  form.setValue(
                    "end_rules.end_phrases",
                    ev.target.value.split(",").map((s) => s.trim()).filter(Boolean),
                    { shouldDirty: true },
                  )
                }
              />
            </Field>
            {(
              [
                ["llm_error", "If the model fails", "Sorry, I'm having a technical problem right now. A member of our team will follow up with you."],
                ["silence", "When the caller goes quiet", "Are you still there?"],
                ["goodbye", "Goodbye after silence", "Thank you for your time. Goodbye!"],
              ] as const
            ).map(([key, label, placeholder]) => (
              <Field key={key} label={label} htmlFor={`fb-${key}`} className="md:col-span-3">
                <Input id={`fb-${key}`} placeholder={placeholder} {...register(`fallback_responses.${key}`)} />
              </Field>
            ))}
          </div>
        </Panel>

        <ToolsPanel form={form} />
      </fieldset>

      {!readOnly ? (
        <div className="sticky bottom-0 -mx-1 flex flex-wrap items-center gap-3 border-t bg-background/95 px-1 py-3 backdrop-blur">
          <Input className="max-w-sm" placeholder="What changed? (optional, saved in version history)" {...register("version_note")} />
          <Button type="submit" disabled={formState.isSubmitting || !formState.isDirty}>
            {formState.isSubmitting ? <Spinner /> : null}
            {submitLabel}
          </Button>
          {Object.keys(e).length ? <span className="text-sm text-destructive">Fix the highlighted fields.</span> : null}
        </div>
      ) : null}
    </form>
  );
}

function ObjectivesEditor({ form }: { form: UseFormReturn<AgentValues> }) {
  const objectives = form.watch("objectives");
  return (
    <div className="grid gap-1.5">
      <span className="text-sm font-medium">Objectives</span>
      <ul className="grid gap-2">
        {objectives.map((_, i) => (
          <li key={i} className="flex gap-2">
            <Input aria-label={`Objective ${i + 1}`} {...form.register(`objectives.${i}`)} />
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label="Remove objective"
              onClick={() => form.setValue("objectives", objectives.filter((__, j) => j !== i), { shouldDirty: true })}
            >
              <X />
            </Button>
          </li>
        ))}
      </ul>
      <div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={objectives.length >= 20}
          onClick={() => form.setValue("objectives", [...objectives, ""], { shouldDirty: true })}
        >
          <Plus /> Add objective
        </Button>
      </div>
    </div>
  );
}

function ModelAndVoice({ form }: { form: UseFormReturn<AgentValues> }) {
  const models = useQuery({
    queryKey: ["models"],
    queryFn: () => get<{ available: boolean; error?: string; models: ModelInfo[]; default: string; allow_remote: boolean }>("/api/system/models"),
  });
  const provider = form.watch("tts.provider");
  const voices = useQuery({
    queryKey: ["voices", provider],
    queryFn: () => get<{ available: boolean; error?: string; voices: { id: string; name: string; num_speakers: number; language: string | null }[] }>(
      "/api/system/voices",
      { provider },
    ),
  });
  const voiceId = form.watch("tts.voice");
  const voice = voices.data?.voices.find((v) => v.id === voiceId);
  const [previewing, setPreviewing] = useState(false);
  const e = form.formState.errors;

  const preview = async () => {
    setPreviewing(true);
    try {
      const v = form.getValues();
      const res = await api<Response>("POST", "/api/system/voices/preview", {
        raw: true,
        json: {
          text: v.greeting || "Hello! This is how I will sound on your calls.",
          provider: v.tts.provider,
          voice: v.tts.voice || null,
          speaker: v.tts.speaker || null,
          speed: v.tts.speed,
        },
      });
      const url = URL.createObjectURL(await res.blob());
      const audio = new Audio(url);
      audio.onended = () => URL.revokeObjectURL(url);
      await audio.play();
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setPreviewing(false);
    }
  };

  return (
    <Panel title="Model and voice" description="Only models installed in your local Ollama can be selected.">
      <div className="grid gap-4 md:grid-cols-3">
        <Field
          label="Language model"
          htmlFor="model"
          className="md:col-span-3"
          error={e.llm?.model?.message ?? (models.data && !models.data.available ? models.data.error : undefined)}
        >
          <NativeSelect id="model" {...form.register("llm.model")}>
            <option value="">Choose a model…</option>
            {models.data?.models.map((m) => (
              <option key={m.name} value={m.name} disabled={!m.is_local && !models.data?.allow_remote}>
                {m.name}
                {m.parameter_size ? ` (${m.parameter_size}${m.quantization ? `, ${m.quantization}` : ""})` : ""}
                {m.size_bytes && m.is_local ? `, ${formatBytes(m.size_bytes)}` : ""}
                {!m.is_local
                  ? models.data?.allow_remote ? ` (cloud: ${m.remote_host ?? "remote"}, conversations leave this Mac)` : " (remote, not allowed)"
                  : !m.fits_memory ? " (large for this Mac)" : ""}
              </option>
            ))}
          </NativeSelect>
        </Field>
        <Field label={`Creativity: ${form.watch("llm.temperature").toFixed(1)}`} htmlFor="temp" hint="Lower is more predictable.">
          <input id="temp" type="range" min={0} max={1.2} step={0.1} className="accent-[var(--signal)]" {...form.register("llm.temperature", num)} />
        </Field>
        <Field label="Longest reply (tokens)" htmlFor="maxtok" error={e.llm?.max_tokens?.message}>
          <Input id="maxtok" type="number" min={16} max={1024} {...form.register("llm.max_tokens", num)} />
        </Field>
        <Field label="End-of-turn pause (ms)" htmlFor="eos" hint="Silence before the agent replies. Blank uses the system default.">
          <Input
            id="eos"
            type="number"
            min={300}
            max={3000}
            {...form.register("stt.end_silence_ms", { setValueAs: (v) => (v === "" || v === null ? null : Number(v)) })}
          />
        </Field>
        <Field label="Voice engine" htmlFor="ttsprov">
          <NativeSelect id="ttsprov" {...form.register("tts.provider", { onChange: () => form.setValue("tts.voice", null) })}>
            <option value="piper">Piper (local neural voice)</option>
            <option value="macos_say">macOS system voice</option>
          </NativeSelect>
        </Field>
        <Field label="Voice" htmlFor="voice" error={voices.data && !voices.data.available ? voices.data.error : undefined}>
          <NativeSelect id="voice" {...form.register("tts.voice", { setValueAs: (v) => v || null })}>
            <option value="">Default voice</option>
            {voices.data?.voices.map((v) => (
              <option key={v.id} value={v.id}>
                {v.name}
                {v.language ? ` (${v.language})` : ""}
              </option>
            ))}
          </NativeSelect>
        </Field>
        <Field
          label="Speaker"
          htmlFor="speaker"
          hint={voice && voice.num_speakers > 1 ? `This voice has ${voice.num_speakers} speakers (0–${voice.num_speakers - 1}).` : "Only for multi-speaker voices."}
        >
          <Input id="speaker" placeholder="0" {...form.register("tts.speaker", { setValueAs: (v) => (v === "" ? null : String(v)) })} />
        </Field>
        <Field label={`Speaking rate: ${form.watch("tts.speed").toFixed(2)}×`} htmlFor="speed">
          <input id="speed" type="range" min={0.7} max={1.4} step={0.05} className="accent-[var(--signal)]" {...form.register("tts.speed", num)} />
        </Field>
        <div className="flex items-end">
          <Button type="button" variant="outline" onClick={preview} disabled={previewing}>
            {previewing ? <Spinner /> : <Volume2 />} Preview voice
          </Button>
        </div>
      </div>
    </Panel>
  );
}

function ToolsPanel({ form }: { form: UseFormReturn<AgentValues> }) {
  const wsApi = useWsApi();
  const catalog = useQuery({
    queryKey: ["tool-catalog"],
    queryFn: () => get<{ name: string; label: string; description: string }[]>(wsApi("/agents/tools")),
  });
  const tools = form.watch("tools");
  return (
    <Panel
      title="Actions the agent may take"
      description="The model can only request these. Each request is validated and recorded for your team; none of them completes a booking, payment or refund."
    >
      <ul className="grid gap-3 md:grid-cols-2">
        {catalog.data?.map((t) => {
          const checked = tools.includes(t.name as AgentValues["tools"][number]);
          return (
            <li key={t.name}>
              <label className="flex cursor-pointer gap-3 rounded-lg border p-3 has-[[data-checked]]:border-ok/60">
                <Checkbox
                  checked={checked}
                  onCheckedChange={(v) =>
                    form.setValue(
                      "tools",
                      v ? [...tools, t.name as AgentValues["tools"][number]] : tools.filter((x) => x !== t.name),
                      { shouldDirty: true },
                    )
                  }
                />
                <span className="min-w-0">
                  <span className="block text-sm font-medium">{t.label}</span>
                  <span className="block text-xs text-muted-foreground">{t.description}</span>
                </span>
              </label>
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}
