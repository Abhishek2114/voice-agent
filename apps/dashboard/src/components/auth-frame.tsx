import type { ReactNode } from "react";

/** Two-column frame for sign-in / setup: a quiet form beside a static two-channel trace. */
export function AuthFrame({ title, subtitle, children }: { title: string; subtitle: string; children: ReactNode }) {
  return (
    <main className="grid min-h-dvh lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      <div className="flex flex-col justify-center px-6 py-12 sm:px-12">
        <div className="mx-auto w-full max-w-sm">
          <p className="mb-10 flex items-center gap-2 text-sm font-semibold">
            <TraceMark /> voice-agent
          </p>
          <h1 className="text-2xl font-semibold tracking-[-0.015em]">{title}</h1>
          <p className="mt-1.5 mb-8 text-sm text-muted-foreground">{subtitle}</p>
          {children}
        </div>
      </div>
      <aside className="relative hidden overflow-hidden border-l bg-rail lg:block" aria-hidden>
        <ChannelArt />
        <div className="absolute inset-x-12 bottom-12 max-w-md text-sm text-muted-foreground">
          <p className="text-foreground">Runs on this Mac.</p>
          <p className="mt-1">
            Speech recognition, the language model and the voice are all local. Nothing is sent to a cloud AI
            service.
          </p>
        </div>
      </aside>
    </main>
  );
}

export function TraceMark({ className = "" }: { className?: string }) {
  return (
    <svg viewBox="0 0 20 20" className={`size-5 ${className}`} aria-hidden>
      <rect x="0.5" y="0.5" width="19" height="19" rx="5" className="fill-primary" />
      {[4, 7, 10, 13, 16].map((x, i) => (
        <rect key={x} x={x - 0.75} y={10 - [2, 5, 7, 4, 2][i]} width="1.5" height={[2, 5, 7, 4, 2][i] * 2} rx="0.75" className={i === 2 ? "fill-signal" : "fill-primary-foreground"} />
      ))}
    </svg>
  );
}

/** Deterministic two-channel waveform: caller (upper) and agent (lower), drawn once. */
function ChannelArt() {
  const bars = 72;
  const caller = Array.from({ length: bars }, (_, i) => Math.abs(Math.sin(i * 0.37) * Math.cos(i * 0.11)) * (i % 23 < 13 ? 1 : 0.08));
  const agent = Array.from({ length: bars }, (_, i) => Math.abs(Math.sin(i * 0.29 + 1.3) * Math.sin(i * 0.07)) * (i % 23 >= 12 ? 1 : 0.06));
  return (
    <svg viewBox="0 0 720 420" preserveAspectRatio="xMidYMid slice" className="absolute inset-0 size-full">
      <line x1="0" x2="720" y1="140" y2="140" className="stroke-border" />
      <line x1="0" x2="720" y1="280" y2="280" className="stroke-border" />
      {caller.map((v, i) => (
        <rect key={`c${i}`} x={i * 10 + 2} y={140 - v * 90} width="5" height={Math.max(2, v * 180)} rx="2.5" className="fill-foreground/25" />
      ))}
      {agent.map((v, i) => (
        <rect key={`a${i}`} x={i * 10 + 2} y={280 - v * 70} width="5" height={Math.max(2, v * 140)} rx="2.5" className="fill-signal/70" />
      ))}
    </svg>
  );
}
