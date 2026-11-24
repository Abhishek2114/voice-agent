"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useForm } from "react-hook-form";

import { AuthFrame } from "@/components/auth-frame";
import { Field, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage, post } from "@/lib/api";
import { setupSchema, type SetupValues } from "@/lib/schemas";

export default function SetupPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const form = useForm<SetupValues>({
    resolver: zodResolver(setupSchema),
    defaultValues: { name: "", email: "", password: "", confirm: "", workspace_name: "" },
  });
  const e = form.formState.errors;

  const onSubmit = form.handleSubmit(async (all) => {
    const { name, email, password, workspace_name } = all;
    const values = { name, email, password, workspace_name };
    setError(null);
    try {
      const res = await post<{ workspace_id: string }>("/api/auth/setup", values);
      await qc.invalidateQueries();
      router.replace(`/w/${res.workspace_id}`);
    } catch (err) {
      setError(errorMessage(err));
    }
  });

  return (
    <AuthFrame
      title="Set up voice-agent"
      subtitle="Create the administrator account and your first business workspace. This only happens once."
    >
      <form onSubmit={onSubmit} className="grid gap-4" noValidate>
        <Field label="Your name" htmlFor="name" error={e.name?.message}>
          <Input id="name" autoComplete="name" autoFocus {...form.register("name")} />
        </Field>
        <Field label="Email" htmlFor="email" error={e.email?.message}>
          <Input id="email" type="email" autoComplete="username" {...form.register("email")} />
        </Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Password" htmlFor="password" error={e.password?.message} hint="At least 10 characters">
            <Input id="password" type="password" autoComplete="new-password" {...form.register("password")} />
          </Field>
          <Field label="Confirm password" htmlFor="confirm" error={e.confirm?.message}>
            <Input id="confirm" type="password" autoComplete="new-password" {...form.register("confirm")} />
          </Field>
        </div>
        <Field label="Business name" htmlFor="workspace_name" error={e.workspace_name?.message} hint="You can add more businesses later.">
          <Input id="workspace_name" placeholder="e.g. ShrotiHost" {...form.register("workspace_name")} />
        </Field>
        {error ? (
          <p role="alert" className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </p>
        ) : null}
        <Button type="submit" size="lg" disabled={form.formState.isSubmitting} className="mt-2">
          {form.formState.isSubmitting ? <Spinner /> : null}
          Create account
        </Button>
      </form>
    </AuthFrame>
  );
}
