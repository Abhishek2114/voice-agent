"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import { toast } from "sonner";

import { Field, NativeSelect, PageHeader, Panel, Spinner } from "@/components/common";
import { CONTACT_STATUSES } from "@/components/contacts/contact-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { errorMessage, get, post } from "@/lib/api";
import { humanize, WEEKDAYS } from "@/lib/format";
import { useWorkspaceId, useWsApi } from "@/lib/hooks";
import { campaignSchema, type CampaignValues } from "@/lib/schemas";
import type { Agent, Campaign, Contact, Page } from "@/lib/types";
import { cn } from "@/lib/utils";

const num = { valueAsNumber: true } as const;

export default function NewCampaignPage() {
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const router = useRouter();
  const qc = useQueryClient();
  const agents = useQuery({ queryKey: ["agents", ws], queryFn: () => get<Agent[]>(wsApi("/agents")) });
  const tags = useQuery({ queryKey: ["contacts", ws, "tags"], queryFn: () => get<Page<Contact> & { tags: string[] }>(wsApi("/contacts"), { page_size: 1 }) });
  const form = useForm<CampaignValues>({
    resolver: zodResolver(campaignSchema),
    defaultValues: {
      name: "",
      description: "",
      agent_id: "",
      objective: "",
      purpose: "promotional",
      window_start: "10:00",
      window_end: "19:00",
      calling_days: [0, 1, 2, 3, 4, 5],
      max_attempts: 2,
      retry_delay_minutes: 60,
      max_concurrent_calls: 1,
      schedule_start_at: "",
      contact_tag: "",
      contact_status: "",
      callee_mode: "scripted",
      answer_rate: 0.75,
      ignore_calling_window: false,
    },
  });
  const e = form.formState.errors;
  const days = form.watch("calling_days");
  const tag = form.watch("contact_tag");
  const status = form.watch("contact_status");
  const audience = useQuery({
    queryKey: ["contacts", ws, "audience", tag, status],
    queryFn: () => get<Page<Contact>>(wsApi("/contacts"), { tag, status, page_size: 1 }),
  });

  const submit = form.handleSubmit(async (v) => {
    try {
      const flt: Record<string, string> = {};
      if (v.contact_tag) flt.tag = v.contact_tag;
      if (v.contact_status) flt.status = v.contact_status;
      let contact_ids: string[] = [];
      if (!v.contact_tag && !v.contact_status) {
        // "everyone": resolve ids page by page (backend requires an explicit selection)
        let page = 1;
        for (;;) {
          const r = await get<Page<Contact>>(wsApi("/contacts"), { page, page_size: 200 });
          contact_ids = contact_ids.concat(r.items.map((c) => c.id));
          if (r.items.length < 200) break;
          page += 1;
        }
      }
      const c = await post<Campaign>(wsApi("/campaigns"), {
        name: v.name,
        description: v.description,
        agent_id: v.agent_id,
        objective: v.objective,
        purpose: v.purpose,
        mode: "SIMULATION",
        window_start: v.window_start,
        window_end: v.window_end,
        calling_days: v.calling_days,
        max_attempts: v.max_attempts,
        retry_delay_minutes: v.retry_delay_minutes,
        max_concurrent_calls: v.max_concurrent_calls,
        schedule_start_at: v.schedule_start_at ? new Date(v.schedule_start_at).toISOString() : null,
        contact_ids,
        contact_filter: Object.keys(flt).length ? flt : null,
        simulation: {
          answer_rate: v.answer_rate,
          busy_rate: Math.min(0.05, 1 - v.answer_rate),
          fail_rate: Math.min(0.05, Math.max(0, 1 - v.answer_rate - 0.05)),
          callee_mode: v.callee_mode,
          ignore_calling_window: v.ignore_calling_window,
        },
      });
      toast.success(`Campaign saved with ${c.progress.total ?? 0} contacts`);
      await qc.invalidateQueries({ queryKey: ["campaigns", ws] });
      router.push(`/w/${ws}/campaigns/${c.id}`);
    } catch (err) {
      toast.error(errorMessage(err));
    }
  });

  return (
    <>
      <PageHeader title="New campaign" description="Saved as a draft. Nothing is dialled until you start it." />
      <form onSubmit={submit} className="grid gap-5" noValidate>
        <Panel title="What and who">
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Campaign name" htmlFor="name" error={e.name?.message}>
              <Input id="name" {...form.register("name")} placeholder="e.g. Festive hosting offer" />
            </Field>
            <Field label="Agent" htmlFor="agent" error={e.agent_id?.message}>
              <NativeSelect id="agent" {...form.register("agent_id")}>
                <option value="">Choose an agent…</option>
                {agents.data?.filter((a) => a.is_active).map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <Field label="Objective for these calls" htmlFor="objective" className="md:col-span-2" hint="Added to the agent’s instructions for this campaign only.">
              <Textarea id="objective" rows={2} {...form.register("objective")} />
            </Field>
            <Field
              label="Purpose"
              htmlFor="purpose"
              hint="Determines which consent and DND checks apply (see Compliance)."
            >
              <NativeSelect id="purpose" {...form.register("purpose")}>
                <option value="promotional">Promotional</option>
                <option value="service">Service</option>
                <option value="transactional">Transactional</option>
                <option value="inquiry_followup">Follow-up on an inquiry</option>
              </NativeSelect>
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Contacts with tag" htmlFor="tag">
                <NativeSelect id="tag" {...form.register("contact_tag")}>
                  <option value="">Any tag</option>
                  {tags.data?.tags.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </NativeSelect>
              </Field>
              <Field label="And status" htmlFor="cstatus">
                <NativeSelect id="cstatus" {...form.register("contact_status")}>
                  <option value="">Any status</option>
                  {CONTACT_STATUSES.filter((s) => !["do_not_call", "wrong_number"].includes(s)).map((s) => (
                    <option key={s} value={s}>
                      {humanize(s)}
                    </option>
                  ))}
                </NativeSelect>
              </Field>
            </div>
            <p className="text-sm text-muted-foreground md:col-span-2">
              {audience.data ? `${audience.data.total} contacts match.` : "Counting contacts…"} Contacts that fail compliance checks are skipped when the
              campaign runs, with the reason recorded.
            </p>
          </div>
        </Panel>

        <Panel title="When" description="The campaign window can only narrow the workspace’s permitted calling hours, never widen them.">
          <div className="grid gap-4 md:grid-cols-3">
            <Field label="Calls start at" htmlFor="ws" error={e.window_start?.message}>
              <Input id="ws" type="time" {...form.register("window_start")} />
            </Field>
            <Field label="Calls stop at" htmlFor="we" error={e.window_end?.message}>
              <Input id="we" type="time" {...form.register("window_end")} />
            </Field>
            <Field label="Start date (optional)" htmlFor="start" hint="Leave blank to start when you press Start.">
              <Input id="start" type="datetime-local" {...form.register("schedule_start_at")} />
            </Field>
            <fieldset className="md:col-span-3">
              <legend className="mb-1.5 text-sm font-medium">Calling days</legend>
              <div className="flex flex-wrap gap-1.5">
                {WEEKDAYS.map((d, i) => {
                  const on = days.includes(i);
                  return (
                    <button
                      key={d}
                      type="button"
                      aria-pressed={on}
                      onClick={() =>
                        form.setValue("calling_days", on ? days.filter((x) => x !== i) : [...days, i].sort(), { shouldValidate: true })
                      }
                      className={cn("h-8 w-12 rounded-md border text-sm", on ? "border-foreground bg-foreground text-background" : "text-muted-foreground")}
                    >
                      {d}
                    </button>
                  );
                })}
              </div>
              {e.calling_days ? <p className="mt-1 text-xs text-destructive">{e.calling_days.message}</p> : null}
            </fieldset>
          </div>
        </Panel>

        <Panel title="Pace and retries">
          <div className="grid gap-4 md:grid-cols-3">
            <Field label="Attempts per contact" htmlFor="att" error={e.max_attempts?.message}>
              <Input id="att" type="number" min={1} max={10} {...form.register("max_attempts", num)} />
            </Field>
            <Field label="Wait before retrying (minutes)" htmlFor="retry" error={e.retry_delay_minutes?.message}>
              <Input id="retry" type="number" min={1} {...form.register("retry_delay_minutes", num)} />
            </Field>
            <Field label="Calls at the same time" htmlFor="conc" error={e.max_concurrent_calls?.message} hint="The local model still answers one turn at a time.">
              <Input id="conc" type="number" min={1} max={20} {...form.register("max_concurrent_calls", num)} />
            </Field>
          </div>
        </Panel>

        <Panel title="Simulation" description="This campaign runs as a simulation. No phone numbers are dialled; simulated people answer and reply." className="sim-hatch">
          <div className="grid gap-4 rounded-lg bg-panel p-4 md:grid-cols-3">
            <Field label={`Answer rate: ${Math.round(form.watch("answer_rate") * 100)}%`} htmlFor="ans">
              <input id="ans" type="range" min={0.1} max={1} step={0.05} className="accent-[var(--signal)]" {...form.register("answer_rate", num)} />
            </Field>
            <Field label="Simulated replies" htmlFor="callee">
              <NativeSelect id="callee" {...form.register("callee_mode")}>
                <option value="scripted">Scripted (fast)</option>
                <option value="llm">Played by the local model (slower)</option>
              </NativeSelect>
            </Field>
            <label className="flex items-center gap-2.5 self-end pb-1.5 text-sm">
              <Switch
                checked={form.watch("ignore_calling_window")}
                onCheckedChange={(v) => form.setValue("ignore_calling_window", v)}
              />
              Ignore calling hours (simulation only)
            </label>
          </div>
        </Panel>

        <div className="flex gap-3">
          <Button type="submit" disabled={form.formState.isSubmitting}>
            {form.formState.isSubmitting ? <Spinner /> : null} Save draft
          </Button>
          <Button type="button" variant="outline" onClick={() => router.back()}>
            Cancel
          </Button>
        </div>
      </form>
    </>
  );
}
