export function ProgressBar({ progress }: { progress: Record<string, number> }) {
  const total = progress.total || 0;
  const done = (progress.completed ?? 0) + (progress.exhausted ?? 0);
  const skipped = (progress.skipped ?? 0) + (progress.failed ?? 0) + (progress.cancelled ?? 0);
  const active = (progress.in_progress ?? 0) + (progress.queued ?? 0);
  const pct = (n: number) => (total ? `${(n / total) * 100}%` : "0%");
  return (
    <div>
      <div className="flex h-1.5 overflow-hidden rounded-full bg-muted" role="img" aria-label={`${done} of ${total} contacts done`}>
        <span className="bg-ok" style={{ width: pct(done) }} />
        <span className="bg-signal" style={{ width: pct(active) }} />
        <span className="bg-muted-foreground/40" style={{ width: pct(skipped) }} />
      </div>
      <p className="mt-1.5 text-xs text-muted-foreground tabular-nums">
        {done} done, {active} in progress, {progress.pending ?? 0} waiting, {skipped} skipped of {total}
      </p>
    </div>
  );
}
