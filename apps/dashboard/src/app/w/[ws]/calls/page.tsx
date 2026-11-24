"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Phone, Search } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import {
  EmptyState,
  ErrorState,
  LoadingRows,
  ModeBadge,
  NativeSelect,
  OutcomeBadge,
  PageHeader,
  Pagination,
  StatusText,
} from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { download, errorMessage, get, patch } from "@/lib/api";
import { formatDateTime, formatDuration, formatPhone, humanize, OUTCOME_LABELS } from "@/lib/format";
import { useCan, useWorkspace, useWorkspaceId, useWsApi } from "@/lib/hooks";
import type { Call, Page } from "@/lib/types";
import { cn } from "@/lib/utils";

export default function CallsPage() {
  return (
    <>
      <PageHeader
        title="Calls"
        description="Every conversation: browser calls, simulations and phone calls. Simulations are hatched; they never reached a real person."
      />
      <Tabs defaultValue="calls">
        <TabsList className="mb-5">
          <TabsTrigger value="calls">Conversations</TabsTrigger>
          <TabsTrigger value="callbacks">Callback requests</TabsTrigger>
        </TabsList>
        <TabsContent value="calls">
          <CallList />
        </TabsContent>
        <TabsContent value="callbacks">
          <Callbacks />
        </TabsContent>
      </Tabs>
    </>
  );
}

function CallList() {
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const router = useRouter();
  const workspace = useWorkspace();
  const canExport = useCan("admin");
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [mode, setMode] = useState("");
  const [outcome, setOutcome] = useState("");
  const [page, setPage] = useState(1);
  useEffect(() => {
    const t = setTimeout(() => (setDebounced(q), setPage(1)), 300);
    return () => clearTimeout(t);
  }, [q]);
  const calls = useQuery({
    queryKey: ["calls", ws, debounced, mode, outcome, page],
    queryFn: () => get<Page<Call>>(wsApi("/calls"), { q: debounced, mode, outcome, page, page_size: 25 }),
    placeholderData: keepPreviousData,
    refetchInterval: (query) => (query.state.data?.items.some((c) => ["dialing", "in_progress", "connecting", "ringing"].includes(c.status)) ? 3000 : false),
  });
  const items = calls.data?.items ?? [];
  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="relative w-full max-w-xs">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search transcripts and summaries" className="pl-8" aria-label="Search calls" />
        </div>
        <NativeSelect value={mode} onChange={(e) => (setMode(e.target.value), setPage(1))} className="w-40" aria-label="Mode filter">
          <option value="">All modes</option>
          <option value="WEBRTC">Browser</option>
          <option value="SIMULATION">Simulation</option>
          <option value="PSTN">Phone</option>
        </NativeSelect>
        <NativeSelect value={outcome} onChange={(e) => (setOutcome(e.target.value), setPage(1))} className="w-48" aria-label="Outcome filter">
          <option value="">All outcomes</option>
          {Object.entries(OUTCOME_LABELS).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </NativeSelect>
        {canExport ? (
          <Button
            variant="outline"
            className="ml-auto"
            onClick={() =>
              download(wsApi("/calls/export"), `${workspace.data?.slug ?? "calls"}-calls.csv`, { mode, outcome, include_transcripts: true }).catch((e) =>
                toast.error(errorMessage(e)),
              )
            }
          >
            <Download /> Export with transcripts
          </Button>
        ) : null}
      </div>
      <div className="rounded-xl border bg-panel">
        {calls.error ? (
          <div className="p-5">
            <ErrorState error={calls.error} onRetry={() => calls.refetch()} />
          </div>
        ) : !calls.data ? (
          <div className="p-5">
            <LoadingRows rows={8} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState icon={<Phone className="size-6" />} title={debounced || mode || outcome ? "No calls match" : "No calls yet"}>
            {debounced || mode || outcome ? "Try a different search or filter." : "Talk to an agent in the playground or run a simulation."}
          </EmptyState>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="pl-5">When</TableHead>
                <TableHead>Mode</TableHead>
                <TableHead>Agent</TableHead>
                <TableHead className="hidden md:table-cell">Contact</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Outcome</TableHead>
                <TableHead className="text-right">Length</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((c) => (
                <TableRow key={c.id} className={cn("cursor-pointer", c.is_simulated && "[&>td:first-child]:sim-hatch")} onClick={() => router.push(`/w/${ws}/calls/${c.id}`)}>
                  <TableCell className="pl-5 whitespace-nowrap">
                    <Link href={`/w/${ws}/calls/${c.id}`} onClick={(e) => e.stopPropagation()} className="hover:underline">
                      {formatDateTime(c.started_at)}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <ModeBadge mode={c.mode} />
                  </TableCell>
                  <TableCell className="max-w-40 truncate">{c.agent_name ?? "—"}</TableCell>
                  <TableCell className="hidden max-w-48 truncate md:table-cell">
                    {c.contact_name ?? (c.direction === "inbound" ? formatPhone(c.from_number) : formatPhone(c.to_number))}
                  </TableCell>
                  <TableCell>
                    <StatusText status={c.status} />
                  </TableCell>
                  <TableCell>
                    <OutcomeBadge outcome={c.outcome} />
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{formatDuration(c.duration_seconds)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
      {calls.data ? <Pagination page={page} pageSize={25} total={calls.data.total} onPage={setPage} /> : null}
    </>
  );
}

interface Callback {
  id: string;
  kind: string;
  preferred_time: string;
  notes: string;
  status: string;
  call_id: string | null;
  contact_id: string | null;
  contact_name: string | null;
  contact_phone: string | null;
  is_simulated: boolean;
  created_at: string;
}

function Callbacks() {
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const qc = useQueryClient();
  const canEdit = useCan("editor");
  const [status, setStatus] = useState("open");
  const [showSim, setShowSim] = useState(false);
  const list = useQuery({
    queryKey: ["callbacks", ws, status, showSim],
    queryFn: () => get<Page<Callback>>(wsApi("/callbacks"), { status, include_simulated: showSim }),
  });
  const update = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) => patch(wsApi(`/callbacks/${id}`), { status }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["callbacks", ws] }),
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <NativeSelect value={status} onChange={(e) => setStatus(e.target.value)} className="w-40" aria-label="Status">
          <option value="open">Open</option>
          <option value="scheduled">Scheduled</option>
          <option value="done">Done</option>
          <option value="cancelled">Cancelled</option>
          <option value="">All</option>
        </NativeSelect>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={showSim} onChange={(e) => setShowSim(e.target.checked)} className="accent-[var(--signal)]" />
          Include requests from simulations
        </label>
      </div>
      <div className="rounded-xl border bg-panel">
        {list.error ? (
          <div className="p-5">
            <ErrorState error={list.error} />
          </div>
        ) : !list.data ? (
          <div className="p-5">
            <LoadingRows />
          </div>
        ) : list.data.items.length === 0 ? (
          <EmptyState title="No callback requests">When a caller asks to be called back, the agent records a request here for your team.</EmptyState>
        ) : (
          <ul className="divide-y">
            {list.data.items.map((cb) => (
              <li key={cb.id} className={cn("flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-3.5 text-sm", cb.is_simulated && "sim-hatch")}>
                <div className="min-w-0 flex-1">
                  <p className="font-medium">
                    {cb.contact_name || formatPhone(cb.contact_phone) || "Unknown caller"}{" "}
                    <span className="font-normal text-muted-foreground">wants {cb.kind === "appointment" ? "an appointment" : "a callback"}: {cb.preferred_time}</span>
                  </p>
                  {cb.notes ? <p className="mt-0.5 text-xs text-muted-foreground">{cb.notes}</p> : null}
                </div>
                {cb.call_id ? (
                  <Link href={`/w/${ws}/calls/${cb.call_id}`} className="text-xs underline underline-offset-4">
                    Call
                  </Link>
                ) : null}
                <span className="text-xs text-muted-foreground">{formatDateTime(cb.created_at)}</span>
                {canEdit ? (
                  <NativeSelect
                    value={cb.status}
                    onChange={(e) => update.mutate({ id: cb.id, status: e.target.value })}
                    className="h-7 w-32 text-xs"
                    aria-label="Callback status"
                  >
                    {["open", "scheduled", "done", "cancelled"].map((s) => (
                      <option key={s} value={s}>
                        {humanize(s)}
                      </option>
                    ))}
                  </NativeSelect>
                ) : (
                  <span className="text-xs">{humanize(cb.status)}</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </>
  );
}
