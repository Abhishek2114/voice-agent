"use client";

import { AlertTriangle, Inbox, Loader2, RotateCw } from "lucide-react";
import { useState, type ReactNode, type SelectHTMLAttributes } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api";
import { humanize, outcomeLabel } from "@/lib/format";
import { cn } from "@/lib/utils";

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-4 pb-6">
      <div className="min-w-0 max-w-2xl">
        <h1 className="text-[1.65rem] leading-tight font-semibold tracking-[-0.015em]">{title}</h1>
        {description ? <p className="mt-1.5 text-sm text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}

export function Panel({
  title,
  description,
  actions,
  children,
  className,
  bodyClassName,
}: {
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={cn("rounded-xl border bg-panel", className)}>
      {title || actions ? (
        <div className="flex items-start justify-between gap-3 border-b px-5 py-3.5">
          <div className="min-w-0">
            {title ? <h2 className="text-[0.95rem] font-semibold">{title}</h2> : null}
            {description ? <p className="mt-0.5 text-xs text-muted-foreground">{description}</p> : null}
          </div>
          {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
        </div>
      ) : null}
      <div className={cn("p-5", bodyClassName)}>{children}</div>
    </section>
  );
}

export function EmptyState({
  icon,
  title,
  children,
  action,
}: {
  icon?: ReactNode;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-14 text-center">
      <div className="mb-1 text-muted-foreground">{icon ?? <Inbox className="size-6" />}</div>
      <p className="font-medium">{title}</p>
      {children ? <p className="max-w-md text-sm text-muted-foreground">{children}</p> : null}
      {action ? <div className="mt-3">{action}</div> : null}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div role="alert" className="flex items-start gap-3 rounded-lg border border-destructive/40 bg-destructive/5 p-4">
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
      <div className="min-w-0 flex-1 text-sm">
        <p className="font-medium">Couldn’t load this</p>
        <p className="mt-0.5 text-muted-foreground">{errorMessage(error)}</p>
      </div>
      {onRetry ? (
        <Button variant="outline" size="sm" onClick={onRetry}>
          <RotateCw /> Retry
        </Button>
      ) : null}
    </div>
  );
}

export function LoadingRows({ rows = 5, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("space-y-2.5", className)} aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-9 w-full" style={{ opacity: 1 - i * 0.12 }} />
      ))}
    </div>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn("size-4 animate-spin", className)} aria-hidden />;
}

type LampState = "ok" | "degraded" | "unavailable" | "disabled" | "live" | "idle";

export function StatusLamp({ state, label, className }: { state: LampState | string; label?: string; className?: string }) {
  const color =
    state === "ok"
      ? "bg-ok"
      : state === "live"
        ? "bg-signal lamp-live"
        : state === "degraded"
          ? "bg-warn"
          : state === "unavailable"
            ? "bg-destructive"
            : "bg-muted-foreground/40";
  return (
    <span className={cn("inline-flex items-center gap-2", className)}>
      <span className={cn("inline-block size-2 shrink-0 rounded-full", color)} aria-hidden />
      {label ? <span>{label}</span> : <span className="sr-only">{state}</span>}
    </span>
  );
}

const MODE_TEXT: Record<string, string> = { WEBRTC: "Browser", SIMULATION: "Simulation", PSTN: "Phone" };

/** Call-mode badge. Simulations are hatched so they can never be mistaken for real calls. */
export function ModeBadge({ mode, className }: { mode: string; className?: string }) {
  return (
    <span
      title={mode === "SIMULATION" ? "Simulated call: no real phone call was placed" : undefined}
      className={cn(
        "inline-flex h-5 items-center rounded-[4px] border px-1.5 text-[11px] font-medium whitespace-nowrap",
        mode === "SIMULATION" && "sim-hatch border-dashed text-muted-foreground",
        mode === "WEBRTC" && "border-ok/40 text-ok",
        mode === "PSTN" && "border-signal/60 bg-signal/10 text-foreground",
        className,
      )}
    >
      {MODE_TEXT[mode] ?? mode}
    </span>
  );
}

const POSITIVE = new Set(["interested", "callback_requested", "appointment_requested"]);
const NEGATIVE = new Set(["failed", "opted_out", "wrong_number"]);

export function OutcomeBadge({ outcome }: { outcome: string | null }) {
  return (
    <span
      className={cn(
        "inline-flex h-5 items-center gap-1.5 rounded-full px-2 text-[11px] font-medium whitespace-nowrap",
        !outcome && "text-muted-foreground",
        outcome && POSITIVE.has(outcome) && "bg-ok/12 text-ok",
        outcome && NEGATIVE.has(outcome) && "bg-destructive/10 text-destructive",
        outcome && !POSITIVE.has(outcome) && !NEGATIVE.has(outcome) && "bg-muted text-foreground/80",
      )}
    >
      {outcomeLabel(outcome)}
    </span>
  );
}

export function StatusText({ status }: { status: string }) {
  const tone =
    status === "completed" || status === "running"
      ? "ok"
      : status === "failed"
        ? "unavailable"
        : status === "in_progress" || status === "connecting"
          ? "live"
          : status === "paused" || status === "scheduled"
            ? "degraded"
            : "idle";
  return <StatusLamp state={tone} label={humanize(status)} className="text-sm" />;
}

export function Field({
  label,
  htmlFor,
  hint,
  error,
  children,
  className,
}: {
  label: string;
  htmlFor?: string;
  hint?: ReactNode;
  error?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("grid gap-1.5", className)}>
      <label htmlFor={htmlFor} className="text-sm font-medium">
        {label}
      </label>
      {children}
      {error ? (
        <p className="text-xs text-destructive" role="alert">
          {error}
        </p>
      ) : hint ? (
        <p className="text-xs text-muted-foreground">{hint}</p>
      ) : null}
    </div>
  );
}

export function NativeSelect({ className, children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cn(
        "h-8 w-full appearance-none rounded-lg border border-input bg-transparent bg-[length:12px] bg-[right_0.6rem_center] bg-no-repeat pr-8 pl-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50 dark:bg-input/30",
        "bg-[url('data:image/svg+xml;utf8,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 12 12%22><path d=%22M2.5 4.5 6 8l3.5-3.5%22 fill=%22none%22 stroke=%22%2395a39f%22 stroke-width=%221.5%22/></svg>')]",
        className,
      )}
      {...props}
    >
      {children}
    </select>
  );
}

export function ConfirmButton({
  title,
  description,
  confirmLabel,
  onConfirm,
  destructive = true,
  children,
  variant = "outline",
  size = "sm",
  disabled,
}: {
  title: string;
  description: ReactNode;
  confirmLabel: string;
  onConfirm: () => Promise<unknown> | void;
  destructive?: boolean;
  children: ReactNode;
  variant?: "outline" | "ghost" | "destructive" | "default" | "secondary";
  size?: "sm" | "default" | "xs" | "icon-sm";
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  return (
    <>
      <Button variant={variant} size={size} onClick={() => setOpen(true)} disabled={disabled}>
        {children}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>{description}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose render={<Button variant="outline">Keep</Button>} />
            <Button
              variant={destructive ? "destructive" : "default"}
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await onConfirm();
                  setOpen(false);
                } finally {
                  setBusy(false);
                }
              }}
            >
              {busy ? <Spinner /> : null}
              {confirmLabel}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export function Pagination({
  page,
  pageSize,
  total,
  onPage,
}: {
  page: number;
  pageSize: number;
  total: number;
  onPage: (p: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (total <= pageSize) return null;
  const from = (page - 1) * pageSize + 1;
  const to = Math.min(total, page * pageSize);
  return (
    <nav className="flex items-center justify-between gap-3 pt-4 text-sm" aria-label="Pagination">
      <span className="text-muted-foreground">
        {from}–{to} of {total}
      </span>
      <div className="flex gap-2">
        <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>
          Previous
        </Button>
        <Button variant="outline" size="sm" disabled={page >= pages} onClick={() => onPage(page + 1)}>
          Next
        </Button>
      </div>
    </nav>
  );
}

export function Stat({ label, value, sub, className }: { label: string; value: ReactNode; sub?: ReactNode; className?: string }) {
  return (
    <div className={cn("min-w-0", className)}>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-2xl font-semibold tracking-tight tabular-nums">{value}</p>
      {sub ? <p className="mt-0.5 text-xs text-muted-foreground">{sub}</p> : null}
    </div>
  );
}
