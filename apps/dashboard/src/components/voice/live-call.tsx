"use client";

import { Mic, MicOff, PhoneOff, Square } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { Spinner, StatusLamp } from "@/components/common";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";
import { VoiceClient, type VoiceEvent, type VoiceState } from "@/lib/voice-client";

export interface TranscriptLine {
  id: number;
  role: "user" | "assistant" | "tool" | "system";
  text: string;
  interrupted?: boolean;
  metrics?: Record<string, number>;
}

const STATE_TEXT: Record<VoiceState, string> = {
  idle: "Ready",
  "requesting-mic": "Waiting for microphone permission",
  connecting: "Connecting",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
  waiting: "Waiting for the local model",
  ended: "Call ended",
  error: "Error",
};

const SEGMENTS = 28;

/** One channel of the signal strip: segmented level meter fed straight from an AnalyserNode. */
function ChannelMeter({ label, tone, levelRef }: { label: string; tone: "caller" | "agent"; levelRef: React.RefObject<number> }) {
  const barsRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let raf = 0;
    const tick = () => {
      const el = barsRef.current;
      if (el) {
        const lit = Math.round((levelRef.current ?? 0) * SEGMENTS);
        const children = el.children;
        for (let i = 0; i < children.length; i++) {
          (children[i] as HTMLElement).dataset.on = i < lit ? "1" : "0";
        }
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [levelRef]);
  return (
    <div className="grid grid-cols-[4.5rem_minmax(0,1fr)] items-center gap-3">
      <span className="text-xs text-muted-foreground">{label}</span>
      <div ref={barsRef} className="flex h-5 gap-[3px]" aria-hidden>
        {Array.from({ length: SEGMENTS }).map((_, i) => (
          <span
            key={i}
            data-on="0"
            className={cn(
              "h-full flex-1 rounded-[2px] bg-muted transition-colors duration-75",
              tone === "agent" ? "data-[on=1]:bg-signal" : "data-[on=1]:bg-foreground/70",
              i > SEGMENTS * 0.85 && (tone === "agent" ? "data-[on=1]:bg-destructive" : "data-[on=1]:bg-destructive/80"),
            )}
          />
        ))}
      </div>
    </div>
  );
}

export interface LiveCallProps {
  offerUrl: string;
  offerBody: Record<string, unknown>;
  endUrl?: (callId: string) => string;
  disabled?: boolean;
  startLabel?: string;
  onCallStarted?: (callId: string) => void;
  onCallEnded?: (callId: string | null) => void;
  renderAfter?: (callId: string | null, state: VoiceState) => ReactNode;
  compactTranscript?: boolean;
}

export function LiveCall({
  offerUrl,
  offerBody,
  endUrl,
  disabled,
  startLabel = "Start conversation",
  onCallStarted,
  onCallEnded,
  renderAfter,
}: LiveCallProps) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const clientRef = useRef<VoiceClient | null>(null);
  const micLevel = useRef(0);
  const agentLevel = useRef(0);
  const [state, setState] = useState<VoiceState>("idle");
  const [detail, setDetail] = useState<string | undefined>();
  const [lines, setLines] = useState<TranscriptLine[]>([]);
  const [partial, setPartial] = useState("");
  const [userSpeaking, setUserSpeaking] = useState(false);
  const [muted, setMuted] = useState(false);
  const [callId, setCallId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastMetrics, setLastMetrics] = useState<Record<string, number> | null>(null);
  const lineId = useRef(0);
  const scrollRef = useRef<HTMLDivElement>(null);

  // drive meters
  useEffect(() => {
    let raf = 0;
    const tick = () => {
      const c = clientRef.current;
      micLevel.current = c?.micMeter && !c.muted ? c.micMeter.level() : 0;
      agentLevel.current = c?.agentMeter ? c.agentMeter.level() : 0;
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [lines, partial]);

  useEffect(() => () => clientRef.current?.cleanup(), []);

  const onEvent = useCallback((ev: VoiceEvent) => {
    if (ev.type === "transcript") {
      const role = ev.role as TranscriptLine["role"];
      if (role === "assistant") setPartial("");
      const metrics = (ev.metrics as Record<string, number> | undefined) ?? undefined;
      if (role === "assistant" && metrics?.first_audio_ms) setLastMetrics(metrics);
      setLines((l) => [...l, { id: ++lineId.current, role, text: String(ev.text ?? ""), interrupted: !!ev.interrupted, metrics }]);
    } else if (ev.type === "agent_delta") {
      setPartial((p) => p + String(ev.text ?? ""));
    } else if (ev.type === "user_speech") {
      setUserSpeaking(!!ev.active);
    } else if (ev.type === "interrupted") {
      setPartial("");
    } else if (ev.type === "tool") {
      setLines((l) => [...l, { id: ++lineId.current, role: "tool", text: `${String(ev.tool)}: ${String(ev.message ?? "")}` }]);
    } else if (ev.type === "error") {
      const msg = String(ev.message ?? "Provider error");
      if (ev.recoverable) setLines((l) => [...l, { id: ++lineId.current, role: "system", text: msg }]);
      else setError(msg);
    }
  }, []);

  const start = async () => {
    setError(null);
    setLines([]);
    setPartial("");
    setLastMetrics(null);
    setMuted(false);
    setCallId(null);
    const client = new VoiceClient({
      offerUrl,
      offerBody,
      endUrl,
      audioElement: audioRef.current!,
      onEvent,
      onState: (s, d) => {
        setState(s);
        setDetail(d);
        if (s === "error" && d) setError(d);
        if (s === "ended" || s === "error") onCallEnded?.(client.callId);
      },
    });
    clientRef.current = client;
    try {
      const id = await client.start();
      setCallId(id);
      onCallStarted?.(id);
    } catch (e) {
      client.cleanup();
      setState("error");
      setError(errorMessage(e));
    }
  };

  const active = !["idle", "ended", "error"].includes(state);
  const lamp = state === "speaking" ? "live" : state === "error" ? "unavailable" : active ? "ok" : "idle";

  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
      <div className="grid content-start gap-5">
        <section className="rounded-xl border bg-panel p-5" aria-label="Call controls">
          <div className="flex items-center justify-between gap-3">
            <StatusLamp state={lamp} label={STATE_TEXT[state]} className="text-sm font-medium" />
            {userSpeaking && active ? <span className="text-xs text-muted-foreground">You’re speaking</span> : null}
          </div>
          {detail && state !== "error" ? <p className="mt-1 text-xs text-muted-foreground">{detail}</p> : null}
          <div className="mt-5 grid gap-2.5">
            <ChannelMeter label="You" tone="caller" levelRef={micLevel} />
            <ChannelMeter label="Agent" tone="agent" levelRef={agentLevel} />
          </div>
          <div className="mt-6 flex flex-wrap gap-2">
            {!active ? (
              <Button onClick={start} disabled={disabled} size="lg">
                {state === "requesting-mic" || state === "connecting" ? <Spinner /> : <Mic />}
                {state === "ended" ? "Start again" : startLabel}
              </Button>
            ) : (
              <>
                <Button variant="destructive" size="lg" onClick={() => clientRef.current?.end()}>
                  <PhoneOff /> End
                </Button>
                <Button
                  variant="outline"
                  size="lg"
                  aria-pressed={muted}
                  onClick={() => {
                    const next = !muted;
                    setMuted(next);
                    clientRef.current?.setMuted(next);
                  }}
                >
                  {muted ? <MicOff /> : <Mic />} {muted ? "Unmute" : "Mute"}
                </Button>
                <Button variant="outline" size="lg" disabled={state !== "speaking"} onClick={() => clientRef.current?.interrupt()}>
                  <Square /> Interrupt
                </Button>
              </>
            )}
          </div>
          {error ? (
            <p role="alert" className="mt-4 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {error}
            </p>
          ) : null}
          {lastMetrics ? (
            <dl className="mt-5 grid grid-cols-4 gap-3 border-t pt-4 text-xs">
              {(
                [
                  ["Speech to text", lastMetrics.stt_ms],
                  ["First token", lastMetrics.llm_ttft_ms],
                  ["First sentence voiced", lastMetrics.tts_first_ms],
                  ["Reply started", lastMetrics.first_audio_ms],
                ] as const
              ).map(([k, v]) => (
                <div key={k}>
                  <dt className="text-muted-foreground">{k}</dt>
                  <dd className="mt-0.5 font-medium tabular-nums">{v !== undefined ? `${Math.round(v)} ms` : "—"}</dd>
                </div>
              ))}
            </dl>
          ) : null}
          {renderAfter ? renderAfter(callId, state) : null}
        </section>
      </div>

      <section className="flex min-h-[420px] flex-col rounded-xl border bg-panel" aria-label="Live transcript">
        <div className="border-b px-5 py-3.5">
          <h2 className="text-[0.95rem] font-semibold">Transcript</h2>
        </div>
        <div ref={scrollRef} className="max-h-[60vh] min-h-0 flex-1 overflow-y-auto px-5 py-4" aria-live="polite">
          {lines.length === 0 && !partial ? (
            <p className="py-10 text-center text-sm text-muted-foreground">
              {active ? "Say hello. The agent speaks first, then listens." : "The conversation will appear here as it happens."}
            </p>
          ) : (
            <ol className="grid gap-3">
              {lines.map((l) => (
                <TranscriptRow key={l.id} line={l} />
              ))}
              {partial ? <TranscriptRow line={{ id: -1, role: "assistant", text: partial }} pending /> : null}
            </ol>
          )}
        </div>
      </section>
      <audio ref={audioRef} autoPlay className="hidden" />
    </div>
  );
}

function TranscriptRow({ line, pending }: { line: TranscriptLine; pending?: boolean }) {
  if (line.role === "tool" || line.role === "system") {
    return <li className="border-l-2 border-signal/60 pl-3 text-xs text-muted-foreground">{line.text}</li>;
  }
  const agent = line.role === "assistant";
  return (
    <li className="grid grid-cols-[3.5rem_minmax(0,1fr)] gap-3 text-sm">
      <span className={cn("pt-px text-xs", agent ? "text-signal" : "text-muted-foreground")}>{agent ? "Agent" : "You"}</span>
      <p className={cn("leading-relaxed", pending && "text-muted-foreground")}>
        {line.text}
        {line.interrupted ? <span className="ml-1.5 text-xs text-muted-foreground">(interrupted)</span> : null}
      </p>
    </li>
  );
}
