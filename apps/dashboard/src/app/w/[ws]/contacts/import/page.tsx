"use client";

import { useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, FileUp } from "lucide-react";
import Link from "next/link";
import { useRef, useState } from "react";
import { toast } from "sonner";

import { ErrorState, Field, NativeSelect, PageHeader, Panel, Spinner } from "@/components/common";
import { Button, buttonVariants } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { post, upload } from "@/lib/api";
import { humanize } from "@/lib/format";
import { useWorkspaceId, useWsApi } from "@/lib/hooks";
import { cn } from "@/lib/utils";

interface Job {
  id: string;
  filename: string;
  row_count: number;
  headers: string[];
  mapping: Record<string, string>;
  preview: string[][];
  targets: string[];
}
interface Validation {
  summary: Record<string, number>;
  rows: { row: number; status: string; errors: string[]; warnings: string[]; data: Record<string, unknown> }[];
  consent_notice: string;
}
interface Report {
  report: Record<string, number> & { invalid_rows?: { row: number; errors: string[] }[] };
}

const STEPS = ["Upload", "Map columns", "Review", "Done"];
const TARGET_LABEL: Record<string, string> = {
  first_name: "First name",
  last_name: "Last name",
  full_name: "Full name",
  phone: "Phone",
  email: "Email",
  company: "Company",
  tags: "Tags",
  notes: "Notes",
  consent_purpose: "Consent: purpose",
  consent_source: "Consent: source",
  consent_evidence: "Consent: evidence",
  consent_obtained_at: "Consent: date",
  dnd_status: "DND status",
};

export default function ImportPage() {
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [custom, setCustom] = useState<string[]>([]);
  const [validation, setValidation] = useState<Validation | null>(null);
  const [updateExisting, setUpdateExisting] = useState(false);
  const [tags, setTags] = useState("");
  const [report, setReport] = useState<Report | null>(null);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const onUpload = (file: File) =>
    run(async () => {
      if (file.size > 10 * 1024 * 1024) throw new Error("The file is larger than 10 MB.");
      const form = new FormData();
      form.append("file", file);
      const j = await upload<Job>(wsApi("/imports"), form);
      setJob(j);
      setMapping(j.mapping);
      setCustom([]);
      setStep(1);
    });

  const onValidate = () =>
    run(async () => {
      if (!job) return;
      const v = await post<Validation>(wsApi(`/imports/${job.id}/validate`), { mapping, custom_columns: custom });
      setValidation(v);
      setStep(2);
    });

  const onCommit = () =>
    run(async () => {
      if (!job) return;
      const r = await post<Report>(wsApi(`/imports/${job.id}/commit`), {
        mapping,
        custom_columns: custom,
        update_existing: updateExisting,
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
      });
      setReport(r);
      setStep(3);
      toast.success(`Imported ${r.report.created ?? 0} contacts`);
      void qc.invalidateQueries({ queryKey: ["contacts", ws] });
    });

  const mapped = new Set(Object.values(mapping).filter(Boolean));
  const hasKey = mapped.has("phone") || mapped.has("email");

  return (
    <>
      <PageHeader title="Import contacts" description="Bring in a spreadsheet of contacts. Nothing is saved until you confirm the final step." />
      <ol className="mb-6 flex flex-wrap gap-x-6 gap-y-2 text-sm" aria-label="Import progress">
        {STEPS.map((s, i) => (
          <li key={s} className={cn("flex items-center gap-2", i === step ? "font-medium" : "text-muted-foreground")} aria-current={i === step ? "step" : undefined}>
            <span
              className={cn(
                "grid size-5 place-items-center rounded-full border text-[11px] tabular-nums",
                i < step && "border-ok bg-ok text-primary-foreground",
                i === step && "border-foreground",
              )}
            >
              {i + 1}
            </span>
            {s}
          </li>
        ))}
      </ol>
      {error ? (
        <div className="mb-4">
          <ErrorState error={error} />
        </div>
      ) : null}

      {step === 0 ? (
        <Panel title="Choose a CSV file" description="UTF-8 CSV up to 10 MB and 50,000 rows. The first row must contain column names.">
          <div
            className="grid place-items-center rounded-lg border border-dashed px-6 py-14 text-center"
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              const f = e.dataTransfer.files[0];
              if (f) void onUpload(f);
            }}
          >
            <FileUp className="mb-3 size-6 text-muted-foreground" />
            <p className="text-sm">Drop a file here or</p>
            <Button className="mt-3" onClick={() => fileRef.current?.click()} disabled={busy}>
              {busy ? <Spinner /> : null} Choose file
            </Button>
            <input
              ref={fileRef}
              type="file"
              accept=".csv,text/csv"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void onUpload(f);
                e.target.value = "";
              }}
            />
          </div>
          <p className="mt-4 text-xs text-muted-foreground">
            Consent is only recorded when your file has columns for consent purpose, source, date and evidence, and each row fills
            them in. Having someone’s number is not consent to call them.
          </p>
        </Panel>
      ) : null}

      {step === 1 && job ? (
        <Panel title={`Map the columns of ${job.filename}`} description={`${job.row_count} rows. Unmapped columns can be kept as custom fields.`}>
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Column in your file</TableHead>
                  <TableHead>Sample values</TableHead>
                  <TableHead className="w-56">Import as</TableHead>
                  <TableHead className="w-28">Custom field</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {job.headers.map((h, i) => (
                  <TableRow key={h}>
                    <TableCell className="font-medium">{h}</TableCell>
                    <TableCell className="max-w-72 truncate text-xs text-muted-foreground">
                      {job.preview.slice(0, 3).map((r) => r[i]).filter(Boolean).join(", ") || "—"}
                    </TableCell>
                    <TableCell>
                      <NativeSelect
                        aria-label={`Import ${h} as`}
                        value={mapping[h] ?? ""}
                        onChange={(e) => {
                          const v = e.target.value;
                          setMapping((m) => {
                            const next = { ...m };
                            for (const k of Object.keys(next)) if (next[k] === v && v) delete next[k];
                            if (v) next[h] = v;
                            else delete next[h];
                            return next;
                          });
                          if (v) setCustom((c) => c.filter((x) => x !== h));
                        }}
                      >
                        <option value="">Don’t import</option>
                        {job.targets.map((t) => (
                          <option key={t} value={t}>
                            {TARGET_LABEL[t] ?? humanize(t)}
                          </option>
                        ))}
                      </NativeSelect>
                    </TableCell>
                    <TableCell>
                      <Checkbox
                        aria-label={`Keep ${h} as a custom field`}
                        disabled={!!mapping[h]}
                        checked={custom.includes(h)}
                        onCheckedChange={(v) => setCustom((c) => (v ? [...c, h] : c.filter((x) => x !== h)))}
                      />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          <div className="mt-5 flex items-center gap-3">
            <Button variant="outline" onClick={() => setStep(0)}>
              Back
            </Button>
            <Button onClick={onValidate} disabled={busy || !hasKey}>
              {busy ? <Spinner /> : null} Check rows
            </Button>
            {!hasKey ? <span className="text-sm text-muted-foreground">Map a phone or email column to continue.</span> : null}
          </div>
        </Panel>
      ) : null}

      {step === 2 && validation ? (
        <div className="grid gap-5">
          <section className="grid grid-cols-2 divide-y rounded-xl border bg-panel sm:grid-cols-5 sm:divide-x sm:divide-y-0 [&>*]:px-5 [&>*]:py-4">
            {(
              [
                ["Will be added", validation.summary.valid],
                ["Already exist", validation.summary.duplicate_existing],
                ["Repeated in file", validation.summary.duplicate_in_file],
                ["Invalid", validation.summary.invalid],
                ["With consent evidence", validation.summary.with_consent],
              ] as const
            ).map(([l, v]) => (
              <div key={l}>
                <p className="text-xs text-muted-foreground">{l}</p>
                <p className="mt-1 text-2xl font-semibold tabular-nums">{v ?? 0}</p>
              </div>
            ))}
          </section>
          <p className="text-sm text-muted-foreground">{validation.consent_notice}</p>
          <Panel title="Rows to look at" description="Valid samples, then rows with errors or warnings." bodyClassName="p-0">
            <div className="max-h-[420px] overflow-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-16 pl-5">Row</TableHead>
                    <TableHead>Result</TableHead>
                    <TableHead>Name</TableHead>
                    <TableHead>Phone</TableHead>
                    <TableHead>Notes</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {validation.rows.map((r) => (
                    <TableRow key={`${r.row}-${r.status}`}>
                      <TableCell className="pl-5 tabular-nums">{r.row}</TableCell>
                      <TableCell className={r.status === "invalid" ? "text-destructive" : r.status === "valid" ? "text-ok" : ""}>
                        {humanize(r.status)}
                      </TableCell>
                      <TableCell>{[r.data.first_name, r.data.last_name].filter(Boolean).join(" ") || "—"}</TableCell>
                      <TableCell className="font-mono text-xs">{String(r.data.phone_e164 ?? "—")}</TableCell>
                      <TableCell className="text-xs">{[...r.errors, ...r.warnings].join("; ") || (r.data.consent ? "Consent will be recorded" : "")}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </Panel>
          <Panel title="Import options">
            <div className="grid max-w-xl gap-4">
              <label className="flex items-center gap-2.5 text-sm">
                <Checkbox checked={updateExisting} onCheckedChange={(v) => setUpdateExisting(!!v)} />
                Update contacts that already exist (names, company and tags are merged)
              </label>
              <Field label="Add tags to every imported contact" htmlFor="imp-tags" hint="Comma separated. A dated import tag is added automatically.">
                <Input id="imp-tags" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="e.g. diwali-2026" />
              </Field>
            </div>
            <div className="mt-5 flex gap-3">
              <Button variant="outline" onClick={() => setStep(1)}>
                Back to mapping
              </Button>
              <Button onClick={onCommit} disabled={busy || (validation.summary.valid === 0 && !(updateExisting && validation.summary.duplicate_existing))}>
                {busy ? <Spinner /> : null} Import contacts
              </Button>
            </div>
          </Panel>
        </div>
      ) : null}

      {step === 3 && report ? (
        <Panel>
          <div className="flex items-start gap-3">
            <CheckCircle2 className="mt-0.5 size-5 text-ok" />
            <div className="text-sm">
              <p className="font-medium">Import finished</p>
              <p className="mt-1 text-muted-foreground">
                {report.report.created} added, {report.report.updated} updated, {report.report.consents_recorded} consent records,{" "}
                {report.report.invalid} invalid rows skipped. The uploaded file has been deleted from this machine.
              </p>
              {report.report.invalid_rows?.length ? (
                <ul className="mt-3 max-h-48 overflow-auto text-xs text-muted-foreground">
                  {report.report.invalid_rows.map((r) => (
                    <li key={r.row}>
                      Row {r.row}: {r.errors.join("; ")}
                    </li>
                  ))}
                </ul>
              ) : null}
              <div className="mt-4 flex gap-2">
                <Link href={`/w/${ws}/contacts`} className={buttonVariants()}>
                  View contacts
                </Link>
                <Button variant="outline" onClick={() => (setStep(0), setJob(null), setValidation(null), setReport(null))}>
                  Import another file
                </Button>
              </div>
            </div>
          </div>
        </Panel>
      ) : null}
    </>
  );
}
