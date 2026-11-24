"use client";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";

import { get } from "@/lib/api";
import type { Me, Role, Workspace } from "@/lib/types";

const RANK: Record<Role, number> = { viewer: 10, editor: 20, admin: 30, owner: 40 };

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => get<Me>("/api/auth/me"), staleTime: 60_000 });
}

export function useWorkspaceId(): string {
  const params = useParams<{ ws: string }>();
  return params.ws;
}

/** Path helper for the current workspace's API resources. */
export function useWsApi(): (path?: string) => string {
  const ws = useWorkspaceId();
  return (path = "") => `/api/workspaces/${ws}${path}`;
}

export function useWorkspace() {
  const ws = useWorkspaceId();
  return useQuery({ queryKey: ["workspace", ws], queryFn: () => get<Workspace>(`/api/workspaces/${ws}`), enabled: !!ws });
}

export function useCan(min: Role): boolean {
  const { data } = useWorkspace();
  return !!data && RANK[data.role] >= RANK[min];
}
