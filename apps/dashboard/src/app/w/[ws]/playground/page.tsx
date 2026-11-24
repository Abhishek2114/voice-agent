"use client";

import { useQuery } from "@tanstack/react-query";
import { Bot } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { EmptyState, ErrorState, Field, LoadingRows, NativeSelect, PageHeader } from "@/components/common";
import { LiveCall } from "@/components/voice/live-call";
import { buttonVariants } from "@/components/ui/button";
import { get } from "@/lib/api";
import { useCan, useWorkspaceId, useWsApi } from "@/lib/hooks";
import type { Agent } from "@/lib/types";

function Playground() {
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const params = useSearchParams();
  const canEdit = useCan("editor");
  const agents = useQuery({ queryKey: ["agents", ws], queryFn: () => get<Agent[]>(wsApi("/agents")) });
  const active = (agents.data ?? []).filter((a) => a.is_active);
  const [picked, setPicked] = useState<string | null>(params.get("agent"));
  const agentId = picked && active.some((a) => a.id === picked) ? picked : active[0]?.id;
  const agent = active.find((a) => a.id === agentId);
  const [inCall, setInCall] = useState(false);

  return (
    <>
      <PageHeader
        title="Voice playground"
        description="Talk to an agent through your microphone. Speech recognition, the model and the voice all run on this Mac."
      />
      {agents.error ? (
        <ErrorState error={agents.error} onRetry={() => agents.refetch()} />
      ) : agents.isLoading ? (
        <LoadingRows rows={4} />
      ) : active.length === 0 ? (
        <div className="rounded-xl border bg-panel">
          <EmptyState
            icon={<Bot className="size-6" />}
            title="No active agents"
            action={
              <Link href={`/w/${ws}/agents/new`} className={buttonVariants()}>
                Create an agent
              </Link>
            }
          >
            Create an agent first. It needs a name, a greeting and a local model.
          </EmptyState>
        </div>
      ) : (
        <div className="grid gap-5">
          <div className="flex flex-wrap items-end gap-4">
            <Field label="Agent" htmlFor="agent" className="w-72">
              <NativeSelect id="agent" value={agentId} disabled={inCall} onChange={(e) => setPicked(e.target.value)}>
                {active.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            {agent ? (
              <p className="pb-1.5 text-sm text-muted-foreground">
                Uses {agent.llm.model} with the {agent.tts.provider === "macos_say" ? "macOS" : "Piper"} voice. Calls end after{" "}
                {Math.round(agent.max_duration_seconds / 60)} minutes.
              </p>
            ) : null}
          </div>
          {!canEdit ? (
            <p className="text-sm text-muted-foreground">Viewers can’t start conversations. Ask an editor for access.</p>
          ) : null}
          <LiveCall
            key={agentId}
            offerUrl={wsApi("/voice/sessions")}
            offerBody={{ agent_id: agentId }}
            endUrl={(id) => wsApi(`/voice/sessions/${id}/control`)}
            disabled={!agentId || !canEdit}
            onCallStarted={() => setInCall(true)}
            onCallEnded={() => setInCall(false)}
            renderAfter={(callId, state) =>
              callId && (state === "ended" || state === "error") ? (
                <p className="mt-4 text-sm">
                  The transcript, summary and outcome are saved.{" "}
                  <Link href={`/w/${ws}/calls/${callId}`} className="font-medium underline underline-offset-4">
                    Open the call record
                  </Link>
                </p>
              ) : null
            }
          />
          <p className="max-w-3xl text-xs text-muted-foreground">
            How audio flows: your speech is transcribed after you pause (about 0.7 s of silence). The reply is generated
            token by token and spoken one sentence at a time. Each sentence is synthesised in full before it plays, so
            playback starts after the first sentence is ready rather than after the whole reply. Talking over the
            agent interrupts it.
          </p>
        </div>
      )}
    </>
  );
}

export default function PlaygroundPage() {
  return (
    <Suspense fallback={<LoadingRows rows={4} />}>
      <Playground />
    </Suspense>
  );
}
