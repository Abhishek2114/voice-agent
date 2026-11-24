"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, FileUp, Plus, Search, Trash2, X } from "lucide-react";
import { useRef, useState } from "react";
import { toast } from "sonner";

import { ConfirmButton, EmptyState, ErrorState, Field, LoadingRows, NativeSelect, PageHeader, Panel, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { del, errorMessage, get, post, put, upload } from "@/lib/api";
import { formatBytes, formatRelative, humanize } from "@/lib/format";
import { useCan, useWorkspaceId, useWsApi } from "@/lib/hooks";
import type { KnowledgeDoc } from "@/lib/types";

const KINDS = ["faq", "business_info", "product", "service", "pricing", "policy", "document"];
const KIND_LABEL: Record<string, string> = {
  faq: "FAQ",
  business_info: "Business information",
  product: "Product",
  service: "Service",
  pricing: "Pricing",
  policy: "Policy",
  document: "Document",
};

export default function KnowledgePage() {
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const qc = useQueryClient();
  const canEdit = useCan("editor");
  const fileRef = useRef<HTMLInputElement>(null);
  const [kind, setKind] = useState("");
  const [editor, setEditor] = useState<{ doc?: KnowledgeDoc; faq: boolean } | null>(null);
  const [q, setQ] = useState("");
  const [uploadKind, setUploadKind] = useState("document");
  const docs = useQuery({
    queryKey: ["knowledge", ws, kind],
    queryFn: () => get<{ items: KnowledgeDoc[]; counts: Record<string, number> }>(wsApi("/knowledge"), { kind }),
  });
  const search = useMutation({
    mutationFn: (query: string) => get<{ title: string; content: string; score: number }[]>(wsApi("/knowledge/search"), { q: query }),
  });
  const refresh = () => void qc.invalidateQueries({ queryKey: ["knowledge", ws] });
  const doUpload = useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      form.append("kind", uploadKind);
      return upload<KnowledgeDoc>(wsApi("/knowledge/upload"), form);
    },
    onSuccess: (d) => {
      toast.success(`Added “${d.title}” (${d.chunk_count} sections indexed)`);
      refresh();
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  return (
    <>
      <PageHeader
        title="Knowledge"
        description="What agents in this workspace can look up during calls: FAQs, products, prices and policies. It stays on this machine and is never shared with other workspaces."
        actions={
          canEdit ? (
            <>
              <NativeSelect value={uploadKind} onChange={(e) => setUploadKind(e.target.value)} className="w-44" aria-label="Type for uploaded file">
                {KINDS.filter((k) => k !== "faq").map((k) => (
                  <option key={k} value={k}>
                    Upload as {KIND_LABEL[k].toLowerCase()}
                  </option>
                ))}
              </NativeSelect>
              <Button variant="outline" onClick={() => fileRef.current?.click()} disabled={doUpload.isPending}>
                {doUpload.isPending ? <Spinner /> : <FileUp />} Upload file
              </Button>
              <input
                ref={fileRef}
                type="file"
                accept=".txt,.md,.markdown,.csv,.pdf"
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) doUpload.mutate(f);
                  e.target.value = "";
                }}
              />
              <Button variant="outline" onClick={() => setEditor({ faq: true })}>
                <Plus /> FAQ
              </Button>
              <Button onClick={() => setEditor({ faq: false })}>
                <Plus /> Write an entry
              </Button>
            </>
          ) : null
        }
      />

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <div>
          <div className="mb-3 flex flex-wrap gap-1.5" role="radiogroup" aria-label="Filter by type">
            {["", ...KINDS].map((k) => (
              <button
                key={k || "all"}
                role="radio"
                aria-checked={kind === k}
                onClick={() => setKind(k)}
                className={`h-7 rounded-md border px-2.5 text-xs ${kind === k ? "border-foreground bg-foreground text-background" : "text-muted-foreground hover:text-foreground"}`}
              >
                {k ? KIND_LABEL[k] : "All"}
                {k && docs.data?.counts[k] ? <span className="ml-1 tabular-nums opacity-70">{docs.data.counts[k]}</span> : null}
              </button>
            ))}
          </div>
          <div className="rounded-xl border bg-panel">
            {docs.error ? (
              <div className="p-5">
                <ErrorState error={docs.error} onRetry={() => docs.refetch()} />
              </div>
            ) : !docs.data ? (
              <div className="p-5">
                <LoadingRows />
              </div>
            ) : docs.data.items.length === 0 ? (
              <EmptyState icon={<BookOpen className="size-6" />} title="Nothing here yet">
                Add FAQs, pricing or policies. Agents only quote prices and policies that appear here or in their own instructions.
              </EmptyState>
            ) : (
              <ul className="divide-y">
                {docs.data.items.map((d) => (
                  <li key={d.id} className="flex items-start gap-4 px-5 py-3.5">
                    <button type="button" className="min-w-0 flex-1 text-left" onClick={() => canEdit && setEditor({ doc: d, faq: false })} disabled={!canEdit}>
                      <p className="font-medium">{d.title}</p>
                      <p className="mt-0.5 line-clamp-2 text-sm text-muted-foreground">{d.preview}</p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {KIND_LABEL[d.kind] ?? humanize(d.kind)}, {d.chunk_count} section{d.chunk_count === 1 ? "" : "s"}, {formatBytes(d.size_bytes)}
                        {d.source_filename ? `, from ${d.source_filename}` : ""}, updated {formatRelative(d.updated_at)}
                      </p>
                    </button>
                    {canEdit ? (
                      <ConfirmButton
                        title={`Remove “${d.title}”?`}
                        description="Agents will no longer be able to look this up."
                        confirmLabel="Remove"
                        variant="ghost"
                        size="icon-sm"
                        onConfirm={async () => {
                          await del(wsApi(`/knowledge/${d.id}`));
                          toast.success("Removed");
                          refresh();
                        }}
                      >
                        <Trash2 />
                        <span className="sr-only">Remove {d.title}</span>
                      </ConfirmButton>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <Panel title="Try a lookup" description="See what an agent would retrieve for a caller’s question.">
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (q.trim()) search.mutate(q.trim());
            }}
          >
            <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. do you offer refunds?" aria-label="Question" />
            <Button type="submit" variant="outline" disabled={search.isPending}>
              <Search />
            </Button>
          </form>
          <div className="mt-4">
            {search.error ? (
              <ErrorState error={search.error} />
            ) : search.data ? (
              search.data.length === 0 ? (
                <p className="text-sm text-muted-foreground">No match. The agent would say it doesn’t know and offer a follow-up.</p>
              ) : (
                <ol className="grid gap-3">
                  {search.data.map((h, i) => (
                    <li key={i} className="rounded-lg border p-3 text-sm">
                      <p className="text-xs font-medium">{h.title}</p>
                      <p className="mt-1 line-clamp-6 whitespace-pre-wrap text-muted-foreground">{h.content}</p>
                    </li>
                  ))}
                </ol>
              )
            ) : null}
          </div>
        </Panel>
      </div>

      {editor ? <DocEditor doc={editor.doc} faq={editor.faq} onClose={() => setEditor(null)} onSaved={refresh} /> : null}
    </>
  );
}

function DocEditor({ doc, faq, onClose, onSaved }: { doc?: KnowledgeDoc; faq: boolean; onClose: () => void; onSaved: () => void }) {
  const wsApi = useWsApi();
  const full = useQuery({ queryKey: ["knowledge-doc", doc?.id], queryFn: () => get<KnowledgeDoc>(wsApi(`/knowledge/${doc!.id}`)), enabled: !!doc });
  const [title, setTitle] = useState(doc?.title ?? (faq ? "Frequently asked questions" : ""));
  const [kind, setKind] = useState(doc?.kind ?? (faq ? "faq" : "business_info"));
  const [content, setContent] = useState<string | null>(null);
  const [pairs, setPairs] = useState<{ question: string; answer: string }[]>([{ question: "", answer: "" }]);
  const text = content ?? full.data?.content ?? "";
  const save = useMutation({
    mutationFn: () => {
      const body = faq
        ? { title, kind: "faq", faq: pairs.filter((p) => p.question.trim() && p.answer.trim()) }
        : { title, kind, content: text };
      return doc ? put(wsApi(`/knowledge/${doc.id}`), body) : post(wsApi("/knowledge"), body);
    },
    onSuccess: () => {
      toast.success("Saved and indexed");
      onSaved();
      onClose();
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{doc ? "Edit entry" : faq ? "Add FAQs" : "New entry"}</DialogTitle>
        </DialogHeader>
        <div className="grid gap-4">
          <div className="grid gap-3 sm:grid-cols-[1fr_12rem]">
            <Field label="Title" htmlFor="k-title">
              <Input id="k-title" value={title} onChange={(e) => setTitle(e.target.value)} />
            </Field>
            {!faq ? (
              <Field label="Type" htmlFor="k-kind">
                <NativeSelect id="k-kind" value={kind} onChange={(e) => setKind(e.target.value)}>
                  {KINDS.map((k) => (
                    <option key={k} value={k}>
                      {KIND_LABEL[k]}
                    </option>
                  ))}
                </NativeSelect>
              </Field>
            ) : null}
          </div>
          {faq ? (
            <div className="grid max-h-[50vh] gap-3 overflow-y-auto">
              {pairs.map((p, i) => (
                <div key={i} className="grid gap-2 rounded-lg border p-3">
                  <div className="flex gap-2">
                    <Input
                      aria-label={`Question ${i + 1}`}
                      placeholder="Question"
                      value={p.question}
                      onChange={(e) => setPairs((ps) => ps.map((x, j) => (j === i ? { ...x, question: e.target.value } : x)))}
                    />
                    <Button variant="ghost" size="icon" aria-label="Remove question" onClick={() => setPairs((ps) => ps.filter((_, j) => j !== i))}>
                      <X />
                    </Button>
                  </div>
                  <Textarea
                    aria-label={`Answer ${i + 1}`}
                    placeholder="Answer"
                    rows={2}
                    value={p.answer}
                    onChange={(e) => setPairs((ps) => ps.map((x, j) => (j === i ? { ...x, answer: e.target.value } : x)))}
                  />
                </div>
              ))}
              <div>
                <Button variant="outline" size="sm" onClick={() => setPairs((ps) => [...ps, { question: "", answer: "" }])}>
                  <Plus /> Another question
                </Button>
              </div>
            </div>
          ) : full.isLoading ? (
            <LoadingRows rows={4} />
          ) : (
            <Field label="Content" htmlFor="k-content" hint="Plain text or Markdown. Separate topics with blank lines.">
              <Textarea id="k-content" rows={14} value={text} onChange={(e) => setContent(e.target.value)} className="font-mono text-xs" />
            </Field>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={() => save.mutate()} disabled={save.isPending || !title.trim()}>
            {save.isPending ? <Spinner /> : null} Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
