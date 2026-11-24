"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, Pause, Play, ShieldCheck, Trash2 } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { ProgressBar } from "@/components/campaigns/progress-bar";
import { BarsChart } from "@/components/charts";
import {
  ConfirmButton,
  ErrorState,
  LoadingRows,
  ModeBadge,
  NativeSelect,
  OutcomeBadge,
  PageHeader,
  Pagination,
  Panel,
  Stat,
  StatusLamp,
  StatusText,
} from "@/components/common";
import { Button } from "@/components/ui/button";
import { del, errorMessage, get, post } from "@/lib/api";
import { formatDateTime, formatDuration, formatPercent, formatPhone, humanize, outcomeLabel, WEEKDAYS } from "@/lib/format";
import { useCan, useWorkspaceId, useWsApi } from "@/lib/hooks";
import type { Campaign } from "@/lib/types";

interface Report {
  calls: number;
  connected: number;
  connect_rate: number;
  positive_outcomes: number;
  conversion_rate: number;
  avg_duration_seconds: number;
  outcomes: Record<string, number>;
}
interface CC {
  id: string;
  contact_id: string;
  name: string;
  phone_e164: string | null;
  status: string;
  attempts: number;
  last_outcome: string | null;
  last_call_id: string | null;
  next_attempt_at: string | null;
  skip_reason: string;
}

export default function CampaignDetailPage() {
  const { id } = useParams<{ id: string }>();
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const qc = useQueryClient();
  const router = useRouter();
  const canEdit = useCan("editor");
  const [ccStatus, setCcStatus] = useState("");
  const [ccPage, setCcPage] = useState(1);
  const campaign = useQuery({
    queryKey: ["campaign", id],
    queryFn: () => get<Campaign>(wsApi(`/campaigns/${id}`)),
    refetchInterval: (q) => (q.state.data?.status === "running" ? 3000 : false),
  });
  const running = campaign.data?.status === "running";
  const report = useQuery({ queryKey: ["campaign-report", id], queryFn: () => get<Report>(wsApi(`/campaigns/${id}/report`)), refetchInterval: running ? 4000 : false });
  const contacts = useQuery({
    queryKey: ["campaign-contacts", id, ccStatus, ccPage],
    queryFn: () => get<{ items: CC[]; total: number }>(wsApi(`/campaigns/${id}/contacts`), { status: ccStatus, page: ccPage }),
    refetchInterval: running ? 4000 : false,
  });
  const preview = useMutation({
    mutationFn: () => post<{ checked: number; eligible_now: number; blocked_by: Record<string, number> }>(wsApi(`/campaigns/${id}/preview`)),
    onError: (e) => toast.error(errorMessage(e)),
  });
  const action = useMutation({
    mutationFn: (a: "start" | "pause" | "resume" | "cancel") => post<Campaign>(wsApi(`/campaigns/${id}/${a}`)),
    onSuccess: (c) => {
      toast.success(`Campaign ${humanize(c.status).toLowerCase()}`);
      void qc.invalidateQueries({ queryKey: ["campaign", id] });
      void qc.invalidateQueries({ queryKey: ["campaigns", ws] });
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  if (campaign.error) return <ErrorState error={campaign.error} onRetry={() => campaign.refetch()} />;
  if (!campaign.data) return <LoadingRows rows={8} />;
  const c = campaign.data;
  const r = report.data;
  const outcomeData = r ? Object.entries(r.outcomes).map(([k, v]) => ({ name: outcomeLabel(k === "unclassified" ? null : k), value: v })) : [];

  return (
    <>
      <PageHeader
        title={c.name}
        description={
          <span className="inline-flex flex-wrap items-center gap-x-3 gap-y-1">
            <ModeBadge mode={c.mode} />
            <StatusText status={c.status} />
            {c.agent_name ? (
              <Link href={`/w/${ws}/agents/${c.agent_id}`} className="underline underline-offset-4">
                {c.agent_name}
              </Link>
            ) : null}
          </span>
        }
        actions={
          canEdit ? (
            <>
              {["draft", "scheduled"].includes(c.status) ? (
                <Button onClick={() => action.mutate("start")} disabled={action.isPending}>
                  <Play /> Start
                </Button>
              ) : null}
              {c.status === "paused" ? (
                <Button onClick={() => action.mutate("resume")} disabled={action.isPending}>
                  <Play /> Resume
                </Button>
              ) : null}
              {running ? (
                <Button variant="outline" onClick={() => action.mutate("pause")} disabled={action.isPending}>
                  <Pause /> Pause
                </Button>
              ) : null}
              {!["completed", "cancelled"].includes(c.status) ? (
                <ConfirmButton
                  title="Cancel this campaign?"
                  description="Waiting contacts won’t be called. Calls already made stay in the history."
                  confirmLabel="Cancel campaign"
                  size="default"
                  onConfirm={() => action.mutateAsync("cancel")}
                >
                  <Ban /> Cancel
                </ConfirmButton>
              ) : null}
              {!["running", "scheduled"].includes(c.status) ? (
                <ConfirmButton
                  title="Delete this campaign?"
                  description="The campaign and its contact list are removed. Call records are kept."
                  confirmLabel="Delete"
                  size="default"
                  variant="ghost"
                  onConfirm={async () => {
                    await del(wsApi(`/campaigns/${id}`));
                    toast.success("Campaign deleted");
                    router.push(`/w/${ws}/campaigns`);
                  }}
                >
                  <Trash2 />
                </ConfirmButton>
              ) : null}
            </>
          ) : null
        }
      />
      {c.status_reason ? <p className="mb-5 rounded-lg border bg-panel px-4 py-2.5 text-sm">{c.status_reason}</p> : null}
      <div className="grid gap-5">
        <section className="grid gap-5 rounded-xl border bg-panel p-5 lg:grid-cols-[minmax(0,1.2fr)_repeat(4,minmax(0,1fr))]">
          <div>
            <p className="mb-2 text-xs text-muted-foreground">Progress</p>
            <ProgressBar progress={c.progress} />
          </div>
          <Stat label="Calls placed" value={r?.calls ?? "—"} sub={c.mode === "SIMULATION" ? "simulated" : undefined} />
          <Stat label="Answered" value={r ? formatPercent(r.connect_rate) : "—"} />
          <Stat label="Positive outcomes" value={r?.positive_outcomes ?? "—"} sub={r ? `${formatPercent(r.conversion_rate)} of answered` : undefined} />
          <Stat label="Average length" value={r ? formatDuration(r.avg_duration_seconds) : "—"} />
        </section>

        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <Panel title="Schedule">
            <dl className="grid grid-cols-[9rem_1fr] gap-y-2 text-sm">
              <dt className="text-muted-foreground">Calling hours</dt>
              <dd>
                {c.window?.start ?? c.window_start}–{c.window?.end ?? c.window_end} ({c.timezone})
              </dd>
              <dt className="text-muted-foreground">Days</dt>
              <dd>{(c.window?.days ?? c.calling_days).map((d) => WEEKDAYS[d]).join(", ") || "None"}</dd>
              <dt className="text-muted-foreground">Right now</dt>
              <dd>
                {c.window?.ignored ? (
                  "Calling hours ignored (simulation)"
                ) : c.window?.open_now ? (
                  <StatusLamp state="ok" label="Inside calling hours" />
                ) : (
                  <span>Outside calling hours{c.window?.next_open ? `, next window ${formatDateTime(c.window.next_open)}` : ""}</span>
                )}
              </dd>
              <dt className="text-muted-foreground">Attempts</dt>
              <dd>
                Up to {c.max_attempts}, {c.retry_delay_minutes} min apart
              </dd>
              <dt className="text-muted-foreground">Concurrency</dt>
              <dd>{c.max_concurrent_calls} at a time</dd>
              <dt className="text-muted-foreground">Purpose</dt>
              <dd>{humanize(c.purpose)}</dd>
            </dl>
            {c.objective ? <p className="mt-4 border-t pt-4 text-sm">{c.objective}</p> : null}
          </Panel>
          <Panel
            title="Compliance check"
            description="Who could be called right now, and what blocks the rest."
            actions={
              <Button variant="outline" size="sm" onClick={() => preview.mutate()} disabled={preview.isPending}>
                <ShieldCheck /> Check now
              </Button>
            }
          >
            {preview.data ? (
              <div className="text-sm">
                <p>
                  <span className="font-semibold tabular-nums">{preview.data.eligible_now}</span> of {preview.data.checked} contacts are eligible now.
                </p>
                {Object.keys(preview.data.blocked_by).length ? (
                  <ul className="mt-3 grid gap-1">
                    {Object.entries(preview.data.blocked_by).map(([k, v]) => (
                      <li key={k} className="flex justify-between">
                        <span className="text-muted-foreground">{humanize(k)}</span>
                        <span className="tabular-nums">{v}</span>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">Runs consent, opt-out, DND, calling-hour and frequency checks against every contact in this campaign.</p>
            )}
          </Panel>
        </div>

        {outcomeData.length ? (
          <Panel title="Outcomes">
            <BarsChart layout="vertical" label="Calls by outcome" height={Math.max(150, outcomeData.length * 30 + 30)} data={outcomeData} />
          </Panel>
        ) : null}

        <Panel
          title="Contacts in this campaign"
          actions={
            <NativeSelect value={ccStatus} onChange={(e) => (setCcStatus(e.target.value), setCcPage(1))} className="h-7 w-36 text-xs" aria-label="Filter">
              <option value="">All</option>
              {["pending", "queued", "in_progress", "completed", "exhausted", "skipped", "failed", "cancelled"].map((s) => (
                <option key={s} value={s}>
                  {humanize(s)}
                </option>
              ))}
            </NativeSelect>
          }
          bodyClassName="p-0"
        >
          {!contacts.data ? (
            <div className="p-5">
              <LoadingRows />
            </div>
          ) : (
            <>
              <ul className="divide-y text-sm">
                {contacts.data.items.map((cc) => (
                  <li key={cc.id} className="grid gap-1 px-5 py-3 md:grid-cols-[minmax(0,1fr)_8rem_7rem_minmax(0,1.3fr)] md:items-center md:gap-4">
                    <Link href={`/w/${ws}/contacts/${cc.contact_id}`} className="truncate font-medium hover:underline">
                      {cc.name || formatPhone(cc.phone_e164)}
                    </Link>
                    <StatusText status={cc.status} />
                    <span className="text-xs text-muted-foreground">
                      {cc.attempts} attempt{cc.attempts === 1 ? "" : "s"}
                    </span>
                    <span className="min-w-0 truncate text-xs text-muted-foreground" title={cc.skip_reason}>
                      {cc.last_call_id ? (
                        <Link href={`/w/${ws}/calls/${cc.last_call_id}`}>
                          <OutcomeBadge outcome={cc.last_outcome} />
                        </Link>
                      ) : null}{" "}
                      {cc.skip_reason || (cc.next_attempt_at && cc.status === "pending" ? `Next try ${formatDateTime(cc.next_attempt_at)}` : "")}
                    </span>
                  </li>
                ))}
              </ul>
              <div className="px-5 pb-4">
                <Pagination page={ccPage} pageSize={50} total={contacts.data.total} onPage={setCcPage} />
              </div>
            </>
          )}
        </Panel>
      </div>
    </>
  );
}
