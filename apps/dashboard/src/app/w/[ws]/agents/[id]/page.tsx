"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, History, Link2, Play, Send, Trash2 } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { AgentForm, agentToValues } from "@/components/agents/agent-form";
import {
  ConfirmButton,
  EmptyState,
  ErrorState,
  Field,
  LoadingRows,
  NativeSelect,
  PageHeader,
  Panel,
  Spinner,
  StatusLamp,
} from "@/components/common";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { del, errorMessage, get, post, put } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { useCan, useWorkspaceId, useWsApi } from "@/lib/hooks";
import type { Agent } from "@/lib/types";

export default function AgentDetailPage() {
  const { id } = useParams<{ id: string }>();
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const qc = useQueryClient();
  const router = useRouter();
  const canEdit = useCan("editor");
  const agent = useQuery({ queryKey: ["agent", id], queryFn: () => get<Agent>(wsApi(`/agents/${id}`)) });

  const refresh = (a?: Agent) => {
    if (a) qc.setQueryData(["agent", id], a);
    void qc.invalidateQueries({ queryKey: ["agents", ws] });
    void qc.invalidateQueries({ queryKey: ["agent-versions", id] });
  };

  if (agent.error) return <ErrorState error={agent.error} onRetry={() => agent.refetch()} />;
  if (!agent.data) return <LoadingRows rows={6} />;
  const a = agent.data;

  return (
    <>
      <PageHeader
        title={a.name}
        description={
          <span className="inline-flex flex-wrap items-center gap-x-3 gap-y-1">
            <StatusLamp state={a.is_active ? "ok" : "idle"} label={a.is_active ? "Active" : "Inactive"} />
            <span>Version {a.version}</span>
            <span>Updated {formatDateTime(a.updated_at)}</span>
          </span>
        }
        actions={
          <>
            <Link href={`/w/${ws}/playground?agent=${a.id}`} className={buttonVariants({ variant: "outline" })}>
              <Play /> Talk to it
            </Link>
            {canEdit ? (
              <ConfirmButton
                title={`Delete ${a.name}?`}
                description="Past calls keep their transcripts. Campaigns using this agent must be paused or cancelled first."
                confirmLabel="Delete agent"
                size="default"
                onConfirm={async () => {
                  try {
                    await del(wsApi(`/agents/${a.id}`));
                    toast.success("Agent deleted");
                    refresh();
                    router.push(`/w/${ws}/agents`);
                  } catch (e) {
                    toast.error(errorMessage(e));
                  }
                }}
              >
                <Trash2 /> Delete
              </ConfirmButton>
            ) : null}
          </>
        }
      />
      <Tabs defaultValue="configure">
        <TabsList className="mb-5">
          <TabsTrigger value="configure">Configure</TabsTrigger>
          <TabsTrigger value="test">Test chat</TabsTrigger>
          <TabsTrigger value="simulate">Simulate a call</TabsTrigger>
          <TabsTrigger value="versions">Versions</TabsTrigger>
          <TabsTrigger value="share">Customer link</TabsTrigger>
        </TabsList>
        <TabsContent value="configure">
          <AgentForm
            key={a.version}
            initial={agentToValues(a)}
            readOnly={!canEdit}
            submitLabel="Save changes"
            onSubmit={async (v) => {
              const saved = await put<Agent>(wsApi(`/agents/${a.id}`), v);
              toast.success(saved.version > a.version ? `Saved as version ${saved.version}` : "No changes to save");
              refresh(saved);
            }}
          />
        </TabsContent>
        <TabsContent value="test">
          <TestConsole agent={a} />
        </TabsContent>
        <TabsContent value="simulate">
          <SimulatePanel agent={a} />
        </TabsContent>
        <TabsContent value="versions">
          <Versions agent={a} onRestored={refresh} />
        </TabsContent>
        <TabsContent value="share">
          <SharePanel agent={a} onChange={refresh} />
        </TabsContent>
      </Tabs>
    </>
  );
}

interface Turn {
  role: "user" | "assistant";
  content: string;
  tools?: { tool: string; message: string; ok: boolean }[];
  ms?: number;
}

function TestConsole({ agent }: { agent: Agent }) {
  const wsApi = useWsApi();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [text, setText] = useState("");
  const [greeting, setGreeting] = useState<string | null>(null);
  const send = useMutation({
    mutationFn: (message: string) =>
      post<{ reply: string; greeting: string; tools: Turn["tools"]; metrics: { llm_ttft_ms?: number; llm_total_ms?: number } }>(
        wsApi(`/agents/${agent.id}/test`),
        { message, history: turns.map(({ role, content }) => ({ role, content })) },
      ),
    onSuccess: (r, message) => {
      setGreeting(r.greeting);
      setTurns((t) => [...t, { role: "user", content: message }, { role: "assistant", content: r.reply, tools: r.tools, ms: r.metrics.llm_total_ms }]);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <Panel
      title="Test chat"
      description="Type as the caller. Uses the real local model and knowledge base. Actions run in test mode, so nothing is saved."
      actions={
        turns.length ? (
          <Button variant="ghost" size="sm" onClick={() => setTurns([])}>
            Clear
          </Button>
        ) : null
      }
    >
      <ol className="grid max-h-[55vh] gap-3 overflow-y-auto pb-4 text-sm" aria-live="polite">
        {greeting ? (
          <li className="grid grid-cols-[3.5rem_1fr] gap-3">
            <span className="text-xs text-signal">Agent</span>
            <p>{greeting}</p>
          </li>
        ) : null}
        {turns.map((t, i) => (
          <li key={i} className="grid grid-cols-[3.5rem_1fr] gap-3">
            <span className={t.role === "assistant" ? "text-xs text-signal" : "text-xs text-muted-foreground"}>
              {t.role === "assistant" ? "Agent" : "Caller"}
            </span>
            <div>
              {t.tools?.map((tool, j) => (
                <p key={j} className="mb-1 border-l-2 border-signal/60 pl-2 text-xs text-muted-foreground">
                  {tool.tool}: {tool.message}
                </p>
              ))}
              <p className="leading-relaxed">{t.content}</p>
              {t.ms ? <p className="mt-0.5 text-xs text-muted-foreground">{Math.round(t.ms)} ms</p> : null}
            </div>
          </li>
        ))}
        {turns.length === 0 && !greeting ? (
          <li className="py-6 text-center text-muted-foreground">Try “What do you offer?” or “Please don’t call me again.”</li>
        ) : null}
      </ol>
      <form
        className="flex gap-2 border-t pt-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (!text.trim() || send.isPending) return;
          send.mutate(text.trim());
          setText("");
        }}
      >
        <Input value={text} onChange={(e) => setText(e.target.value)} placeholder="Message as the caller…" aria-label="Message" maxLength={2000} />
        <Button type="submit" disabled={send.isPending || !text.trim()}>
          {send.isPending ? <Spinner /> : <Send />} Send
        </Button>
      </form>
    </Panel>
  );
}

const PERSONAS: [string, string][] = [
  ["interested", "Interested prospect"],
  ["questions", "Asks questions"],
  ["callback", "Wants a callback"],
  ["not_interested", "Not interested"],
  ["wrong_number", "Wrong number"],
  ["opt_out", "Asks not to be called"],
];

function SimulatePanel({ agent }: { agent: Agent }) {
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const router = useRouter();
  const [persona, setPersona] = useState("interested");
  const [mode, setMode] = useState<"scripted" | "llm">("scripted");
  const [direction, setDirection] = useState<"outbound" | "inbound">("outbound");
  const run = useMutation({
    mutationFn: () => post<{ call_id: string }>(wsApi("/calls/simulate"), { agent_id: agent.id, persona, callee_mode: mode, direction }),
    onSuccess: (r) => {
      toast.success("Simulation started");
      router.push(`/w/${ws}/calls/${r.call_id}`);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <Panel
      title="Simulate a call"
      description="A text-only rehearsal against a simulated person. No phone call is made, and the record is marked as a simulation."
    >
      <div className="grid max-w-3xl gap-4 md:grid-cols-3">
        <Field label="Simulated person" htmlFor="persona">
          <NativeSelect id="persona" value={persona} onChange={(e) => setPersona(e.target.value)}>
            {PERSONAS.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </NativeSelect>
        </Field>
        <Field label="Their replies" htmlFor="callee">
          <NativeSelect id="callee" value={mode} onChange={(e) => setMode(e.target.value as "scripted" | "llm")}>
            <option value="scripted">Scripted (fast, repeatable)</option>
            <option value="llm">Played by the local model</option>
          </NativeSelect>
        </Field>
        <Field label="Direction" htmlFor="direction">
          <NativeSelect id="direction" value={direction} onChange={(e) => setDirection(e.target.value as "outbound" | "inbound")}>
            <option value="outbound">Agent calls them</option>
            <option value="inbound">They call the agent</option>
          </NativeSelect>
        </Field>
      </div>
      <Button className="mt-5" onClick={() => run.mutate()} disabled={run.isPending || !agent.is_active}>
        {run.isPending ? <Spinner /> : <Play />} Run simulation
      </Button>
      {!agent.is_active ? <p className="mt-2 text-xs text-muted-foreground">Activate the agent to run simulations.</p> : null}
    </Panel>
  );
}

function Versions({ agent, onRestored }: { agent: Agent; onRestored: (a: Agent) => void }) {
  const wsApi = useWsApi();
  const canEdit = useCan("editor");
  const versions = useQuery({
    queryKey: ["agent-versions", agent.id],
    queryFn: () => get<{ version: number; note: string; created_at: string }[]>(wsApi(`/agents/${agent.id}/versions`)),
  });
  if (versions.error) return <ErrorState error={versions.error} />;
  if (!versions.data) return <LoadingRows />;
  return (
    <Panel title="Version history" description="Every saved change creates a version. Restoring creates a new version with the old settings." bodyClassName="p-0">
      <ol className="divide-y">
        {versions.data.map((v) => (
          <li key={v.version} className="flex items-center gap-4 px-5 py-3 text-sm">
            <History className="size-4 text-muted-foreground" />
            <span className="w-10 font-medium tabular-nums">v{v.version}</span>
            <span className="min-w-0 flex-1 truncate">{v.note || "—"}</span>
            <span className="text-muted-foreground">{formatDateTime(v.created_at)}</span>
            {canEdit && v.version !== agent.version ? (
              <ConfirmButton
                title={`Restore version ${v.version}?`}
                description="The agent’s settings will be replaced with this version’s. Your current settings stay in the history."
                confirmLabel="Restore"
                destructive={false}
                size="xs"
                onConfirm={async () => {
                  try {
                    const a = await post<Agent>(wsApi(`/agents/${agent.id}/versions/${v.version}/restore`));
                    toast.success(`Restored as version ${a.version}`);
                    onRestored(a);
                  } catch (e) {
                    toast.error(errorMessage(e));
                  }
                }}
              >
                Restore
              </ConfirmButton>
            ) : v.version === agent.version ? (
              <span className="text-xs text-muted-foreground">current</span>
            ) : null}
          </li>
        ))}
      </ol>
    </Panel>
  );
}

function SharePanel({ agent, onChange }: { agent: Agent; onChange: (a?: Agent) => void }) {
  const wsApi = useWsApi();
  const canEdit = useCan("editor");
  const [link, setLink] = useState<string | null>(null);
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => post<{ enabled: boolean; path: string | null }>(wsApi(`/agents/${agent.id}/public-link`), undefined, { enabled }),
    onSuccess: (r) => {
      setLink(r.path ? `${window.location.origin}${r.path}` : null);
      onChange();
      toast.success(r.enabled ? "Customer link created" : "Customer link turned off");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <Panel title="Customer link" description="A page where a customer can talk to this agent in their browser. Every conversation can become a lead.">
      <div className="grid max-w-2xl gap-4 text-sm">
        <p className="text-muted-foreground">
          The link only works for people who can reach this computer. Making it public means serving the dashboard over
          HTTPS (browsers require HTTPS for microphones) through your own reverse proxy or tunnel, adding that host to{" "}
          <code className="font-mono text-xs">VA_ALLOWED_HOSTS</code>, and setting{" "}
          <code className="font-mono text-xs">VA_PUBLIC_CALLS_ENABLED=true</code>. Calls are rate-limited per network and
          limited by your voice-session capacity.
        </p>
        <p>
          Status:{" "}
          <StatusLamp state={agent.public_link_enabled ? "ok" : "idle"} label={agent.public_link_enabled ? "Link active" : "No active link"} />
        </p>
        {link ? (
          <div className="flex gap-2">
            <Input readOnly value={link} aria-label="Customer link" className="font-mono text-xs" />
            <Button variant="outline" onClick={() => navigator.clipboard.writeText(link).then(() => toast.success("Copied"))}>
              <Copy /> Copy
            </Button>
          </div>
        ) : agent.public_link_enabled ? (
          <p className="text-xs text-muted-foreground">The link is only shown once. Create a new one if you need it again; the old one stops working.</p>
        ) : null}
        {canEdit ? (
          <div className="flex gap-2">
            <Button onClick={() => toggle.mutate(true)} disabled={toggle.isPending}>
              <Link2 /> {agent.public_link_enabled ? "Create a new link" : "Create link"}
            </Button>
            {agent.public_link_enabled ? (
              <Button variant="outline" onClick={() => toggle.mutate(false)} disabled={toggle.isPending}>
                Turn off
              </Button>
            ) : null}
          </div>
        ) : null}
        {!agent.is_active ? <EmptyState title="This agent is inactive">Customers can’t reach inactive agents.</EmptyState> : null}
      </div>
    </Panel>
  );
}
