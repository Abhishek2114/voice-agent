"use client";

import { useQuery } from "@tanstack/react-query";
import { Megaphone, Plus } from "lucide-react";
import Link from "next/link";

import { EmptyState, ErrorState, LoadingRows, ModeBadge, PageHeader, StatusText } from "@/components/common";
import { buttonVariants } from "@/components/ui/button";
import { get } from "@/lib/api";
import { formatRelative, humanize } from "@/lib/format";
import { useCan, useWorkspaceId, useWsApi } from "@/lib/hooks";
import type { Campaign } from "@/lib/types";
import { ProgressBar } from "@/components/campaigns/progress-bar";

export default function CampaignsPage() {
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const canEdit = useCan("editor");
  const campaigns = useQuery({
    queryKey: ["campaigns", ws],
    queryFn: () => get<Campaign[]>(wsApi("/campaigns")),
    refetchInterval: (q) => (q.state.data?.some((c) => c.status === "running") ? 4000 : false),
  });
  return (
    <>
      <PageHeader
        title="Campaigns"
        description="Call a list of contacts with an agent. Campaigns run as simulations unless real telephony is configured and authorised."
        actions={
          canEdit ? (
            <Link href={`/w/${ws}/campaigns/new`} className={buttonVariants()}>
              <Plus /> New campaign
            </Link>
          ) : null
        }
      />
      {campaigns.error ? (
        <ErrorState error={campaigns.error} onRetry={() => campaigns.refetch()} />
      ) : !campaigns.data ? (
        <LoadingRows rows={4} />
      ) : campaigns.data.length === 0 ? (
        <div className="rounded-xl border bg-panel">
          <EmptyState
            icon={<Megaphone className="size-6" />}
            title="No campaigns yet"
            action={
              canEdit ? (
                <Link href={`/w/${ws}/campaigns/new`} className={buttonVariants()}>
                  Plan a campaign
                </Link>
              ) : null
            }
          >
            Every contact is checked for consent, opt-outs, DND status, calling hours and call frequency before it is dialled.
          </EmptyState>
        </div>
      ) : (
        <ul className="grid gap-3">
          {campaigns.data.map((c) => (
            <li key={c.id}>
              <Link
                href={`/w/${ws}/campaigns/${c.id}`}
                className="grid gap-4 rounded-xl border bg-panel p-5 transition-colors hover:bg-muted/40 md:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)_auto]"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2.5">
                    <h2 className="truncate font-semibold">{c.name}</h2>
                    <ModeBadge mode={c.mode} />
                  </div>
                  <p className="mt-1 text-sm text-muted-foreground">
                    {c.agent_name ?? "Agent removed"}, {humanize(c.purpose).toLowerCase()} calls between {c.window_start} and {c.window_end}
                  </p>
                </div>
                <ProgressBar progress={c.progress} />
                <div className="flex flex-col items-end gap-1 text-right">
                  <StatusText status={c.status} />
                  <span className="text-xs text-muted-foreground">{formatRelative(c.created_at)}</span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
