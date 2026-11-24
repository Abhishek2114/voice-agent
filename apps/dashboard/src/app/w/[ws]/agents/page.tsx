"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Copy, Plus } from "lucide-react";
import Link from "next/link";
import { toast } from "sonner";

import { EmptyState, ErrorState, LoadingRows, PageHeader, StatusLamp } from "@/components/common";
import { Button, buttonVariants } from "@/components/ui/button";
import { errorMessage, get, post } from "@/lib/api";
import { formatRelative } from "@/lib/format";
import { useCan, useWorkspaceId, useWsApi } from "@/lib/hooks";
import type { Agent } from "@/lib/types";

export default function AgentsPage() {
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const qc = useQueryClient();
  const canEdit = useCan("editor");
  const agents = useQuery({ queryKey: ["agents", ws], queryFn: () => get<Agent[]>(wsApi("/agents")) });
  const duplicate = useMutation({
    mutationFn: (id: string) => post<Agent>(wsApi(`/agents/${id}/duplicate`)),
    onSuccess: (a) => {
      toast.success(`Duplicated as “${a.name}” (inactive)`);
      void qc.invalidateQueries({ queryKey: ["agents", ws] });
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  return (
    <>
      <PageHeader
        title="Agents"
        description="Each agent has its own instructions, voice and allowed actions. All of them run on your local model."
        actions={
          canEdit ? (
            <Link href={`/w/${ws}/agents/new`} className={buttonVariants()}>
              <Plus /> New agent
            </Link>
          ) : null
        }
      />
      {agents.error ? (
        <ErrorState error={agents.error} onRetry={() => agents.refetch()} />
      ) : !agents.data ? (
        <LoadingRows rows={4} />
      ) : agents.data.length === 0 ? (
        <div className="rounded-xl border bg-panel">
          <EmptyState
            icon={<Bot className="size-6" />}
            title="No agents yet"
            action={
              canEdit ? (
                <Link href={`/w/${ws}/agents/new`} className={buttonVariants()}>
                  Create your first agent
                </Link>
              ) : null
            }
          >
            Describe how the agent should greet people, what it should achieve and which actions it may take.
          </EmptyState>
        </div>
      ) : (
        <ul className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {agents.data.map((a) => (
            <li key={a.id} className="flex flex-col rounded-xl border bg-panel">
              <Link href={`/w/${ws}/agents/${a.id}`} className="flex-1 p-5 transition-colors hover:bg-muted/40">
                <div className="flex items-start justify-between gap-3">
                  <h2 className="font-semibold">{a.name}</h2>
                  <StatusLamp state={a.is_active ? "ok" : "idle"} label={a.is_active ? "Active" : "Inactive"} className="shrink-0 text-xs" />
                </div>
                <p className="mt-1.5 line-clamp-2 min-h-10 text-sm text-muted-foreground">
                  {a.description || a.greeting || "No description"}
                </p>
                <dl className="mt-4 grid grid-cols-3 gap-2 text-xs">
                  <div>
                    <dt className="text-muted-foreground">Model</dt>
                    <dd className="mt-0.5 truncate font-mono text-[11px]" title={a.llm.model}>
                      {a.llm.model?.split(":")[0] ?? "—"}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Calls</dt>
                    <dd className="mt-0.5 tabular-nums">{a.stats.calls ?? 0}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Version</dt>
                    <dd className="mt-0.5 tabular-nums">v{a.version}</dd>
                  </div>
                </dl>
              </Link>
              <div className="flex items-center justify-between border-t px-5 py-2.5 text-xs text-muted-foreground">
                <span>Updated {formatRelative(a.updated_at)}</span>
                <span className="flex gap-1">
                  <Link href={`/w/${ws}/playground?agent=${a.id}`} className={buttonVariants({ variant: "ghost", size: "xs" })}>
                    Talk
                  </Link>
                  {canEdit ? (
                    <Button variant="ghost" size="xs" onClick={() => duplicate.mutate(a.id)} disabled={duplicate.isPending}>
                      <Copy /> Duplicate
                    </Button>
                  ) : null}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
