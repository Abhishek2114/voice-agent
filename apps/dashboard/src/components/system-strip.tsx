"use client";

import { StatusLamp } from "@/components/common";
import type { SystemHealth } from "@/lib/types";
import { cn } from "@/lib/utils";

export function SystemStrip({ health, compact = false }: { health: SystemHealth; compact?: boolean }) {
  const items: { label: string; state: string; detail: string }[] = [
    { label: "Language model", state: health.llm.status, detail: health.llm.detail },
    { label: "Speech recognition", state: health.stt.status, detail: health.stt.detail },
    { label: "Voice synthesis", state: health.tts.status, detail: health.tts.detail },
    { label: "Voice detection", state: health.vad.status, detail: health.vad.detail },
    { label: "Scheduler", state: health.scheduler.status, detail: health.scheduler.detail },
    { label: "Database", state: health.database.status, detail: health.database.detail ?? `${health.database.latency_ms ?? "–"} ms` },
    { label: "Telephony", state: health.telephony.status, detail: health.telephony.detail },
  ];
  return (
    <ul className={cn("grid gap-x-6 gap-y-3", compact ? "sm:grid-cols-2" : "sm:grid-cols-2 lg:grid-cols-4")}>
      {items.map((i) => (
        <li key={i.label} className="min-w-0">
          <StatusLamp state={i.state} label={i.label} className="text-sm font-medium" />
          <p className="mt-0.5 truncate pl-4 text-xs text-muted-foreground" title={i.detail}>
            {i.detail}
          </p>
        </li>
      ))}
    </ul>
  );
}
