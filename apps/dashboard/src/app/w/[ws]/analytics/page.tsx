"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { BarChart3 } from "lucide-react";
import { useState } from "react";

import { BarsChart, Legend, TrendChart } from "@/components/charts";
import { EmptyState, ErrorState, LoadingRows, ModeBadge, NativeSelect, PageHeader, Panel, Stat } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { get } from "@/lib/api";
import { formatDuration, formatNumber, formatPercent, outcomeLabel } from "@/lib/format";
import { useWorkspaceId, useWsApi } from "@/lib/hooks";
import type { Agent, Campaign } from "@/lib/types";

interface Report {
  range: { from: string; to: string };
  totals: { calls: number; completed: number; failed: number; positive: number; success_rate: number; conversion_rate: number; callback_requests: number; leads_captured: number };
  calls_over_time: { date: string; calls: number; completed: number; failed: number; positive: number }[];
  outcomes: { outcome: string; count: number }[];
  duration_distribution: { bucket: string; count: number }[];
  agents: { id: string; name: string; calls: number; positive: number; avg_duration_seconds: number; failed: number; conversion_rate: number }[];
  campaigns: { id: string; name: string; mode: string; calls: number; connected: number; positive: number }[];
}

/** Fill missing days so the trend line does not skip quiet days. */
function fillDays(rows: Report["calls_over_time"], from: string, to: string) {
  const byDate = new Map(rows.map((r) => [r.date, r]));
  const out: Record<string, string | number>[] = [];
  const start = new Date(from);
  const end = new Date(to);
  for (let d = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), start.getUTCDate())); d <= end; d.setUTCDate(d.getUTCDate() + 1)) {
    const key = d.toISOString().slice(0, 10);
    const r = byDate.get(key);
    out.push({
      label: d.toLocaleDateString(undefined, { day: "numeric", month: "short" }),
      date: key,
      calls: r?.calls ?? 0,
      positive: r?.positive ?? 0,
      failed: r?.failed ?? 0,
    });
  }
  return out;
}

export default function AnalyticsPage() {
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const [days, setDays] = useState(30);
  const [mode, setMode] = useState("");
  const [agentId, setAgentId] = useState("");
  const [campaignId, setCampaignId] = useState("");
  const [asTable, setAsTable] = useState(false);
  const agents = useQuery({ queryKey: ["agents", ws], queryFn: () => get<Agent[]>(wsApi("/agents")) });
  const campaigns = useQuery({ queryKey: ["campaigns", ws], queryFn: () => get<Campaign[]>(wsApi("/campaigns")) });
  const report = useQuery({
    queryKey: ["analytics", ws, days, mode, agentId, campaignId],
    queryFn: () => get<Report>(wsApi("/analytics/report"), { days, mode, agent_id: agentId, campaign_id: campaignId }),
    placeholderData: keepPreviousData,
  });
  const r = report.data;
  const trend = r ? fillDays(r.calls_over_time, r.range.from, r.range.to) : [];
  const series = [
    { key: "calls", label: "All calls", color: "var(--chart-1)" },
    { key: "positive", label: "Positive outcomes", color: "var(--chart-2)" },
  ];

  return (
    <>
      <PageHeader title="Analytics" description="Computed from this workspace’s call records. Use the mode filter to separate simulations from real conversations." />
      <div className="mb-5 flex flex-wrap items-center gap-3" role="group" aria-label="Filters">
        <div className="inline-flex rounded-lg border p-0.5" role="radiogroup" aria-label="Date range">
          {[7, 30, 90].map((d) => (
            <button
              key={d}
              role="radio"
              aria-checked={days === d}
              onClick={() => setDays(d)}
              className={`h-7 rounded-md px-3 text-sm ${days === d ? "bg-foreground text-background" : "text-muted-foreground hover:text-foreground"}`}
            >
              {d} days
            </button>
          ))}
        </div>
        <NativeSelect value={mode} onChange={(e) => setMode(e.target.value)} className="w-40" aria-label="Mode">
          <option value="">All modes</option>
          <option value="WEBRTC">Browser</option>
          <option value="SIMULATION">Simulation</option>
          <option value="PSTN">Phone</option>
        </NativeSelect>
        <NativeSelect value={agentId} onChange={(e) => setAgentId(e.target.value)} className="w-44" aria-label="Agent">
          <option value="">All agents</option>
          {agents.data?.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </NativeSelect>
        <NativeSelect value={campaignId} onChange={(e) => setCampaignId(e.target.value)} className="w-48" aria-label="Campaign">
          <option value="">All campaigns</option>
          {campaigns.data?.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </NativeSelect>
      </div>

      {report.error ? (
        <ErrorState error={report.error} onRetry={() => report.refetch()} />
      ) : !r ? (
        <LoadingRows rows={8} />
      ) : r.totals.calls === 0 ? (
        <div className="rounded-xl border bg-panel">
          <EmptyState icon={<BarChart3 className="size-6" />} title="No calls in this range">
            Analytics fill in as conversations happen. Try a longer range or remove filters.
          </EmptyState>
        </div>
      ) : (
        <div className="grid gap-5">
          <section className="grid grid-cols-2 divide-y rounded-xl border bg-panel sm:grid-cols-3 lg:grid-cols-6 lg:divide-x lg:divide-y-0 [&>*]:px-5 [&>*]:py-4">
            <Stat label="Calls" value={formatNumber(r.totals.calls)} />
            <Stat label="Completed" value={formatPercent(r.totals.success_rate)} sub={`${r.totals.completed} calls`} />
            <Stat label="Failed" value={formatNumber(r.totals.failed)} />
            <Stat label="Positive outcome rate" value={formatPercent(r.totals.conversion_rate)} sub={`${r.totals.positive} of completed`} />
            <Stat label="Callback requests" value={formatNumber(r.totals.callback_requests)} />
            <Stat label="Leads captured" value={formatNumber(r.totals.leads_captured)} sub="from conversations" />
          </section>

          <Panel
            title="Calls over time"
            description="Positive means interested, callback or appointment requested."
            actions={
              <Button variant="ghost" size="sm" onClick={() => setAsTable((v) => !v)} aria-pressed={asTable}>
                {asTable ? "Show chart" : "Show table"}
              </Button>
            }
          >
            {asTable ? (
              <div className="max-h-72 overflow-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Day</TableHead>
                      <TableHead className="text-right">Calls</TableHead>
                      <TableHead className="text-right">Positive</TableHead>
                      <TableHead className="text-right">Failed</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {trend.filter((d) => Number(d.calls) > 0).map((d) => (
                      <TableRow key={String(d.date)}>
                        <TableCell>{d.label}</TableCell>
                        <TableCell className="text-right tabular-nums">{d.calls}</TableCell>
                        <TableCell className="text-right tabular-nums">{d.positive}</TableCell>
                        <TableCell className="text-right tabular-nums">{d.failed}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ) : (
              <>
                <Legend items={series.map((s) => ({ label: s.label, color: s.color, value: s.key === "calls" ? r.totals.calls : r.totals.positive }))} />
                <TrendChart data={trend} series={series} />
              </>
            )}
          </Panel>

          <div className="grid gap-5 lg:grid-cols-2">
            <Panel title="Outcomes">
              <BarsChart
                layout="vertical"
                label="Calls by outcome"
                height={Math.max(160, r.outcomes.length * 30 + 30)}
                data={[...r.outcomes]
                  .sort((a, b) => b.count - a.count)
                  .map((o) => ({ name: outcomeLabel(o.outcome === "unclassified" ? null : o.outcome), value: o.count }))}
              />
            </Panel>
            <Panel title="Call length" description={r.totals.calls && mode !== "WEBRTC" ? "Simulated call lengths are estimates." : undefined}>
              <BarsChart label="Calls by length" color="var(--chart-3)" data={r.duration_distribution.map((d) => ({ name: d.bucket, value: d.count }))} />
            </Panel>
          </div>

          <Panel title="Agents" bodyClassName="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="pl-5">Agent</TableHead>
                  <TableHead className="text-right">Calls</TableHead>
                  <TableHead className="text-right">Positive</TableHead>
                  <TableHead className="text-right">Positive rate</TableHead>
                  <TableHead className="text-right">Failed</TableHead>
                  <TableHead className="pr-5 text-right">Average length</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {r.agents.map((a) => (
                  <TableRow key={a.id}>
                    <TableCell className="pl-5 font-medium">{a.name}</TableCell>
                    <TableCell className="text-right tabular-nums">{a.calls}</TableCell>
                    <TableCell className="text-right tabular-nums">{a.positive}</TableCell>
                    <TableCell className="text-right tabular-nums">{formatPercent(a.conversion_rate)}</TableCell>
                    <TableCell className="text-right tabular-nums">{a.failed}</TableCell>
                    <TableCell className="pr-5 text-right tabular-nums">{formatDuration(a.avg_duration_seconds)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Panel>

          {r.campaigns.length ? (
            <Panel title="Campaigns" bodyClassName="p-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="pl-5">Campaign</TableHead>
                    <TableHead>Mode</TableHead>
                    <TableHead className="text-right">Calls</TableHead>
                    <TableHead className="text-right">Answered</TableHead>
                    <TableHead className="pr-5 text-right">Positive</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {r.campaigns.map((c) => (
                    <TableRow key={c.id}>
                      <TableCell className="pl-5 font-medium">{c.name}</TableCell>
                      <TableCell>
                        <ModeBadge mode={c.mode} />
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{c.calls}</TableCell>
                      <TableCell className="text-right tabular-nums">
                        {c.connected} <span className="text-muted-foreground">({formatPercent(c.calls ? c.connected / c.calls : 0)})</span>
                      </TableCell>
                      <TableCell className="pr-5 text-right tabular-nums">{c.positive}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Panel>
          ) : null}
        </div>
      )}
    </>
  );
}
