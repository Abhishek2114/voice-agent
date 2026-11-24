"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, Pencil, ShieldCheck, Trash2 } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import {
  ConfirmButton,
  EmptyState,
  ErrorState,
  Field,
  LoadingRows,
  ModeBadge,
  NativeSelect,
  OutcomeBadge,
  PageHeader,
  Panel,
  Spinner,
  StatusLamp,
} from "@/components/common";
import { ContactDialog } from "@/components/contacts/contact-dialog";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { del, errorMessage, get, post } from "@/lib/api";
import { formatDateTime, formatDuration, formatPhone, humanize } from "@/lib/format";
import { useCan, useWorkspaceId, useWsApi } from "@/lib/hooks";
import type { Contact } from "@/lib/types";

interface History {
  calls: { id: string; mode: string; status: string; outcome: string | null; started_at: string; duration_seconds: number | null; summary: string | null }[];
  consents: { id: string; purpose: string; status: string; source: string; obtained_at: string; expires_at: string | null; evidence: string }[];
  callbacks: { id: string; kind: string; preferred_time: string; status: string; created_at: string }[];
  events: { action: string; created_at: string }[];
  suppressed: { reason: string; created_at: string } | null;
}

interface Eligibility {
  allowed: boolean;
  checks: { name: string; passed: boolean; detail: string }[];
}

export default function ContactDetailPage() {
  const { id } = useParams<{ id: string }>();
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const qc = useQueryClient();
  const router = useRouter();
  const canEdit = useCan("editor");
  const canAdmin = useCan("admin");
  const [editing, setEditing] = useState(false);
  const [consentOpen, setConsentOpen] = useState(false);
  const contact = useQuery({ queryKey: ["contact", id], queryFn: () => get<Contact>(wsApi(`/contacts/${id}`)) });
  const history = useQuery({ queryKey: ["contact-history", id], queryFn: () => get<History>(wsApi(`/contacts/${id}/history`)) });
  const [purpose, setPurpose] = useState("promotional");
  const elig = useQuery({
    queryKey: ["eligibility", id, purpose],
    queryFn: () => post<Eligibility>(wsApi("/compliance/eligibility"), { contact_id: id, purpose, mode: "SIMULATION" }),
    enabled: !!contact.data,
  });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["contact", id] });
    void qc.invalidateQueries({ queryKey: ["contact-history", id] });
    void qc.invalidateQueries({ queryKey: ["eligibility", id] });
    void qc.invalidateQueries({ queryKey: ["contacts", ws] });
  };
  const optOut = useMutation({
    mutationFn: () => post(wsApi("/compliance/suppression"), { phone: contact.data?.phone_e164, reason: "opt_out" }),
    onSuccess: () => {
      toast.success("Opt-out recorded. This number won’t be called again.");
      refresh();
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  if (contact.error) return <ErrorState error={contact.error} onRetry={() => contact.refetch()} />;
  if (!contact.data) return <LoadingRows rows={6} />;
  const c = contact.data;
  const h = history.data;

  return (
    <>
      <PageHeader
        title={c.full_name || c.email || formatPhone(c.phone_e164)}
        description={
          <span className="inline-flex flex-wrap gap-x-3">
            <span>{humanize(c.status)}</span>
            <span>Source: {humanize(c.source)}</span>
            {c.is_fictional ? <span>Sample contact</span> : null}
          </span>
        }
        actions={
          canEdit ? (
            <>
              <Button variant="outline" onClick={() => setEditing(true)}>
                <Pencil /> Edit
              </Button>
              {c.phone_e164 && !h?.suppressed ? (
                <ConfirmButton
                  title="Record an opt-out?"
                  description="The number goes on the suppression list, marketing consent is revoked and no campaign will call it again."
                  confirmLabel="Record opt-out"
                  size="default"
                  onConfirm={() => optOut.mutateAsync()}
                >
                  <Ban /> Opt out
                </ConfirmButton>
              ) : null}
              <ConfirmButton
                title="Delete this contact?"
                description="The contact is hidden from lists and campaigns. Use “Erase personal data” to remove transcripts and recordings too."
                confirmLabel="Delete"
                size="default"
                onConfirm={async () => {
                  await del(wsApi(`/contacts/${c.id}`));
                  toast.success("Contact deleted");
                  router.push(`/w/${ws}/contacts`);
                }}
              >
                <Trash2 /> Delete
              </ConfirmButton>
            </>
          ) : null
        }
      />
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <div className="grid content-start gap-5">
          <Panel title="Details">
            <dl className="grid grid-cols-[7rem_1fr] gap-y-2.5 text-sm">
              <dt className="text-muted-foreground">Phone</dt>
              <dd className="font-mono text-xs leading-5">{c.phone_e164 ? formatPhone(c.phone_e164) : c.phone_raw ? `${c.phone_raw} (invalid)` : "—"}</dd>
              <dt className="text-muted-foreground">Email</dt>
              <dd>{c.email ?? "—"}</dd>
              <dt className="text-muted-foreground">Company</dt>
              <dd>{c.company ?? "—"}</dd>
              <dt className="text-muted-foreground">DND status</dt>
              <dd>
                {humanize(c.dnd_status)}
                {c.dnd_checked_at ? <span className="text-muted-foreground"> (checked {formatDateTime(c.dnd_checked_at)})</span> : null}
              </dd>
              <dt className="text-muted-foreground">Tags</dt>
              <dd>{c.tags.length ? c.tags.join(", ") : "—"}</dd>
              {Object.entries(c.custom_fields).map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="truncate text-muted-foreground">{humanize(k)}</dt>
                  <dd className="break-words">{v}</dd>
                </div>
              ))}
            </dl>
            {c.notes ? <p className="mt-4 border-t pt-4 text-sm whitespace-pre-wrap">{c.notes}</p> : null}
          </Panel>

          <Panel
            title="Can we call this person?"
            description="Live result of the workspace’s compliance checks. Any failed check blocks campaign calls."
            actions={
              <NativeSelect value={purpose} onChange={(e) => setPurpose(e.target.value)} className="h-7 w-40 text-xs" aria-label="Call purpose">
                <option value="promotional">Promotional</option>
                <option value="service">Service</option>
                <option value="transactional">Transactional</option>
                <option value="inquiry_followup">Inquiry follow-up</option>
              </NativeSelect>
            }
          >
            {elig.data ? (
              <>
                <p className="mb-3 text-sm font-medium">
                  <StatusLamp state={elig.data.allowed ? "ok" : "unavailable"} label={elig.data.allowed ? "Eligible right now" : "Not eligible"} />
                </p>
                <ul className="grid gap-1.5 text-sm">
                  {elig.data.checks.map((ch) => (
                    <li key={ch.name} className="flex gap-2">
                      <span className={ch.passed ? "text-ok" : "text-destructive"} aria-label={ch.passed ? "passed" : "failed"}>
                        {ch.passed ? "✓" : "✕"}
                      </span>
                      <span className={ch.passed ? "text-muted-foreground" : ""}>{ch.detail}</span>
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <LoadingRows rows={3} />
            )}
          </Panel>

          <Panel
            title="Consent records"
            actions={
              canEdit ? (
                <Button variant="outline" size="sm" onClick={() => setConsentOpen(true)}>
                  <ShieldCheck /> Record consent
                </Button>
              ) : null
            }
            bodyClassName="p-0"
          >
            {!h ? (
              <div className="p-5">
                <LoadingRows rows={2} />
              </div>
            ) : h.consents.length === 0 ? (
              <EmptyState title="No consent on record">A phone number alone is not consent to be called.</EmptyState>
            ) : (
              <ul className="divide-y text-sm">
                {h.consents.map((r) => (
                  <li key={r.id} className="px-5 py-3">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium">{humanize(r.purpose)}</span>
                      <StatusLamp state={r.status === "granted" ? "ok" : "idle"} label={humanize(r.status)} className="text-xs" />
                    </div>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      {humanize(r.source)}, {formatDateTime(r.obtained_at)}
                      {r.expires_at ? `, expires ${formatDateTime(r.expires_at)}` : ""}
                    </p>
                    <p className="mt-1 text-xs">{r.evidence}</p>
                  </li>
                ))}
              </ul>
            )}
            {h?.suppressed ? (
              <p className="border-t px-5 py-3 text-sm text-destructive">
                On the suppression list ({humanize(h.suppressed.reason)}) since {formatDateTime(h.suppressed.created_at)}.
              </p>
            ) : null}
          </Panel>
        </div>

        <div className="grid content-start gap-5">
          <Panel title="Conversations" bodyClassName="p-0">
            {!h ? (
              <div className="p-5">
                <LoadingRows rows={3} />
              </div>
            ) : h.calls.length === 0 ? (
              <EmptyState title="No conversations yet" />
            ) : (
              <ul className="divide-y">
                {h.calls.map((call) => (
                  <li key={call.id}>
                    <Link href={`/w/${ws}/calls/${call.id}`} className="block px-5 py-3 text-sm hover:bg-muted/40">
                      <div className="flex items-center gap-3">
                        <ModeBadge mode={call.mode} />
                        <OutcomeBadge outcome={call.outcome} />
                        <span className="ml-auto text-xs text-muted-foreground">
                          {formatDateTime(call.started_at)}, {formatDuration(call.duration_seconds)}
                        </span>
                      </div>
                      {call.summary ? <p className="mt-1.5 line-clamp-2 text-muted-foreground">{call.summary}</p> : null}
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
          {h && h.callbacks.length > 0 ? (
            <Panel title="Callback requests" bodyClassName="p-0">
              <ul className="divide-y text-sm">
                {h.callbacks.map((cb) => (
                  <li key={cb.id} className="flex items-center gap-3 px-5 py-3">
                    <span className="font-medium">{humanize(cb.kind)}</span>
                    <span className="flex-1 text-muted-foreground">{cb.preferred_time}</span>
                    <span className="text-xs">{humanize(cb.status)}</span>
                  </li>
                ))}
              </ul>
            </Panel>
          ) : null}
          <Panel title="Activity" bodyClassName="p-0">
            {h && h.events.length ? (
              <ol className="divide-y text-sm">
                {h.events.map((ev, i) => (
                  <li key={i} className="flex justify-between gap-3 px-5 py-2.5">
                    <span>{humanize(ev.action.replace(".", " "))}</span>
                    <span className="text-muted-foreground">{formatDateTime(ev.created_at)}</span>
                  </li>
                ))}
              </ol>
            ) : (
              <EmptyState title="No activity recorded" />
            )}
          </Panel>
          {canAdmin ? (
            <Panel title="Erase personal data" description="Removes this contact, their transcripts, recordings, consent records and callback requests. The number stays on the suppression list so they are never called again.">
              <ConfirmButton
                title="Erase all data for this person?"
                description="This cannot be undone. Call records remain only as anonymous counts for analytics."
                confirmLabel="Erase data"
                onConfirm={async () => {
                  try {
                    const r = await post<{ calls_scrubbed: number; recordings_deleted: number }>(wsApi(`/contacts/${c.id}/erase`));
                    toast.success(`Erased. ${r.calls_scrubbed} call(s) scrubbed, ${r.recordings_deleted} recording(s) deleted.`);
                    router.push(`/w/${ws}/contacts`);
                  } catch (e) {
                    toast.error(errorMessage(e));
                  }
                }}
              >
                Erase personal data
              </ConfirmButton>
            </Panel>
          ) : null}
        </div>
      </div>
      <ContactDialog open={editing} onOpenChange={setEditing} contact={c} baseUrl={wsApi("/contacts")} onSaved={(saved) => (qc.setQueryData(["contact", id], saved), refresh())} />
      <ConsentDialog open={consentOpen} onOpenChange={setConsentOpen} contactId={c.id} onSaved={refresh} />
    </>
  );
}

function ConsentDialog({ open, onOpenChange, contactId, onSaved }: { open: boolean; onOpenChange: (v: boolean) => void; contactId: string; onSaved: () => void }) {
  const wsApi = useWsApi();
  const [purpose, setPurpose] = useState("promotional");
  const [source, setSource] = useState("web_form");
  const [evidence, setEvidence] = useState("");
  const [obtained, setObtained] = useState(new Date().toISOString().slice(0, 10));
  const [expires, setExpires] = useState("");
  const save = useMutation({
    mutationFn: () =>
      post(wsApi("/compliance/consents"), {
        contact_id: contactId,
        purpose,
        source,
        evidence,
        obtained_at: new Date(obtained).toISOString(),
        expires_at: expires ? new Date(expires).toISOString() : null,
      }),
    onSuccess: () => {
      toast.success("Consent recorded");
      setEvidence("");
      onSaved();
      onOpenChange(false);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Record consent</DialogTitle>
        </DialogHeader>
        <div className="grid gap-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Purpose" htmlFor="cp">
              <NativeSelect id="cp" value={purpose} onChange={(e) => setPurpose(e.target.value)}>
                <option value="promotional">Promotional calls</option>
                <option value="service">Service calls</option>
                <option value="transactional">Transactional calls</option>
                <option value="inquiry_followup">Follow-up on their inquiry</option>
              </NativeSelect>
            </Field>
            <Field label="How it was given" htmlFor="cs">
              <NativeSelect id="cs" value={source} onChange={(e) => setSource(e.target.value)}>
                <option value="web_form">Web form</option>
                <option value="written">Written / signed</option>
                <option value="verbal_call">Verbally on a call</option>
                <option value="digital_consent">Registered digital consent</option>
                <option value="customer_inquiry">Customer inquiry</option>
                <option value="other">Other</option>
              </NativeSelect>
            </Field>
            <Field label="Date given" htmlFor="co">
              <Input id="co" type="date" value={obtained} onChange={(e) => setObtained(e.target.value)} />
            </Field>
            <Field label="Expires (optional)" htmlFor="ce">
              <Input id="ce" type="date" value={expires} onChange={(e) => setExpires(e.target.value)} />
            </Field>
          </div>
          <Field label="Evidence" htmlFor="cev" hint="Where the proof is kept, e.g. form submission ID or recording reference.">
            <Textarea id="cev" rows={3} value={evidence} onChange={(e) => setEvidence(e.target.value)} />
          </Field>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={() => save.mutate()} disabled={save.isPending || evidence.trim().length < 3}>
            {save.isPending ? <Spinner /> : null} Record consent
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
