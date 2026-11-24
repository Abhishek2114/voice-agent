"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { ErrorState, Spinner } from "@/components/common";
import { get } from "@/lib/api";
import type { Me } from "@/lib/types";

interface Status {
  setup_required: boolean;
  authenticated: boolean;
}

export default function Home() {
  const router = useRouter();
  const status = useQuery({ queryKey: ["auth-status"], queryFn: () => get<Status>("/api/auth/status") });
  const me = useQuery({
    queryKey: ["me"],
    queryFn: () => get<Me>("/api/auth/me"),
    enabled: !!status.data?.authenticated,
  });

  useEffect(() => {
    if (!status.data) return;
    if (status.data.setup_required) router.replace("/setup");
    else if (!status.data.authenticated) router.replace("/login");
    else if (me.data) {
      const last = typeof window !== "undefined" ? localStorage.getItem("va:last-ws") : null;
      const ws = me.data.workspaces.find((w) => w.id === last) ?? me.data.workspaces[0];
      router.replace(ws ? `/w/${ws.id}` : "/workspaces/new");
    }
  }, [status.data, me.data, router]);

  return (
    <main className="grid min-h-dvh place-items-center p-6">
      {status.error ? (
        <div className="w-full max-w-md">
          <ErrorState error={status.error} onRetry={() => status.refetch()} />
        </div>
      ) : (
        <Spinner className="size-5 text-muted-foreground" />
      )}
    </main>
  );
}
