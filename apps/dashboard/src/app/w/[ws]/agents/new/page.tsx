"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { AgentForm, agentToValues } from "@/components/agents/agent-form";
import { LoadingRows, PageHeader } from "@/components/common";
import { get, post } from "@/lib/api";
import { useWorkspaceId, useWsApi } from "@/lib/hooks";
import type { Agent } from "@/lib/types";

export default function NewAgentPage() {
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const router = useRouter();
  const qc = useQueryClient();
  const models = useQuery({ queryKey: ["models"], queryFn: () => get<{ default: string }>("/api/system/models") });

  return (
    <>
      <PageHeader title="New agent" description="You can test it in the playground as soon as it’s saved." />
      {models.isLoading ? (
        <LoadingRows rows={6} />
      ) : (
        <AgentForm
          initial={agentToValues(undefined, models.data?.default)}
          submitLabel="Create agent"
          onSubmit={async (v) => {
            const a = await post<Agent>(wsApi("/agents"), v);
            toast.success(`Created ${a.name}`);
            await qc.invalidateQueries({ queryKey: ["agents", ws] });
            router.push(`/w/${ws}/agents/${a.id}`);
          }}
        />
      )}
    </>
  );
}
