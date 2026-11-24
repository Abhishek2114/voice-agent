"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { toast } from "sonner";

import { Field, NativeSelect, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, errorMessage, patch, post } from "@/lib/api";
import { humanize } from "@/lib/format";
import { contactSchema, type ContactValues } from "@/lib/schemas";
import type { Contact } from "@/lib/types";

export const CONTACT_STATUSES = [
  "new",
  "lead",
  "contacted",
  "interested",
  "not_interested",
  "callback",
  "converted",
  "do_not_call",
  "wrong_number",
];

export function ContactDialog({
  open,
  onOpenChange,
  contact,
  baseUrl,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  contact?: Contact;
  baseUrl: string;
  onSaved: (c: Contact) => void;
}) {
  const form = useForm<ContactValues>({
    resolver: zodResolver(contactSchema),
    values: {
      first_name: contact?.first_name ?? "",
      last_name: contact?.last_name ?? "",
      phone: contact?.phone_e164 ?? "",
      email: contact?.email ?? "",
      company: contact?.company ?? "",
      tags: contact?.tags.join(", ") ?? "",
      notes: contact?.notes ?? "",
      status: contact?.status ?? "new",
    },
  });
  const e = form.formState.errors;
  const submit = form.handleSubmit(async (v) => {
    const body = {
      first_name: v.first_name,
      last_name: v.last_name,
      phone: v.phone || null,
      email: v.email || null,
      company: v.company || null,
      tags: v.tags.split(",").map((t) => t.trim()).filter(Boolean),
      notes: v.notes,
      status: v.status,
    };
    try {
      const saved = contact ? await patch<Contact>(`${baseUrl}/${contact.id}`, body) : await post<Contact>(baseUrl, body);
      toast.success(contact ? "Contact saved" : "Contact added");
      onSaved(saved);
      onOpenChange(false);
    } catch (err) {
      if (err instanceof ApiError && err.code === "invalid_phone") form.setError("phone", { message: err.message });
      else if (err instanceof ApiError && err.code === "duplicate_contact")
        form.setError("phone", { message: "Another contact already has this phone or email." });
      else toast.error(errorMessage(err));
    }
  });
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{contact ? "Edit contact" : "Add contact"}</DialogTitle>
        </DialogHeader>
        <form onSubmit={submit} className="grid gap-4" noValidate>
          <div className="grid grid-cols-2 gap-3">
            <Field label="First name" htmlFor="c-first" error={e.first_name?.message}>
              <Input id="c-first" autoFocus {...form.register("first_name")} />
            </Field>
            <Field label="Last name" htmlFor="c-last">
              <Input id="c-last" {...form.register("last_name")} />
            </Field>
            <Field label="Phone" htmlFor="c-phone" error={e.phone?.message} hint="Indian numbers can omit +91.">
              <Input id="c-phone" inputMode="tel" {...form.register("phone")} />
            </Field>
            <Field label="Email" htmlFor="c-email" error={e.email?.message}>
              <Input id="c-email" type="email" {...form.register("email")} />
            </Field>
            <Field label="Company" htmlFor="c-company">
              <Input id="c-company" {...form.register("company")} />
            </Field>
            <Field label="Status" htmlFor="c-status">
              <NativeSelect id="c-status" {...form.register("status")}>
                {CONTACT_STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {humanize(s)}
                  </option>
                ))}
              </NativeSelect>
            </Field>
          </div>
          <Field label="Tags" htmlFor="c-tags" hint="Comma separated.">
            <Input id="c-tags" {...form.register("tags")} />
          </Field>
          <Field label="Notes" htmlFor="c-notes">
            <Textarea id="c-notes" rows={3} {...form.register("notes")} />
          </Field>
          <p className="text-xs text-muted-foreground">Adding a phone number does not record consent to be called. Record consent from the contact page.</p>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={form.formState.isSubmitting}>
              {form.formState.isSubmitting ? <Spinner /> : null}
              {contact ? "Save contact" : "Add contact"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
