"use client";

import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Plus, Search, Tag, Upload, Users } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { ContactDialog, CONTACT_STATUSES } from "@/components/contacts/contact-dialog";
import { EmptyState, ErrorState, LoadingRows, NativeSelect, PageHeader, Pagination } from "@/components/common";
import { Button, buttonVariants } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { download, errorMessage, get, post } from "@/lib/api";
import { formatPhone, formatRelative, humanize } from "@/lib/format";
import { useCan, useWorkspace, useWorkspaceId, useWsApi } from "@/lib/hooks";
import type { Contact, Page } from "@/lib/types";

export default function ContactsPage() {
  const ws = useWorkspaceId();
  const wsApi = useWsApi();
  const qc = useQueryClient();
  const router = useRouter();
  const canEdit = useCan("editor");
  const canExport = useCan("admin");
  const workspace = useWorkspace();
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [status, setStatus] = useState("");
  const [tag, setTag] = useState("");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [adding, setAdding] = useState(false);
  const [tagInput, setTagInput] = useState("");

  useEffect(() => {
    const t = setTimeout(() => {
      setDebounced(q);
      setPage(1);
    }, 250);
    return () => clearTimeout(t);
  }, [q]);

  const key = ["contacts", ws, debounced, status, tag, page];
  const contacts = useQuery({
    queryKey: key,
    queryFn: () => get<Page<Contact> & { tags: string[] }>(wsApi("/contacts"), { q: debounced, status, tag, page, page_size: 25 }),
    placeholderData: keepPreviousData,
  });
  const items = contacts.data?.items ?? [];
  const allChecked = items.length > 0 && items.every((c) => selected.has(c.id));

  const bulkTag = async () => {
    if (!tagInput.trim() || selected.size === 0) return;
    try {
      const r = await post<{ updated: number }>(wsApi("/contacts/bulk-tags"), { contact_ids: [...selected], add: [tagInput.trim()] });
      toast.success(`Tagged ${r.updated} contact${r.updated === 1 ? "" : "s"}`);
      setTagInput("");
      setSelected(new Set());
      void qc.invalidateQueries({ queryKey: ["contacts", ws] });
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  return (
    <>
      <PageHeader
        title="Contacts"
        description="People and leads in this workspace. Leads captured by agents during conversations appear here automatically."
        actions={
          <>
            {canExport ? (
              <Button
                variant="outline"
                onClick={() => download(wsApi("/contacts/export"), `${workspace.data?.slug ?? "contacts"}.csv`, { q: debounced, status, tag }).catch((e) => toast.error(errorMessage(e)))}
              >
                <Download /> Export
              </Button>
            ) : null}
            {canEdit ? (
              <>
                <Link href={`/w/${ws}/contacts/import`} className={buttonVariants({ variant: "outline" })}>
                  <Upload /> Import CSV
                </Link>
                <Button onClick={() => setAdding(true)}>
                  <Plus /> Add contact
                </Button>
              </>
            ) : null}
          </>
        }
      />

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="relative w-full max-w-xs">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Name, phone, email, company" className="pl-8" aria-label="Search contacts" />
        </div>
        <NativeSelect value={status} onChange={(e) => (setStatus(e.target.value), setPage(1))} className="w-44" aria-label="Status filter">
          <option value="">All statuses</option>
          {CONTACT_STATUSES.map((s) => (
            <option key={s} value={s}>
              {humanize(s)}
            </option>
          ))}
        </NativeSelect>
        <NativeSelect value={tag} onChange={(e) => (setTag(e.target.value), setPage(1))} className="w-40" aria-label="Tag filter">
          <option value="">All tags</option>
          {contacts.data?.tags.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </NativeSelect>
        {selected.size > 0 && canEdit ? (
          <div className="ml-auto flex items-center gap-2 text-sm">
            <span className="text-muted-foreground">{selected.size} selected</span>
            <Input value={tagInput} onChange={(e) => setTagInput(e.target.value)} placeholder="Tag" className="w-32" aria-label="Tag to add" />
            <Button variant="outline" size="sm" onClick={bulkTag}>
              <Tag /> Add tag
            </Button>
          </div>
        ) : null}
      </div>

      <div className="rounded-xl border bg-panel">
        {contacts.error ? (
          <div className="p-5">
            <ErrorState error={contacts.error} onRetry={() => contacts.refetch()} />
          </div>
        ) : !contacts.data ? (
          <div className="p-5">
            <LoadingRows rows={8} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState icon={<Users className="size-6" />} title={debounced || status || tag ? "No contacts match" : "No contacts yet"}>
            {debounced || status || tag ? "Try a different search or filter." : "Add contacts one by one, import a CSV, or let agents capture leads during calls."}
          </EmptyState>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10 pl-4">
                  <Checkbox
                    aria-label="Select all on this page"
                    checked={allChecked}
                    onCheckedChange={(v) => setSelected(v ? new Set(items.map((c) => c.id)) : new Set())}
                  />
                </TableHead>
                <TableHead>Name</TableHead>
                <TableHead>Phone</TableHead>
                <TableHead className="hidden md:table-cell">Company</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="hidden lg:table-cell">Tags</TableHead>
                <TableHead className="hidden text-right sm:table-cell">Added</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((c) => (
                <TableRow key={c.id} className="cursor-pointer" onClick={() => router.push(`/w/${ws}/contacts/${c.id}`)}>
                  <TableCell className="pl-4" onClick={(e) => e.stopPropagation()}>
                    <Checkbox
                      aria-label={`Select ${c.full_name || c.phone_e164}`}
                      checked={selected.has(c.id)}
                      onCheckedChange={(v) =>
                        setSelected((s) => {
                          const n = new Set(s);
                          if (v) n.add(c.id);
                          else n.delete(c.id);
                          return n;
                        })
                      }
                    />
                  </TableCell>
                  <TableCell className="font-medium">
                    <Link href={`/w/${ws}/contacts/${c.id}`} onClick={(e) => e.stopPropagation()} className="hover:underline">
                      {c.full_name || c.email || "Unnamed"}
                    </Link>
                    {c.is_fictional ? <span className="ml-2 text-xs text-muted-foreground">sample</span> : null}
                  </TableCell>
                  <TableCell className="font-mono text-xs">{c.phone_e164 ? formatPhone(c.phone_e164) : <span className="text-muted-foreground">—</span>}</TableCell>
                  <TableCell className="hidden max-w-48 truncate md:table-cell">{c.company ?? "—"}</TableCell>
                  <TableCell>{humanize(c.status)}</TableCell>
                  <TableCell className="hidden lg:table-cell">
                    <span className="flex flex-wrap gap-1">
                      {c.tags.slice(0, 3).map((t) => (
                        <span key={t} className="rounded bg-muted px-1.5 py-0.5 text-[11px]">
                          {t}
                        </span>
                      ))}
                      {c.tags.length > 3 ? <span className="text-[11px] text-muted-foreground">+{c.tags.length - 3}</span> : null}
                    </span>
                  </TableCell>
                  <TableCell className="hidden text-right text-muted-foreground sm:table-cell">{formatRelative(c.created_at)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
      {contacts.data ? (
        <Pagination page={page} pageSize={25} total={contacts.data.total} onPage={setPage} />
      ) : null}
      <ContactDialog
        open={adding}
        onOpenChange={setAdding}
        baseUrl={wsApi("/contacts")}
        onSaved={() => void qc.invalidateQueries({ queryKey: ["contacts", ws] })}
      />
    </>
  );
}
