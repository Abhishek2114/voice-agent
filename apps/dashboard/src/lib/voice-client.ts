/**
 * Browser side of a live voice session.
 *
 * Microphone (echo-cancelled) → WebRTC (Opus) → voice-agent backend → agent audio back over the
 * same peer connection. A data channel named "events" carries transcript/state events from the
 * server and control messages (interrupt, mute, end) from the browser. No STUN/TURN servers
 * are used: media stays on this machine/LAN.
 */

import { ApiError, readCookie } from "@/lib/api";

export type VoiceState =
  | "idle"
  | "requesting-mic"
  | "connecting"
  | "listening"
  | "thinking"
  | "speaking"
  | "waiting"
  | "ended"
  | "error";

export interface VoiceEvent {
  type: string;
  [key: string]: unknown;
}

export interface VoiceClientOptions {
  offerUrl: string;
  offerBody: Record<string, unknown>;
  endUrl?: (callId: string) => string;
  onEvent: (e: VoiceEvent) => void;
  onState: (s: VoiceState, detail?: string) => void;
  audioElement: HTMLAudioElement;
}

export class Meter {
  private analyser: AnalyserNode;
  private data: Float32Array<ArrayBuffer>;

  constructor(ctx: AudioContext, stream: MediaStream) {
    const src = ctx.createMediaStreamSource(stream);
    this.analyser = ctx.createAnalyser();
    this.analyser.fftSize = 1024;
    this.data = new Float32Array(new ArrayBuffer(this.analyser.fftSize * 4));
    src.connect(this.analyser);
  }

  /** 0..1 level on a perceptual (dB-ish) scale. */
  level(): number {
    this.analyser.getFloatTimeDomainData(this.data);
    let sum = 0;
    for (let i = 0; i < this.data.length; i++) sum += this.data[i] * this.data[i];
    const rms = Math.sqrt(sum / this.data.length);
    const db = 20 * Math.log10(Math.max(rms, 1e-5));
    return Math.min(1, Math.max(0, (db + 60) / 50));
  }
}

export class VoiceClient {
  pc: RTCPeerConnection | null = null;
  channel: RTCDataChannel | null = null;
  mic: MediaStream | null = null;
  callId: string | null = null;
  micMeter: Meter | null = null;
  agentMeter: Meter | null = null;
  private ctx: AudioContext | null = null;
  private opts: VoiceClientOptions;
  private closed = false;
  muted = false;

  constructor(opts: VoiceClientOptions) {
    this.opts = opts;
  }

  async start(): Promise<string> {
    const { onState } = this.opts;
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error("This browser cannot capture audio. Use a current Chrome, Edge, Safari or Firefox on localhost or HTTPS.");
    }
    onState("requesting-mic");
    try {
      this.mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
    } catch (e) {
      const name = (e as DOMException).name;
      throw new Error(
        name === "NotAllowedError"
          ? "Microphone access was blocked. Allow it in the browser’s site settings and try again."
          : name === "NotFoundError"
            ? "No microphone was found. Connect one and try again."
            : `Could not open the microphone (${name}).`,
      );
    }
    onState("connecting");
    this.ctx = new AudioContext();
    this.micMeter = new Meter(this.ctx, this.mic);

    const stun = process.env.VA_STUN_URL;
    const pc = new RTCPeerConnection({ iceServers: stun ? [{ urls: stun }] : [] });
    this.pc = pc;
    const channel = pc.createDataChannel("events");
    this.channel = channel;
    channel.onmessage = (m) => {
      try {
        const ev = JSON.parse(String(m.data)) as VoiceEvent;
        this.handleEvent(ev);
      } catch {
        /* ignore malformed */
      }
    };
    for (const track of this.mic.getAudioTracks()) pc.addTrack(track, this.mic);
    pc.ontrack = (e) => {
      const stream = e.streams[0] ?? new MediaStream([e.track]);
      this.opts.audioElement.srcObject = stream;
      void this.opts.audioElement.play().catch(() => undefined);
      if (this.ctx) this.agentMeter = new Meter(this.ctx, stream);
    };
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "failed") this.fail("The audio connection failed. Check that the backend is running and try again.");
      if (pc.connectionState === "disconnected") onState("connecting", "Reconnecting…");
    };

    await pc.setLocalDescription(await pc.createOffer());
    await waitForIce(pc, 2500);

    const csrf = readCookie("va_csrf");
    const res = await fetch(this.opts.offerUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json", ...(csrf ? { "x-csrf-token": csrf } : {}) },
      body: JSON.stringify({ ...this.opts.offerBody, sdp: pc.localDescription?.sdp, type: "offer" }),
    });
    if (!res.ok) {
      let msg = `The server refused the session (${res.status}).`;
      let code = `http_${res.status}`;
      try {
        const body = await res.json();
        msg = body.error?.message ?? body.detail ?? msg;
        code = body.error?.code ?? code;
      } catch {
        /* keep default */
      }
      this.cleanup();
      throw new ApiError(res.status, code, msg);
    }
    const answer = await res.json();
    this.callId = answer.call_id;
    await pc.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
    return answer.call_id as string;
  }

  private handleEvent(ev: VoiceEvent) {
    this.opts.onEvent(ev);
    if (ev.type === "state") {
      const s = ev.state as VoiceState;
      if (s === "ended") this.finish();
      else this.opts.onState(s, typeof ev.detail === "string" ? ev.detail : undefined);
    } else if (ev.type === "ended") {
      this.finish();
    }
  }

  send(msg: Record<string, unknown>) {
    if (this.channel?.readyState === "open") this.channel.send(JSON.stringify(msg));
  }

  interrupt() {
    this.send({ type: "interrupt" });
  }

  setMuted(muted: boolean) {
    this.muted = muted;
    this.mic?.getAudioTracks().forEach((t) => (t.enabled = !muted));
    this.send({ type: "mute", muted });
  }

  async end() {
    if (this.closed) return;
    this.send({ type: "end" });
    if (this.callId && this.opts.endUrl) {
      const csrf = readCookie("va_csrf");
      await fetch(this.opts.endUrl(this.callId), {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json", ...(csrf ? { "x-csrf-token": csrf } : {}) },
        body: JSON.stringify({ type: "end" }),
      }).catch(() => undefined);
    }
    this.finish();
  }

  private fail(message: string) {
    if (this.closed) return;
    this.cleanup();
    this.opts.onState("error", message);
  }

  private finish() {
    if (this.closed) return;
    this.cleanup();
    this.opts.onState("ended");
  }

  cleanup() {
    this.closed = true;
    this.mic?.getTracks().forEach((t) => t.stop());
    this.channel?.close();
    this.pc?.close();
    void this.ctx?.close().catch(() => undefined);
    this.opts.audioElement.srcObject = null;
  }
}

function waitForIce(pc: RTCPeerConnection, timeoutMs: number): Promise<void> {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const t = setTimeout(resolve, timeoutMs);
    pc.addEventListener("icegatheringstatechange", () => {
      if (pc.iceGatheringState === "complete") {
        clearTimeout(t);
        resolve();
      }
    });
  });
}
