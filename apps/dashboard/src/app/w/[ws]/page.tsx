"use client";

import { useQuery } from "@tanstack/react-query";
import { AudioLines, Megaphone, Phone } from "lucide-react";
import Link from "next/link";

import {
  EmptyState,
  ErrorState,
  LoadingRows,
  ModeBadge,
  OutcomeBadge,
  PageHeader,
  Panel,
  Stat,
  StatusLamp,
} from "@/components/common";
import { buttonVariants } from "@/components/ui/button";
import { get } from "@/lib/api";
import { formatDuration, formatNumber, formatRelative } from "@/lib/format";
import { useWorkspace, useWsApi } from "@/lib/hooks";
import type { SystemHealth } from "@/lib/types";
import { SystemStrip } from "@/components/system-strip";

interface Overview {
  total_calls: number;
  successful_calls: number;
  failed_calls: number;
  avg_duration_seconds: number;
  total_duration_seconds: number;
  calls_by_mode: Record<string, number>;
  agents_active: number;
  agents_total: number;
  campaigns: Record<string, number>;
  contacts: number;
  leads: number;
  open_callbacks: number;
  live_sessions: number;
  recent_calls: {
    id: string;
    mode: string;
    status: string;
    outcome: string | null;
    agent_name: string | null;
    started_at: string;
    duration_seconds: number | null;
    channel: string;
  }[];
}

export default function OverviewPage() {
  const wsApi = useWsApi();
  const ws = useWorkspace();
  const overview = useQuery({
    queryKey: ["overview", wsApi()],
    queryFn: () => get<Overview>(wsApi("/analytics/overview")),
    refetchInterval: 15_000,
  });
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => get<SystemHealth>("/api/system/health"),
    refetchInterval: 20_000,
  });
  const base = `/w/${ws.data?.id ?? ""}`;
  const o = overview.data;

  return (
    <>
      <PageHeader
        title={ws.data?.name ?? "Overview"}
        description="Calls, agents and the health of the local AI stack, from this workspace’s records."
        actions={
          <Link href={`${base}/playground`} className={buttonVariants()}>
            <AudioLines /> Talk to an agent
          </Link>
        }
      />

      <div className="grid gap-5">
        <Panel
          title="System"
          description={
            health.data
              ? `Memory ${health.data.resources.memory_used_percent}% of ${health.data.resources.memory_total_gb} GB used, ${health.data.resources.disk_free_gb} GB disk free, ${health.data.voice_sessions.active} of ${health.data.voice_sessions.max} voice sessions in use`
              : undefined
          }
        >
          {health.error ? (
            <ErrorState error={health.error} onRetry={() => health.refetch()} />
          ) : health.data ? (
            <SystemStrip health={health.data} />
          ) : (
            <LoadingRows rows={2} />
          )}
        </Panel>

        {overview.error ? (
          <ErrorState error={overview.error} onRetry={() => overview.refetch()} />
        ) : !o ? (
          <LoadingRows rows={3} />
        ) : (
          <>
            <section
              aria-label="Key figures"
              className="grid grid-cols-2 divide-y rounded-xl border bg-panel sm:grid-cols-4 sm:divide-y-0 lg:grid-cols-8 lg:divide-x [&>*]:px-5 [&>*]:py-4"
            >
              <Stat label="Conversations" value={formatNumber(o.total_calls)} sub={modeSplit(o.calls_by_mode)} />
              <Stat label="Completed" value={formatNumber(o.successful_calls)} />
              <Stat label="Failed" value={formatNumber(o.failed_calls)} />
              <Stat label="Average length" value={formatDuration(o.avg_duration_seconds)} />
              <Stat label="Talk time" value={formatDuration(o.total_duration_seconds)} />
              <Stat label="Active agents" value={`${o.agents_active}/${o.agents_total}`} />
              <Stat label="Leads" value={formatNumber(o.leads)} sub={`${formatNumber(o.contacts)} contacts`} />
              <Stat label="Open callbacks" value={formatNumber(o.open_callbacks)} />
            </section>

            <div className="grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
              <Panel
                title="Recent conversations"
                actions={
                  <Link href={`${base}/calls`} className={buttonVariants({ variant: "ghost", size: "sm" })}>
                    All calls
                  </Link>
                }
                bodyClassName="p-0"
              >
                {o.recent_calls.length === 0 ? (
                  <EmptyState icon={<Phone className="size-6" />} title="No conversations yet">
                    Start one in the voice playground, or run a simulated call from an agent.
                  </EmptyState>
                ) : (
                  <ul className="divide-y">
                    {o.recent_calls.map((c) => (
                      <li key={c.id}>
                        <Link
                          href={`${base}/calls/${c.id}`}
                          className="flex items-center gap-3 px-5 py-3 text-sm transition-colors hover:bg-muted/50"
                        >
                          <ModeBadge mode={c.mode} />
                          <span className="min-w-0 flex-1 truncate">{c.agent_name ?? "Deleted agent"}</span>
                          <OutcomeBadge outcome={c.outcome} />
                          <span className="w-14 text-right text-muted-foreground tabular-nums">
                            {formatDuration(c.duration_seconds)}
                          </span>
                          <span className="hidden w-24 text-right text-muted-foreground sm:block">
                            {formatRelative(c.started_at)}
                          </span>
                        </Link>
                      </li>
                    ))}
                  </ul>
                )}
              </Panel>

              <Panel title="Campaign activity">
                {Object.keys(o.campaigns).length === 0 ? (
                  <EmptyState icon={<Megaphone className="size-6" />} title="No campaigns">
                    Campaigns call a list of contacts. They run as simulations unless real telephony is set up.
                  </EmptyState>
                ) : (
                  <dl className="grid gap-2.5 text-sm">
                    {["running", "scheduled", "paused", "draft", "completed", "cancelled"]
                      .filter((s) => o.campaigns[s])
                      .map((s) => (
                        <div key={s} className="flex items-center justify-between">
                          <dt>
                            <StatusLamp
                              state={s === "running" ? "live" : s === "paused" ? "degraded" : s === "completed" ? "ok" : "idle"}
                              label={s.charAt(0).toUpperCase() + s.slice(1)}
                            />
                          </dt>
                          <dd className="font-medium tabular-nums">{o.campaigns[s]}</dd>
                        </div>
                      ))}
                  </dl>
                )}
              </Panel>
            </div>
          </>
        )}
      </div>
    </>
  );
}

function modeSplit(m: Record<string, number>): string | undefined {
  const parts = [
    m.WEBRTC ? `${m.WEBRTC} browser` : null,
    m.PSTN ? `${m.PSTN} phone` : null,
    m.SIMULATION ? `${m.SIMULATION} simulated` : null,
  ].filter(Boolean);
  return parts.length ? parts.join(", ") : undefined;
}
