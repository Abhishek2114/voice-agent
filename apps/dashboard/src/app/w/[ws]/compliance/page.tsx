"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Trash2 } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";

import { ConfirmButton, EmptyState, ErrorState, Field, LoadingRows, NativeSelect, PageHeader, Pagination, Panel, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { del, errorMessage, get, post, put } from "@/lib/api";
import { formatDateTime, formatPhone, humanize, WEEKDAYS } from "@/lib/format";
import { useCan, useWorkspaceId, useWsApi } from "@/lib/hooks";
import { cn } from "@/lib/utils";

interface Policy {
  ai_disclosure_required: boolean;
  ai_disclosure_text: string;
  recording_enabled: boolean;
  recording_disclosure_text: string;
  calling_window_start: string;
  calling_window_end: string;
  calling_days: number[];
  max_calls_per_contact_per_day: number;
  max_calls_per_contact_per_week: number;
  consent_required_for: string[];
  inquiry_followup_days: number;
  dnd_check_required_for: string[];
  dnd_max_age_days: number;
  retention_days_recordings: number;
  retention_days_transcripts: number;
  pstn_authorized: boolean;
  telemarketer_registration_id: string;
  registered_number_series: string;
  a2p_declared_to_provider: boolean;
}

const PURPOSES = ["promotional", "service", "transactional", "inquiry_followup"];

export default function CompliancePage() {
  return (
    <>
      <PageHeader
        title="Compliance"
        description="Controls that decide who may be called, when, and how often. They help you operate responsibly; they don’t make calling legal on their own."
      />
      <p className="mb-5 flex gap-2.5 rounded-lg border border-warn/40 bg-warn/8 px-4 py-3 text-sm">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" />
        <span>
          Real calls to Indian numbers are governed by TRAI’s TCCCPR (including the 2025 and 2026 amendments), DLT registration, the 140/160 number series
          and your telecom provider’s terms. Check current requirements with your provider and a lawyer before enabling real calling. See docs/COMPLIANCE.md.
        </span>
      </p>
      <Tabs defaultValue="policy">
        <TabsList className="mb-5 flex-wrap">
          <TabsTrigger value="policy">Policy</TabsTrigger>
          <TabsTrigger value="suppression">Do-not-call list</TabsTrigger>
          <TabsTrigger value="dnd">DND scrub results</TabsTrigger>
          <TabsTrigger value="consents">Consent records</TabsTrigger>
          <TabsTrigger value="audit">Audit log</TabsTrigger>
        </TabsList>
        <TabsContent value="policy">
          <PolicyForm />
        </TabsContent>
        <TabsContent value="suppression">
          <Suppression />
        </TabsContent>
        <TabsContent value="dnd">
          <DndScrub />
        </TabsContent>
        <TabsContent value="consents">
          <Consents />
        </TabsContent>
        <TabsContent value="audit">
          <Audit />
        </TabsContent>
      </Tabs>
    </>
  );
}

function PolicyForm() {
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const data = useQuery({ queryKey: ["policy", ws], queryFn: () => get<{ policy: Policy; pstn_missing: string[] }>(wsApi("/compliance")) });
  if (data.error) return <ErrorState error={data.error} onRetry={() => data.refetch()} />;
  if (!data.data) return <LoadingRows rows={8} />;
  return <PolicyEditor initial={data.data.policy} pstnMissing={data.data.pstn_missing} />;
}

function PolicyEditor({ initial, pstnMissing }: { initial: Policy; pstnMissing: string[] }) {
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const qc = useQueryClient();
  const canAdmin = useCan("admin");
  const [p, setP] = useState<Policy>(initial);
  const save = useMutation({
    mutationFn: (v: Policy) => put<{ policy: Policy; pstn_missing: string[] }>(wsApi("/compliance"), v),
    onSuccess: (r) => {
      qc.setQueryData(["policy", ws], r);
      toast.success("Policy saved");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const set = <K extends keyof Policy>(k: K, v: Policy[K]) => setP({ ...p, [k]: v });
  const toggleIn = (k: "consent_required_for" | "dnd_check_required_for", v: string) =>
    set(k, p[k].includes(v) ? p[k].filter((x) => x !== v) : [...p[k], v]);

  return (
    <form
      className="grid gap-5"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate(p);
      }}
    >
      <fieldset disabled={!canAdmin} className="grid gap-5">
        <Panel title="Disclosures" description="Spoken at the start of every call.">
          <div className="grid gap-4">
            <label className="flex items-center gap-2.5 text-sm">
              <Switch checked={p.ai_disclosure_required} onCheckedChange={(v) => set("ai_disclosure_required", v)} />
              Tell people they are speaking with an AI
            </label>
            <Field label="AI disclosure for phone calls" htmlFor="aidisc" hint="{business} is replaced with the workspace name.">
              <Input id="aidisc" value={p.ai_disclosure_text} onChange={(e) => set("ai_disclosure_text", e.target.value)} />
            </Field>
            <label className="flex items-center gap-2.5 text-sm">
              <Switch checked={p.recording_enabled} onCheckedChange={(v) => set("recording_enabled", v)} />
              Record call audio (stored on this machine, deleted after the retention period)
            </label>
            {p.recording_enabled ? (
              <Field label="Recording disclosure" htmlFor="recdisc">
                <Input id="recdisc" value={p.recording_disclosure_text} onChange={(e) => set("recording_disclosure_text", e.target.value)} />
              </Field>
            ) : null}
          </div>
        </Panel>

        <Panel title="Calling hours and frequency" description="Applies to every campaign in this workspace, in the workspace timezone.">
          <div className="grid gap-4 md:grid-cols-4">
            <Field label="Earliest call" htmlFor="cws">
              <Input id="cws" type="time" value={p.calling_window_start} onChange={(e) => set("calling_window_start", e.target.value)} />
            </Field>
            <Field label="Latest call" htmlFor="cwe">
              <Input id="cwe" type="time" value={p.calling_window_end} onChange={(e) => set("calling_window_end", e.target.value)} />
            </Field>
            <Field label="Calls per person per day" htmlFor="perday">
              <Input id="perday" type="number" min={1} max={10} value={p.max_calls_per_contact_per_day} onChange={(e) => set("max_calls_per_contact_per_day", Number(e.target.value))} />
            </Field>
            <Field label="Calls per person per week" htmlFor="perweek">
              <Input id="perweek" type="number" min={1} max={30} value={p.max_calls_per_contact_per_week} onChange={(e) => set("max_calls_per_contact_per_week", Number(e.target.value))} />
            </Field>
            <fieldset className="md:col-span-4">
              <legend className="mb-1.5 text-sm font-medium">Calling days</legend>
              <div className="flex flex-wrap gap-1.5">
                {WEEKDAYS.map((d, i) => {
                  const on = p.calling_days.includes(i);
                  return (
                    <button
                      type="button"
                      key={d}
                      aria-pressed={on}
                      onClick={() => set("calling_days", on ? p.calling_days.filter((x) => x !== i) : [...p.calling_days, i].sort())}
                      className={cn("h-8 w-12 rounded-md border text-sm", on ? "border-foreground bg-foreground text-background" : "text-muted-foreground")}
                    >
                      {d}
                    </button>
                  );
                })}
              </div>
            </fieldset>
          </div>
        </Panel>

        <Panel title="Consent and DND" description="Calls without recorded consent, or without a recent DND check where required, are blocked.">
          <div className="grid gap-5 md:grid-cols-2">
            <fieldset>
              <legend className="mb-2 text-sm font-medium">Require recorded consent for</legend>
              {PURPOSES.map((x) => (
                <label key={x} className="mb-1.5 flex items-center gap-2 text-sm">
                  <Checkbox checked={p.consent_required_for.includes(x)} onCheckedChange={() => toggleIn("consent_required_for", x)} />
                  {humanize(x)} calls
                </label>
              ))}
            </fieldset>
            <fieldset>
              <legend className="mb-2 text-sm font-medium">Require a DND check for</legend>
              {PURPOSES.map((x) => (
                <label key={x} className="mb-1.5 flex items-center gap-2 text-sm">
                  <Checkbox checked={p.dnd_check_required_for.includes(x)} onCheckedChange={() => toggleIn("dnd_check_required_for", x)} />
                  {humanize(x)} calls
                </label>
              ))}
            </fieldset>
            <Field label="DND results stay valid for (days)" htmlFor="dndage">
              <Input id="dndage" type="number" min={1} max={365} value={p.dnd_max_age_days} onChange={(e) => set("dnd_max_age_days", Number(e.target.value))} />
            </Field>
            <Field label="Inquiry follow-up window (days)" htmlFor="inq" hint="How long after someone contacts you a follow-up call counts as answering their inquiry.">
              <Input id="inq" type="number" min={1} max={30} value={p.inquiry_followup_days} onChange={(e) => set("inquiry_followup_days", Number(e.target.value))} />
            </Field>
          </div>
        </Panel>

        <Panel title="Data retention">
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Delete recordings after (days)" htmlFor="retrec">
              <Input id="retrec" type="number" min={1} value={p.retention_days_recordings} onChange={(e) => set("retention_days_recordings", Number(e.target.value))} />
            </Field>
            <Field label="Delete transcripts after (days)" htmlFor="rettr">
              <Input id="rettr" type="number" min={1} value={p.retention_days_transcripts} onChange={(e) => set("retention_days_transcripts", Number(e.target.value))} />
            </Field>
          </div>
        </Panel>

        <Panel
          title="Real phone calls"
          description="All four must be recorded before this workspace can place real calls. These are your statements; voice-agent cannot verify them."
        >
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Telemarketer / entity registration ID" htmlFor="tmid">
              <Input id="tmid" value={p.telemarketer_registration_id} onChange={(e) => set("telemarketer_registration_id", e.target.value)} />
            </Field>
            <Field label="Registered calling number series" htmlFor="series" hint="e.g. the 140xx series for promotional or 160xx for service calls">
              <Input id="series" value={p.registered_number_series} onChange={(e) => set("registered_number_series", e.target.value)} />
            </Field>
            <label className="flex items-center gap-2.5 text-sm">
              <Checkbox checked={p.a2p_declared_to_provider} onCheckedChange={(v) => set("a2p_declared_to_provider", !!v)} />
              Automated calling has been declared to our telecom provider
            </label>
            <label className="flex items-center gap-2.5 text-sm">
              <Checkbox checked={p.pstn_authorized} onCheckedChange={(v) => set("pstn_authorized", !!v)} />
              This workspace is authorised to place real calls
            </label>
          </div>
          {pstnMissing.length ? (
            <ul className="mt-4 grid gap-1 text-sm text-muted-foreground">
              {pstnMissing.map((m) => (
                <li key={m}>Missing: {m}</li>
              ))}
            </ul>
          ) : (
            <p className="mt-4 text-sm">All workspace prerequisites recorded. Real calling also has to be enabled for the installation and a provider configured in Settings.</p>
          )}
        </Panel>
      </fieldset>
      {canAdmin ? (
        <div>
          <Button type="submit" disabled={save.isPending}>
            {save.isPending ? <Spinner /> : null} Save policy
          </Button>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">Only workspace admins can change the policy.</p>
      )}
    </form>
  );
}

function Suppression() {
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const qc = useQueryClient();
  const canEdit = useCan("editor");
  const canAdmin = useCan("admin");
  const [page, setPage] = useState(1);
  const [phone, setPhone] = useState("");
  const [reason, setReason] = useState("opt_out");
  const list = useQuery({
    queryKey: ["suppression", ws, page],
    queryFn: () => get<{ items: { id: string; phone_e164: string; reason: string; source: string; created_at: string }[]; total: number }>(wsApi("/compliance/suppression"), { page }),
  });
  const add = useMutation({
    mutationFn: () => post(wsApi("/compliance/suppression"), { phone, reason }),
    onSuccess: () => {
      toast.success("Number added to the do-not-call list");
      setPhone("");
      void qc.invalidateQueries({ queryKey: ["suppression", ws] });
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <Panel title="Do-not-call list" description="Numbers here are never called by any campaign in this workspace. Opt-outs from calls are added automatically." bodyClassName="p-0">
      {canEdit ? (
        <form
          className="flex flex-wrap gap-2 border-b p-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (phone.trim()) add.mutate();
          }}
        >
          <Input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="Phone number" className="w-56" aria-label="Phone number" />
          <NativeSelect value={reason} onChange={(e) => setReason(e.target.value)} className="w-44" aria-label="Reason">
            <option value="opt_out">Asked not to be called</option>
            <option value="dnd_registry">On the DND registry</option>
            <option value="wrong_number">Wrong number</option>
            <option value="manual">Other</option>
          </NativeSelect>
          <Button type="submit" disabled={add.isPending}>
            Add number
          </Button>
        </form>
      ) : null}
      {!list.data ? (
        <div className="p-5">
          <LoadingRows />
        </div>
      ) : list.data.items.length === 0 ? (
        <EmptyState title="The list is empty" />
      ) : (
        <ul className="divide-y text-sm">
          {list.data.items.map((s) => (
            <li key={s.id} className="flex items-center gap-4 px-5 py-2.5">
              <span className="w-44 font-mono text-xs">{formatPhone(s.phone_e164)}</span>
              <span className="flex-1">{humanize(s.reason)}</span>
              <span className="text-xs text-muted-foreground">{formatDateTime(s.created_at)}</span>
              {canAdmin ? (
                <ConfirmButton
                  title="Remove from the do-not-call list?"
                  description="Only do this if the person has since given new consent. The removal is recorded in the audit log."
                  confirmLabel="Remove"
                  variant="ghost"
                  size="icon-sm"
                  onConfirm={async () => {
                    await del(wsApi(`/compliance/suppression/${s.id}`));
                    void qc.invalidateQueries({ queryKey: ["suppression", ws] });
                  }}
                >
                  <Trash2 />
                  <span className="sr-only">Remove</span>
                </ConfirmButton>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {list.data ? (
        <div className="px-5 pb-4">
          <Pagination page={page} pageSize={50} total={list.data.total} onPage={setPage} />
        </div>
      ) : null}
    </Panel>
  );
}

function DndScrub() {
  const wsApi = useWsApi();
  const canAdmin = useCan("admin");
  const [text, setText] = useState("");
  const [status, setStatus] = useState<"registered" | "not_registered">("not_registered");
  const run = useMutation({
    mutationFn: () => {
      const entries = text
        .split(/[\n,;]+/)
        .map((s) => s.trim())
        .filter(Boolean)
        .map((phone) => ({ phone, status }));
      return post<{ updated: number; invalid: number; unmatched: number }>(wsApi("/compliance/dnd-scrub"), { entries });
    },
    onSuccess: (r) => {
      toast.success(`${r.updated} contacts updated, ${r.unmatched} numbers not in contacts, ${r.invalid} invalid`);
      setText("");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <Panel
      title="Apply DND scrub results"
      description="There is no public lookup for the NCPR/DND registry. Scrub numbers through your telecom provider or DLT platform, then paste the results here."
    >
      {!canAdmin ? (
        <p className="text-sm text-muted-foreground">Only workspace admins can import scrub results.</p>
      ) : (
        <div className="grid max-w-2xl gap-4">
          <Field label="Numbers" htmlFor="scrub" hint="One per line, or separated by commas.">
            <Textarea id="scrub" rows={8} value={text} onChange={(e) => setText(e.target.value)} className="font-mono text-xs" />
          </Field>
          <Field label="Result for these numbers" htmlFor="scrubstatus">
            <NativeSelect id="scrubstatus" value={status} onChange={(e) => setStatus(e.target.value as "registered" | "not_registered")}>
              <option value="not_registered">Not registered on DND</option>
              <option value="registered">Registered on DND (do not call for promotions)</option>
            </NativeSelect>
          </Field>
          <div>
            <Button onClick={() => run.mutate()} disabled={run.isPending || !text.trim()}>
              {run.isPending ? <Spinner /> : null} Apply results
            </Button>
          </div>
        </div>
      )}
    </Panel>
  );
}

function Consents() {
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const [page, setPage] = useState(1);
  const list = useQuery({
    queryKey: ["consents", ws, page],
    queryFn: () =>
      get<{ items: { id: string; contact_id: string | null; phone_e164: string | null; purpose: string; status: string; source: string; evidence: string; obtained_at: string }[]; total: number }>(
        wsApi("/compliance/consents"),
        { page },
      ),
  });
  return (
    <Panel title="Consent records" description="Recorded from contact pages, CSV imports with evidence, and callers who agreed to a follow-up during a call." bodyClassName="p-0">
      {!list.data ? (
        <div className="p-5">
          <LoadingRows />
        </div>
      ) : list.data.items.length === 0 ? (
        <EmptyState title="No consent recorded yet" />
      ) : (
        <ul className="divide-y text-sm">
          {list.data.items.map((r) => (
            <li key={r.id} className="grid gap-1 px-5 py-3 md:grid-cols-[11rem_9rem_minmax(0,1fr)_9rem] md:gap-4">
              {r.contact_id ? (
                <Link href={`/w/${ws}/contacts/${r.contact_id}`} className="font-mono text-xs underline-offset-4 hover:underline">
                  {formatPhone(r.phone_e164)}
                </Link>
              ) : (
                <span className="font-mono text-xs">{formatPhone(r.phone_e164)}</span>
              )}
              <span>
                {humanize(r.purpose)}
                {r.status !== "granted" ? <span className="text-destructive"> ({r.status})</span> : null}
              </span>
              <span className="truncate text-muted-foreground" title={r.evidence}>
                {humanize(r.source)}: {r.evidence}
              </span>
              <span className="text-xs text-muted-foreground md:text-right">{formatDateTime(r.obtained_at)}</span>
            </li>
          ))}
        </ul>
      )}
      {list.data ? (
        <div className="px-5 pb-4">
          <Pagination page={page} pageSize={50} total={list.data.total} onPage={setPage} />
        </div>
      ) : null}
    </Panel>
  );
}

function Audit() {
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const canAdmin = useCan("admin");
  const [page, setPage] = useState(1);
  const [action, setAction] = useState("");
  const list = useQuery({
    queryKey: ["audit", ws, page, action],
    queryFn: () => get<{ items: { id: string; action: string; entity_type: string | null; details: Record<string, unknown>; ip: string | null; created_at: string }[]; total: number }>(wsApi("/compliance/audit"), { page, action }),
    enabled: canAdmin,
  });
  if (!canAdmin) return <p className="text-sm text-muted-foreground">Only workspace admins can read the audit log.</p>;
  return (
    <Panel
      title="Audit log"
      actions={
        <NativeSelect value={action} onChange={(e) => (setAction(e.target.value), setPage(1))} className="h-7 w-44 text-xs" aria-label="Filter actions">
          <option value="">All actions</option>
          {["compliance", "consent", "contact", "campaign", "agent", "recording", "import", "auth", "member", "lead"].map((a) => (
            <option key={a} value={a}>
              {humanize(a)}
            </option>
          ))}
        </NativeSelect>
      }
      bodyClassName="p-0"
    >
      {!list.data ? (
        <div className="p-5">
          <LoadingRows />
        </div>
      ) : list.data.items.length === 0 ? (
        <EmptyState title="No entries" />
      ) : (
        <ul className="divide-y text-sm">
          {list.data.items.map((a) => (
            <li key={a.id} className="grid gap-1 px-5 py-2.5 md:grid-cols-[13rem_minmax(0,1fr)_9rem] md:gap-4">
              <span className="font-medium">{a.action}</span>
              <span className="truncate font-mono text-xs text-muted-foreground" title={JSON.stringify(a.details)}>
                {Object.keys(a.details).length ? JSON.stringify(a.details) : ""}
              </span>
              <span className="text-xs text-muted-foreground md:text-right">{formatDateTime(a.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
      {list.data ? (
        <div className="px-5 pb-4">
          <Pagination page={page} pageSize={50} total={list.data.total} onPage={setPage} />
        </div>
      ) : null}
    </Panel>
  );
}
