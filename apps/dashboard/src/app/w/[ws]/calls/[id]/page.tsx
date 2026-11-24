"use client";

import { useQuery } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import {
  ConfirmButton,
  EmptyState,
  ErrorState,
  LoadingRows,
  ModeBadge,
  OutcomeBadge,
  PageHeader,
  Panel,
  StatusText,
} from "@/components/common";
import { api, del, errorMessage, get } from "@/lib/api";
import { formatDateTime, formatDuration, formatPhone, humanize } from "@/lib/format";
import { useCan, useWorkspaceId, useWsApi } from "@/lib/hooks";
import type { CallDetail } from "@/lib/types";
import { cn } from "@/lib/utils";

const LIVE = ["queued", "dialing", "ringing", "connecting", "in_progress"];

export default function CallDetailPage() {
  const { id } = useParams<{ id: string }>();
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const router = useRouter();
  const canAdmin = useCan("admin");
  const canEdit = useCan("editor");
  const call = useQuery({
    queryKey: ["call", id],
    queryFn: () => get<CallDetail>(wsApi(`/calls/${id}`)),
    refetchInterval: (q) => (q.state.data && LIVE.includes(q.state.data.status) ? 1500 : false),
  });

  if (call.error) return <ErrorState error={call.error} onRetry={() => call.refetch()} />;
  if (!call.data) return <LoadingRows rows={8} />;
  const c = call.data;
  const live = LIVE.includes(c.status);
  const turns = (c.metrics.turns as Record<string, number>[] | undefined) ?? [];

  return (
    <>
      <PageHeader
        title={c.contact_name ?? (c.direction === "inbound" ? "Inbound conversation" : "Outbound call")}
        description={
          <span className="inline-flex flex-wrap items-center gap-x-3 gap-y-1">
            <ModeBadge mode={c.mode} />
            <span>{humanize(c.direction)}</span>
            <span>{formatDateTime(c.started_at)}</span>
            {c.agent_name ? (
              <Link href={`/w/${ws}/agents/${c.agent_id}`} className="underline underline-offset-4">
                {c.agent_name}
              </Link>
            ) : null}
          </span>
        }
        actions={
          canAdmin ? (
            <ConfirmButton
              title="Delete this call record?"
              description="The transcript and any recording are removed permanently."
              confirmLabel="Delete record"
              size="default"
              onConfirm={async () => {
                try {
                  await del(wsApi(`/calls/${c.id}`));
                  toast.success("Call record deleted");
                  router.push(`/w/${ws}/calls`);
                } catch (e) {
                  toast.error(errorMessage(e));
                }
              }}
            >
              <Trash2 /> Delete
            </ConfirmButton>
          ) : null
        }
      />
      {c.mode === "SIMULATION" ? (
        <p className="sim-hatch mb-5 rounded-lg border border-dashed px-4 py-2.5 text-sm">
          Simulation: no phone call was made. The other side was a simulated person
          {c.simulation_persona ? ` (${humanize(c.simulation_persona)})` : ""}.
        </p>
      ) : null}
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
        <Panel title="Transcript" description={live ? "Updating while the call is in progress" : undefined} bodyClassName="p-0">
          {c.messages.length === 0 ? (
            <EmptyState title={live ? "Waiting for the conversation" : "No transcript"}>
              {live ? "Lines appear when the call ends." : c.error_detail ?? "This call ended before anyone spoke."}
            </EmptyState>
          ) : (
            <ol className="grid gap-3.5 px-5 py-4">
              {c.messages.map((m) =>
                m.role === "tool" ? (
                  <li key={m.seq} className="ml-[4.25rem] border-l-2 border-signal/60 pl-3 text-xs text-muted-foreground">
                    {m.content}
                  </li>
                ) : (
                  <li key={m.seq} className="grid grid-cols-[3.5rem_minmax(0,1fr)] gap-3 text-sm">
                    <span className={cn("pt-px text-xs", m.role === "assistant" ? "text-signal" : "text-muted-foreground")}>
                      {m.role === "assistant" ? "Agent" : c.mode === "SIMULATION" ? "Simulated" : "Caller"}
                    </span>
                    <p className="leading-relaxed">
                      {m.content}
                      {m.interrupted ? <span className="ml-1.5 text-xs text-muted-foreground">(interrupted)</span> : null}
                    </p>
                  </li>
                ),
              )}
            </ol>
          )}
        </Panel>
        <div className="grid content-start gap-5">
          <Panel title="Summary">
            <div className="mb-3 flex flex-wrap items-center gap-3">
              <StatusText status={c.status} />
              <OutcomeBadge outcome={c.outcome} />
              {c.sentiment ? <span className="text-xs text-muted-foreground">Sentiment: {c.sentiment}</span> : null}
            </div>
            <p className="text-sm leading-relaxed">
              {c.summary ?? <span className="text-muted-foreground">{live ? "Generated when the call ends." : "No summary."}</span>}
            </p>
            {c.analysis_error ? <p className="mt-2 text-xs text-warn">{c.analysis_error}</p> : null}
            {c.error_detail ? (
              <p className="mt-3 rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">
                {c.error_code ? `${humanize(c.error_code)}: ` : ""}
                {c.error_detail}
              </p>
            ) : null}
            <dl className="mt-4 grid grid-cols-2 gap-y-2 border-t pt-4 text-sm">
              <dt className="text-muted-foreground">Length</dt>
              <dd className="tabular-nums">
                {formatDuration(c.duration_seconds)}
                {c.mode === "SIMULATION" ? <span className="text-xs text-muted-foreground"> (estimated)</span> : null}
              </dd>
              <dt className="text-muted-foreground">Ended because</dt>
              <dd>{c.end_reason ? humanize(c.end_reason) : "—"}</dd>
              <dt className="text-muted-foreground">Number</dt>
              <dd className="font-mono text-xs leading-5">{formatPhone(c.direction === "inbound" ? c.from_number : c.to_number)}</dd>
              <dt className="text-muted-foreground">Model</dt>
              <dd className="truncate font-mono text-xs leading-5">{c.llm_model ?? "—"}</dd>
              {c.campaign_name ? (
                <>
                  <dt className="text-muted-foreground">Campaign</dt>
                  <dd>
                    <Link href={`/w/${ws}/campaigns/${c.campaign_id}`} className="underline underline-offset-4">
                      {c.campaign_name}
                    </Link>
                  </dd>
                </>
              ) : null}
              {c.contact_id ? (
                <>
                  <dt className="text-muted-foreground">Contact</dt>
                  <dd>
                    <Link href={`/w/${ws}/contacts/${c.contact_id}`} className="underline underline-offset-4">
                      {c.contact_name ?? "Open contact"}
                    </Link>
                  </dd>
                </>
              ) : null}
            </dl>
          </Panel>
          {c.recording ? <RecordingPanel callId={c.id} canPlay={canEdit} duration={c.recording.duration_seconds} /> : null}
          {c.callbacks.length ? (
            <Panel title="Requests from this call" bodyClassName="p-0">
              <ul className="divide-y text-sm">
                {c.callbacks.map((cb) => (
                  <li key={cb.id} className="px-5 py-3">
                    <span className="font-medium">{humanize(cb.kind)}</span>: {cb.preferred_time}
                    <span className="ml-2 text-xs text-muted-foreground">{humanize(cb.status)}</span>
                  </li>
                ))}
              </ul>
            </Panel>
          ) : null}
          {turns.length ? (
            <Panel title="Response times" description="Per agent turn: speech-to-text, first model token, first sentence voiced, and caller pause to first agent audio.">
              <table className="w-full text-xs tabular-nums">
                <thead className="text-muted-foreground">
                  <tr>
                    <th className="pb-1.5 text-left font-normal">Turn</th>
                    <th className="pb-1.5 text-right font-normal">STT</th>
                    <th className="pb-1.5 text-right font-normal">First token</th>
                    <th className="pb-1.5 text-right font-normal">Voice</th>
                    <th className="pb-1.5 text-right font-normal">Reply started</th>
                  </tr>
                </thead>
                <tbody>
                  {turns.map((t, i) => (
                    <tr key={i} className="border-t">
                      <td className="py-1">{i + 1}</td>
                      {["stt_ms", "llm_ttft_ms", "tts_first_ms", "first_audio_ms"].map((k) => (
                        <td key={k} className="py-1 text-right">
                          {t[k] !== undefined ? `${Math.round(t[k])} ms` : "—"}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
          ) : null}
        </div>
      </div>
    </>
  );
}

function RecordingPanel({ callId, canPlay, duration }: { callId: string; canPlay: boolean; duration: number }) {
  const wsApi = useWsApi();
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => () => (url ? URL.revokeObjectURL(url) : undefined), [url]);
  return (
    <Panel title="Recording" description={`${formatDuration(duration)}. Left channel caller, right channel agent. Every playback is logged.`}>
      {!canPlay ? (
        <p className="text-sm text-muted-foreground">Only editors and above can play recordings.</p>
      ) : url ? (
        <audio controls src={url} className="w-full" />
      ) : (
        <button
          type="button"
          className="text-sm underline underline-offset-4"
          onClick={async () => {
            try {
              const res = await api<Response>("GET", wsApi(`/calls/${callId}/recording`), { raw: true });
              setUrl(URL.createObjectURL(await res.blob()));
            } catch (e) {
              setError(errorMessage(e));
            }
          }}
        >
          Load recording
        </button>
      )}
      {error ? <p className="mt-2 text-xs text-destructive">{error}</p> : null}
    </Panel>
  );
}
