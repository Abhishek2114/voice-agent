"use client";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";

import { TraceMark } from "@/components/auth-frame";
import { ErrorState, LoadingRows } from "@/components/common";
import { LiveCall } from "@/components/voice/live-call";
import { get } from "@/lib/api";

interface PublicInfo {
  agent_name: string;
  business_name: string;
  brand_color: string;
  description: string;
  ai_notice: string;
  recording: boolean;
}

/** Customer-facing page: a visitor talks to one agent. No sign-in; rate-limited server-side. */
export default function TalkPage() {
  const { token } = useParams<{ token: string }>();
  const info = useQuery({ queryKey: ["public", token], queryFn: () => get<PublicInfo>(`/api/public/talk/${token}`), retry: false });
  return (
    <main className="mx-auto min-h-dvh w-full max-w-5xl px-4 py-10 sm:px-8">
      {info.error ? (
        <div className="mx-auto max-w-md pt-20">
          <ErrorState error={new Error("This link isn’t active. Ask the business for a new one.")} />
        </div>
      ) : !info.data ? (
        <LoadingRows rows={4} />
      ) : (
        <>
          <header className="mb-8 flex items-center gap-3">
            <span className="grid size-10 place-items-center rounded-lg text-lg font-semibold text-white" style={{ background: info.data.brand_color }} aria-hidden>
              {info.data.business_name.slice(0, 1)}
            </span>
            <div>
              <h1 className="text-xl font-semibold">Talk to {info.data.business_name}</h1>
              <p className="text-sm text-muted-foreground">{info.data.description || `Speak with ${info.data.agent_name}.`}</p>
            </div>
          </header>
          <p className="mb-6 max-w-2xl text-sm">
            {info.data.ai_notice} {info.data.recording ? "This conversation may be recorded. " : ""}If you’d like someone from the team to follow up, tell the
            assistant your name and number, and say whether they may contact you.
          </p>
          <LiveCall
            offerUrl={`/api/public/talk/${token}/sessions`}
            offerBody={{}}
            endUrl={(id) => `/api/public/talk/${token}/sessions/${id}/end`}
            startLabel="Start talking"
          />
          <footer className="mt-10 flex items-center gap-2 text-xs text-muted-foreground">
            <TraceMark className="size-4" /> Runs on voice-agent. Speech is processed on the business’s own computer.
          </footer>
        </>
      )}
    </main>
  );
}
