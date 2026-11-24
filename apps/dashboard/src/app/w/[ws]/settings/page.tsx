"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileUp, Play, Trash2, Zap } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useRef, useState } from "react";
import { toast } from "sonner";

import { ConfirmButton, ErrorState, Field, LoadingRows, NativeSelect, PageHeader, Panel, Spinner, StatusLamp } from "@/components/common";
import { SystemStrip } from "@/components/system-strip";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { del, download, errorMessage, get, patch, post, put, upload } from "@/lib/api";
import { formatBytes, formatDateTime, formatRelative, humanize } from "@/lib/format";
import { useCan, useMe, useWorkspace, useWorkspaceId, useWsApi } from "@/lib/hooks";
import type { ModelInfo, Role, SystemHealth, Workspace } from "@/lib/types";

function SettingsInner() {
  const params = useSearchParams();
  const me = useMe();
  const admin = me.data?.user.is_instance_admin;
  const canWsAdmin = useCan("admin");
  const tab = params.get("tab") ?? "profile";
  return (
    <>
      <PageHeader title="Settings" />
      <Tabs defaultValue={tab}>
        <TabsList className="mb-5 flex-wrap">
          <TabsTrigger value="profile">Profile and security</TabsTrigger>
          <TabsTrigger value="workspace">Workspace</TabsTrigger>
          <TabsTrigger value="workspaces">New workspace</TabsTrigger>
          <TabsTrigger value="ai">AI models</TabsTrigger>
          <TabsTrigger value="speech">Speech and audio</TabsTrigger>
          <TabsTrigger value="telephony">Telephony</TabsTrigger>
          {admin ? <TabsTrigger value="backups">Backups</TabsTrigger> : null}
          {admin ? <TabsTrigger value="users">Users</TabsTrigger> : null}
          <TabsTrigger value="diagnostics">Diagnostics</TabsTrigger>
        </TabsList>
        <TabsContent value="profile">
          <Profile />
        </TabsContent>
        <TabsContent value="workspace">
          <WorkspaceSettings canAdmin={canWsAdmin} />
        </TabsContent>
        <TabsContent value="workspaces">
          <NewWorkspace />
        </TabsContent>
        <TabsContent value="ai">
          <AiSettings admin={!!admin} />
        </TabsContent>
        <TabsContent value="speech">
          <SpeechSettings admin={!!admin} />
        </TabsContent>
        <TabsContent value="telephony">
          <TelephonySettings admin={!!admin} />
        </TabsContent>
        {admin ? (
          <TabsContent value="backups">
            <Backups />
          </TabsContent>
        ) : null}
        {admin ? (
          <TabsContent value="users">
            <Users />
          </TabsContent>
        ) : null}
        <TabsContent value="diagnostics">
          <Diagnostics admin={!!admin} />
        </TabsContent>
      </Tabs>
    </>
  );
}

export default function SettingsPage() {
  return (
    <Suspense fallback={<LoadingRows rows={6} />}>
      <SettingsInner />
    </Suspense>
  );
}

// ---------------------------------------------------------------- profile
function Profile() {
  const me = useMe();
  if (!me.data) return <LoadingRows />;
  return <ProfileForms initialName={me.data.user.name} email={me.data.user.email} />;
}

function ProfileForms({ initialName, email }: { initialName: string; email: string }) {
  const qc = useQueryClient();
  const router = useRouter();
  const [name, setName] = useState(initialName);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const sessions = useQuery({
    queryKey: ["sessions"],
    queryFn: () => get<{ current: boolean; created_at: string; last_seen_at: string; ip: string | null; user_agent: string | null }[]>("/api/users/me/sessions"),
  });
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Panel title="Profile">
        <form
          className="grid gap-4"
          onSubmit={async (e) => {
            e.preventDefault();
            try {
              await patch("/api/users/me", { name });
              await qc.invalidateQueries({ queryKey: ["me"] });
              toast.success("Profile saved");
            } catch (err) {
              toast.error(errorMessage(err));
            }
          }}
        >
          <Field label="Name" htmlFor="pname">
            <Input id="pname" value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Email" htmlFor="pemail">
            <Input id="pemail" value={email} disabled />
          </Field>
          <div>
            <Button type="submit">Save profile</Button>
          </div>
        </form>
      </Panel>
      <Panel title="Change password" description="Signs out your other sessions.">
        <form
          className="grid gap-4"
          onSubmit={async (e) => {
            e.preventDefault();
            try {
              await post("/api/users/me/password", { current_password: current, new_password: next });
              setCurrent("");
              setNext("");
              toast.success("Password changed. Other sessions were signed out.");
              void sessions.refetch();
            } catch (err) {
              toast.error(errorMessage(err));
            }
          }}
        >
          <Field label="Current password" htmlFor="cur">
            <Input id="cur" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
          </Field>
          <Field label="New password" htmlFor="new" hint="At least 10 characters.">
            <Input id="new" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
          </Field>
          <div>
            <Button type="submit" disabled={!current || next.length < 10}>
              Change password
            </Button>
          </div>
        </form>
      </Panel>
      <Panel
        title="Signed-in sessions"
        className="lg:col-span-2"
        actions={
          <ConfirmButton
            title="Sign out everywhere?"
            description="Every session, including this one, will be signed out."
            confirmLabel="Sign out everywhere"
            onConfirm={async () => {
              await post("/api/auth/logout-all");
              qc.clear();
              router.replace("/login");
            }}
          >
            Sign out everywhere
          </ConfirmButton>
        }
        bodyClassName="p-0"
      >
        <ul className="divide-y text-sm">
          {sessions.data?.map((s, i) => (
            <li key={i} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-5 py-3">
              <span className="min-w-0 flex-1 truncate">{s.user_agent ?? "Unknown browser"}</span>
              <span className="font-mono text-xs text-muted-foreground">{s.ip}</span>
              <span className="text-xs text-muted-foreground">active {formatRelative(s.last_seen_at)}</span>
              {s.current ? <span className="text-xs font-medium">This session</span> : null}
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}

// ---------------------------------------------------------------- workspace
function WorkspaceSettings({ canAdmin }: { canAdmin: boolean }) {
  const ws = useWorkspace();
  if (ws.error) return <ErrorState error={ws.error} />;
  if (!ws.data) return <LoadingRows />;
  return <WorkspaceEditor key={ws.data.id} workspace={ws.data} canAdmin={canAdmin} />;
}

function WorkspaceEditor({ workspace, canAdmin }: { workspace: Workspace; canAdmin: boolean }) {
  const id = useWorkspaceId();
  const wsApi = useWsApi();
  const qc = useQueryClient();
  const router = useRouter();
  const [form, setForm] = useState<Partial<Workspace>>(workspace);
  const logoRef = useRef<HTMLInputElement>(null);
  const ws = { data: workspace };
  const set = (k: keyof Workspace, v: string) => setForm((f) => ({ ...f, [k]: v }));
  const save = async () => {
    try {
      await patch(wsApi(), {
        name: form.name,
        description: form.description ?? "",
        brand_color: form.brand_color,
        contact_email: form.contact_email || null,
        contact_phone: form.contact_phone || null,
        website: form.website || null,
        address: form.address || null,
        timezone: form.timezone,
        default_country: form.default_country,
      });
      await qc.invalidateQueries({ queryKey: ["workspace", id] });
      await qc.invalidateQueries({ queryKey: ["me"] });
      toast.success("Workspace saved");
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };
  return (
    <div className="grid gap-5">
      <Panel title="Business profile" description="Agents introduce themselves on behalf of this business.">
        <fieldset disabled={!canAdmin} className="grid gap-4 md:grid-cols-2">
          <Field label="Business name" htmlFor="wn">
            <Input id="wn" value={form.name ?? ""} onChange={(e) => set("name", e.target.value)} />
          </Field>
          <Field label="Website" htmlFor="wweb">
            <Input id="wweb" value={form.website ?? ""} onChange={(e) => set("website", e.target.value)} placeholder="https://" />
          </Field>
          <Field label="Contact email" htmlFor="wemail">
            <Input id="wemail" value={form.contact_email ?? ""} onChange={(e) => set("contact_email", e.target.value)} />
          </Field>
          <Field label="Contact phone" htmlFor="wphone">
            <Input id="wphone" value={form.contact_phone ?? ""} onChange={(e) => set("contact_phone", e.target.value)} />
          </Field>
          <Field label="Timezone" htmlFor="wtz">
            <Input id="wtz" value={form.timezone ?? ""} onChange={(e) => set("timezone", e.target.value)} />
          </Field>
          <Field label="Default phone country" htmlFor="wcc" hint="Two-letter code used to read local numbers.">
            <Input id="wcc" value={form.default_country ?? ""} maxLength={2} onChange={(e) => set("default_country", e.target.value.toUpperCase())} />
          </Field>
          <Field label="Description" htmlFor="wdesc" className="md:col-span-2">
            <Textarea id="wdesc" rows={3} value={form.description ?? ""} onChange={(e) => set("description", e.target.value)} />
          </Field>
          <Field label="Address" htmlFor="waddr" className="md:col-span-2">
            <Textarea id="waddr" rows={2} value={form.address ?? ""} onChange={(e) => set("address", e.target.value)} />
          </Field>
          <Field label="Brand colour" htmlFor="wcolor">
            <div className="flex items-center gap-2">
              <input id="wcolor" type="color" value={form.brand_color ?? "#5b6865"} onChange={(e) => set("brand_color", e.target.value)} className="h-8 w-12 rounded border" />
              <span className="font-mono text-xs">{form.brand_color}</span>
            </div>
          </Field>
          <Field label="Logo" htmlFor="wlogo" hint="PNG, JPEG or WebP up to 1 MB.">
            <div className="flex items-center gap-3">
              {/* eslint-disable-next-line @next/next/no-img-element -- authenticated API image, not a static asset */}
              {ws.data.has_logo ? <img src={`${wsApi("/logo")}?v=${ws.data.name}`} alt="" className="size-8 rounded object-contain" /> : null}
              <Button type="button" variant="outline" size="sm" onClick={() => logoRef.current?.click()}>
                <FileUp /> Upload logo
              </Button>
              <input
                ref={logoRef}
                id="wlogo"
                type="file"
                accept="image/png,image/jpeg,image/webp"
                className="hidden"
                onChange={async (e) => {
                  const f = e.target.files?.[0];
                  e.target.value = "";
                  if (!f) return;
                  const fd = new FormData();
                  fd.append("file", f);
                  try {
                    await upload(wsApi("/logo"), fd);
                    await qc.invalidateQueries({ queryKey: ["workspace", id] });
                    toast.success("Logo updated");
                  } catch (err) {
                    toast.error(errorMessage(err));
                  }
                }}
              />
            </div>
          </Field>
        </fieldset>
        {canAdmin ? (
          <Button className="mt-5" onClick={save}>
            Save workspace
          </Button>
        ) : null}
      </Panel>
      <Members canAdmin={canAdmin} />
      {canAdmin ? (
        <Panel title="Export" description="A JSON file with this workspace’s profile, agents, knowledge, campaigns and (optionally) contacts. Calls and recordings are not included.">
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => download(wsApi("/export"), `${ws.data.slug}-export.json`).catch((e) => toast.error(errorMessage(e)))}>
              <Download /> Export with contacts
            </Button>
            <Button variant="outline" onClick={() => download(wsApi("/export"), `${ws.data.slug}-config.json`, { include_contacts: false }).catch((e) => toast.error(errorMessage(e)))}>
              <Download /> Export without contacts
            </Button>
          </div>
        </Panel>
      ) : null}
      {ws.data.role === "owner" ? (
        <Panel title="Delete workspace" description="Hides the workspace and everything in it from every member.">
          <ConfirmButton
            title={`Delete ${ws.data.name}?`}
            description="Members lose access immediately. Data stays in the database until you remove it from a backup restore point."
            confirmLabel="Delete workspace"
            onConfirm={async () => {
              await del(wsApi());
              await qc.invalidateQueries({ queryKey: ["me"] });
              router.replace("/");
            }}
          >
            <Trash2 /> Delete workspace
          </ConfirmButton>
        </Panel>
      ) : null}
    </div>
  );
}

function Members({ canAdmin }: { canAdmin: boolean }) {
  const wsApi = useWsApi();
  const id = useWorkspaceId();
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("editor");
  const members = useQuery({ queryKey: ["members", id], queryFn: () => get<{ id: string; email: string; name: string; role: Role }[]>(wsApi("/members")) });
  const refresh = () => void qc.invalidateQueries({ queryKey: ["members", id] });
  return (
    <Panel title="Members" description="Owners and admins manage members; editors manage agents, contacts and campaigns; viewers can only look." bodyClassName="p-0">
      <ul className="divide-y text-sm">
        {members.data?.map((m) => (
          <li key={m.id} className="flex flex-wrap items-center gap-3 px-5 py-3">
            <span className="min-w-0 flex-1">
              <span className="font-medium">{m.name}</span> <span className="text-muted-foreground">{m.email}</span>
            </span>
            {canAdmin ? (
              <NativeSelect
                value={m.role}
                className="h-7 w-28 text-xs"
                aria-label={`Role for ${m.email}`}
                onChange={async (e) => {
                  try {
                    await patch(wsApi(`/members/${m.id}`), { role: e.target.value });
                    refresh();
                  } catch (err) {
                    toast.error(errorMessage(err));
                  }
                }}
              >
                {(["owner", "admin", "editor", "viewer"] as Role[]).map((r) => (
                  <option key={r} value={r}>
                    {humanize(r)}
                  </option>
                ))}
              </NativeSelect>
            ) : (
              <span className="text-xs">{humanize(m.role)}</span>
            )}
            {canAdmin ? (
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={`Remove ${m.email}`}
                onClick={async () => {
                  try {
                    await del(wsApi(`/members/${m.id}`));
                    refresh();
                  } catch (err) {
                    toast.error(errorMessage(err));
                  }
                }}
              >
                <Trash2 />
              </Button>
            ) : null}
          </li>
        ))}
      </ul>
      {canAdmin ? (
        <form
          className="flex flex-wrap gap-2 border-t p-4"
          onSubmit={async (e) => {
            e.preventDefault();
            try {
              await post(wsApi("/members"), { email, role });
              setEmail("");
              refresh();
              toast.success("Member added");
            } catch (err) {
              toast.error(errorMessage(err));
            }
          }}
        >
          <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="Email of an existing user" className="w-64" aria-label="Member email" />
          <NativeSelect value={role} onChange={(e) => setRole(e.target.value as Role)} className="w-28" aria-label="Role">
            <option value="admin">Admin</option>
            <option value="editor">Editor</option>
            <option value="viewer">Viewer</option>
          </NativeSelect>
          <Button type="submit" disabled={!email}>
            Add member
          </Button>
        </form>
      ) : null}
    </Panel>
  );
}

function NewWorkspace() {
  const qc = useQueryClient();
  const router = useRouter();
  const [name, setName] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Panel title="Create a workspace" description="Each business gets its own agents, contacts, knowledge, calls and compliance settings.">
        <form
          className="flex gap-2"
          onSubmit={async (e) => {
            e.preventDefault();
            try {
              const w = await post<Workspace>("/api/workspaces", { name });
              await qc.invalidateQueries({ queryKey: ["me"] });
              toast.success(`Created ${w.name}`);
              router.push(`/w/${w.id}`);
            } catch (err) {
              toast.error(errorMessage(err));
            }
          }}
        >
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Business name" aria-label="Business name" />
          <Button type="submit" disabled={!name.trim()}>
            Create
          </Button>
        </form>
      </Panel>
      <Panel title="Import a workspace" description="From a voice-agent export, or the sample at demo/shrotihost/workspace.json. Imports always create a new workspace; campaigns arrive as simulation drafts.">
        <Button variant="outline" onClick={() => fileRef.current?.click()}>
          <FileUp /> Choose export file
        </Button>
        <input
          ref={fileRef}
          type="file"
          accept="application/json,.json"
          className="hidden"
          onChange={async (e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (!f) return;
            const fd = new FormData();
            fd.append("file", f);
            try {
              const r = await upload<{ workspace: Workspace; report: Record<string, number> }>("/api/workspaces/import", fd);
              await qc.invalidateQueries({ queryKey: ["me"] });
              toast.success(`Imported ${r.workspace.name}: ${r.report.agents} agents, ${r.report.contacts} contacts, ${r.report.knowledge} knowledge entries`);
              router.push(`/w/${r.workspace.id}`);
            } catch (err) {
              toast.error(errorMessage(err));
            }
          }}
        />
      </Panel>
    </div>
  );
}

// ---------------------------------------------------------------- providers
interface ProviderSettings {
  [kind: string]: unknown;
  defaults: { ollama_url: string; default_llm_model: string; whisper_server_url: string; piper_url: string; default_tts_voice: string; audio: Record<string, number | boolean | string> };
}
type ProviderRow = { provider: string; config: Record<string, unknown>; enabled: boolean; has_secrets: boolean; options: string[] };

function useProviders() {
  return useQuery({ queryKey: ["providers"], queryFn: () => get<ProviderSettings>("/api/system/providers") });
}

async function saveProvider(kind: string, body: { provider: string; config: Record<string, unknown>; enabled?: boolean; secrets?: Record<string, string> | null }) {
  await put(`/api/system/providers/${kind}`, { enabled: true, ...body });
}

function AiSettings({ admin }: { admin: boolean }) {
  const providers = useProviders();
  if (providers.error) return <ErrorState error={providers.error} />;
  if (!providers.data) return <LoadingRows />;
  const llm = providers.data.llm as ProviderRow;
  return (
    <AiEditor
      admin={admin}
      initialUrl={String(llm.config.base_url ?? providers.data.defaults.ollama_url)}
      initialModel={String(llm.config.default_model ?? providers.data.defaults.default_llm_model)}
    />
  );
}

function AiEditor({ admin, initialUrl, initialModel }: { admin: boolean; initialUrl: string; initialModel: string }) {
  const qc = useQueryClient();
  const models = useQuery({ queryKey: ["models"], queryFn: () => get<{ available: boolean; error?: string; models: ModelInfo[]; default: string }>("/api/system/models") });
  const [url, setUrl] = useState(initialUrl);
  const [model, setModel] = useState(initialModel);
  const [testResult, setTestResult] = useState<string | null>(null);
  const test = useMutation({
    mutationFn: (name: string) => post<{ reply: string; ttft_ms: number; total_ms: number }>("/api/system/models/test", { model: name }),
    onSuccess: (r, name) => setTestResult(`${name}: “${r.reply}” (first token ${Math.round(r.ttft_ms)} ms, total ${Math.round(r.total_ms)} ms)`),
    onError: (e) => toast.error(errorMessage(e)),
  });
  const warm = useMutation({
    mutationFn: (name: string) => post<{ load_ms: number }>("/api/system/models/warmup", { model: name }),
    onSuccess: (r) => toast.success(`Model loaded in ${Math.round(r.load_ms)} ms`),
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <div className="grid gap-5">
      <Panel title="Local language models" description="Read from your Ollama installation. voice-agent never downloads models and never falls back to a cloud service." bodyClassName="p-0">
        {models.data && !models.data.available ? (
          <div className="p-5">
            <ErrorState error={new Error(models.data.error ?? "Ollama unavailable")} onRetry={() => models.refetch()} />
          </div>
        ) : !models.data ? (
          <div className="p-5">
            <LoadingRows />
          </div>
        ) : (
          <ul className="divide-y text-sm">
            {models.data.models.map((m) => (
              <li key={m.name} className="flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-3">
                <span className="min-w-0 flex-1">
                  <span className="font-mono text-xs font-medium">{m.name}</span>
                  {m.name === models.data.default ? <span className="ml-2 text-xs text-muted-foreground">default</span> : null}
                  <span className="block text-xs text-muted-foreground">
                    {[m.family, m.parameter_size, m.quantization, m.size_bytes && m.is_local ? formatBytes(m.size_bytes) : null].filter(Boolean).join(", ")}
                  </span>
                </span>
                {m.is_local ? (
                  <StatusLamp state={m.fits_memory ? "ok" : "degraded"} label={m.fits_memory ? "Local" : "Local, large for this Mac"} className="text-xs" />
                ) : (
                  <StatusLamp state="unavailable" label={`Remote (${m.remote_host ?? "cloud"}), blocked`} className="text-xs" />
                )}
                {m.is_local ? (
                  <span className="flex gap-1">
                    <Button variant="ghost" size="xs" onClick={() => warm.mutate(m.name)} disabled={warm.isPending}>
                      <Zap /> Load
                    </Button>
                    <Button variant="ghost" size="xs" onClick={() => test.mutate(m.name)} disabled={test.isPending}>
                      {test.isPending && test.variables === m.name ? <Spinner /> : <Play />} Test
                    </Button>
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
        {testResult ? <p className="border-t px-5 py-3 text-sm">{testResult}</p> : null}
      </Panel>
      <Panel title="Ollama connection">
        <fieldset disabled={!admin} className="grid max-w-2xl gap-4 md:grid-cols-2">
          <Field label="Ollama address" htmlFor="ourl" hint="Must be on this machine or your private network.">
            <Input id="ourl" value={url} onChange={(e) => setUrl(e.target.value)} />
          </Field>
          <Field label="Default model for new agents" htmlFor="omodel">
            <NativeSelect id="omodel" value={model} onChange={(e) => setModel(e.target.value)}>
              {models.data?.models.filter((m) => m.is_local).map((m) => (
                <option key={m.name} value={m.name}>
                  {m.name}
                </option>
              ))}
            </NativeSelect>
          </Field>
        </fieldset>
        {admin ? (
          <Button
            className="mt-4"
            onClick={async () => {
              try {
                await saveProvider("llm", { provider: "ollama", config: { base_url: url, default_model: model } });
                await qc.invalidateQueries();
                toast.success("Saved");
              } catch (e) {
                toast.error(errorMessage(e));
              }
            }}
          >
            Save
          </Button>
        ) : (
          <p className="mt-3 text-xs text-muted-foreground">Only instance administrators can change provider settings.</p>
        )}
      </Panel>
    </div>
  );
}

function SpeechSettings({ admin }: { admin: boolean }) {
  const providers = useProviders();
  if (providers.error) return <ErrorState error={providers.error} />;
  if (!providers.data) return <LoadingRows />;
  return <SpeechEditor admin={admin} data={providers.data} />;
}

function SpeechEditor({ admin, data }: { admin: boolean; data: ProviderSettings }) {
  const qc = useQueryClient();
  const d = data.defaults;
  const sRow = data.stt as ProviderRow;
  const tRow = data.tts as ProviderRow;
  const [stt, setStt] = useState({ provider: sRow.provider, base_url: String(sRow.config.base_url ?? d.whisper_server_url), language: String(sRow.config.language ?? "en") });
  const [tts, setTts] = useState({ provider: tRow.provider, base_url: String(tRow.config.base_url ?? d.piper_url), default_voice: String(tRow.config.default_voice ?? d.default_tts_voice) });
  const [audio, setAudio] = useState<Record<string, number | boolean | string>>(d.audio);
  const saveAll = async () => {
    try {
      await saveProvider("stt", { provider: stt.provider, config: { base_url: stt.base_url, language: stt.language } });
      await saveProvider("tts", { provider: tts.provider, config: { base_url: tts.base_url, default_voice: tts.default_voice } });
      await saveProvider("audio", { provider: "default", config: audio });
      await qc.invalidateQueries();
      toast.success("Speech settings saved");
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };
  const num = (k: string) => (e: React.ChangeEvent<HTMLInputElement>) => setAudio((a) => ({ ...a, [k]: Number(e.target.value) }));
  return (
    <fieldset disabled={!admin} className="grid gap-5">
      <Panel title="Speech recognition" description="whisper.cpp, running locally with Metal acceleration.">
        <div className="grid max-w-3xl gap-4 md:grid-cols-3">
          <Field label="Engine" htmlFor="sttp">
            <NativeSelect id="sttp" value={stt.provider} onChange={(e) => setStt({ ...stt, provider: e.target.value })}>
              <option value="whisper_cpp_server">whisper-server (model kept loaded)</option>
              <option value="whisper_cpp_cli">whisper-cli (loads per utterance)</option>
            </NativeSelect>
          </Field>
          <Field label="whisper-server address" htmlFor="sttu">
            <Input id="sttu" value={stt.base_url} onChange={(e) => setStt({ ...stt, base_url: e.target.value })} />
          </Field>
          <Field label="Default language" htmlFor="sttl">
            <Input id="sttl" value={stt.language} maxLength={2} onChange={(e) => setStt({ ...stt, language: e.target.value })} />
          </Field>
        </div>
      </Panel>
      <Panel title="Voice" description="Piper runs as a separate local process (GPL-3.0). macOS voices are an alternative that needs no download.">
        <div className="grid max-w-3xl gap-4 md:grid-cols-3">
          <Field label="Default engine" htmlFor="ttsp">
            <NativeSelect id="ttsp" value={tts.provider} onChange={(e) => setTts({ ...tts, provider: e.target.value })}>
              <option value="piper">Piper</option>
              <option value="macos_say">macOS system voice</option>
            </NativeSelect>
          </Field>
          <Field label="Piper address" htmlFor="ttsu">
            <Input id="ttsu" value={tts.base_url} onChange={(e) => setTts({ ...tts, base_url: e.target.value })} />
          </Field>
          <Field label="Default Piper voice" htmlFor="ttsv">
            <Input id="ttsv" value={tts.default_voice} onChange={(e) => setTts({ ...tts, default_voice: e.target.value })} />
          </Field>
        </div>
      </Panel>
      <Panel title="Turn-taking" description="How the agent decides you’ve finished speaking, and when you interrupt it.">
        <div className="grid max-w-3xl gap-4 md:grid-cols-3">
          <Field label="Voice detector" htmlFor="vad">
            <NativeSelect id="vad" value={String(audio.vad ?? "silero")} onChange={(e) => setAudio((a) => ({ ...a, vad: e.target.value }))}>
              <option value="silero">Silero (neural, recommended)</option>
              <option value="energy">Energy threshold (fallback)</option>
            </NativeSelect>
          </Field>
          <Field label="Speech threshold (0–1)" htmlFor="vth">
            <Input id="vth" type="number" step={0.05} min={0.2} max={0.9} value={Number(audio.vad_threshold ?? 0.5)} onChange={num("vad_threshold")} />
          </Field>
          <Field label="End-of-turn silence (ms)" htmlFor="eos">
            <Input id="eos" type="number" min={300} max={3000} value={Number(audio.end_silence_ms ?? 700)} onChange={num("end_silence_ms")} />
          </Field>
          <Field label="Minimum speech (ms)" htmlFor="ms">
            <Input id="ms" type="number" min={64} max={1000} value={Number(audio.min_speech_ms ?? 160)} onChange={num("min_speech_ms")} />
          </Field>
          <Field label="Talk-over needed to interrupt (ms)" htmlFor="bim">
            <Input id="bim" type="number" min={100} max={2000} value={Number(audio.barge_in_min_ms ?? 300)} onChange={num("barge_in_min_ms")} />
          </Field>
          <label className="flex items-center gap-2.5 self-end pb-1.5 text-sm">
            <Switch checked={Boolean(audio.barge_in ?? true)} onCheckedChange={(v) => setAudio((a) => ({ ...a, barge_in: v }))} />
            Let callers interrupt the agent
          </label>
        </div>
      </Panel>
      {admin ? (
        <div>
          <Button onClick={saveAll}>Save speech settings</Button>
        </div>
      ) : null}
    </fieldset>
  );
}

function TelephonySettings({ admin }: { admin: boolean }) {
  const providers = useProviders();
  if (providers.error) return <ErrorState error={providers.error} />;
  if (!providers.data) return <LoadingRows />;
  return <TelephonyEditor admin={admin} row={providers.data.telephony as ProviderRow} />;
}

function TelephonyEditor({ admin, row }: { admin: boolean; row: ProviderRow }) {
  const qc = useQueryClient();
  const health = useQuery({ queryKey: ["health"], queryFn: () => get<SystemHealth>("/api/system/health") });
  const [cfg, setCfg] = useState<Record<string, string>>(() => Object.fromEntries(Object.entries(row.config).map(([k, v]) => [k, String(v)])));
  const [password, setPassword] = useState("");
  const [enabled, setEnabled] = useState(row.provider === "asterisk_ari" && row.enabled);
  const realOn = health.data?.real_telephony_enabled;
  const field = (k: string, label: string, hint?: string, placeholder?: string) => (
    <Field label={label} htmlFor={`ari-${k}`} hint={hint}>
      <Input id={`ari-${k}`} value={cfg[k] ?? ""} placeholder={placeholder} onChange={(e) => setCfg((c) => ({ ...c, [k]: e.target.value }))} />
    </Field>
  );
  return (
    <div className="grid gap-5">
      <Panel title="Calling modes">
        <ul className="grid gap-2 text-sm">
          <li>
            <StatusLamp state="ok" label="Browser calls" /> <span className="text-muted-foreground">Always available through the playground and customer links.</span>
          </li>
          <li>
            <StatusLamp state="ok" label="Simulation" /> <span className="text-muted-foreground">Always available; no phone network is touched.</span>
          </li>
          <li>
            <StatusLamp state={realOn ? (enabled ? "ok" : "degraded") : "disabled"} label="Real phone calls (PSTN)" />{" "}
            <span className="text-muted-foreground">
              {realOn ? (enabled ? "Provider enabled. Each workspace still needs its compliance prerequisites." : "Allowed on this installation; no provider enabled.") : "Disabled on this installation (VA_REAL_TELEPHONY_ENABLED=false)."}
            </span>
          </li>
        </ul>
      </Panel>
      <Panel
        title="Asterisk (SIP) connection"
        description="voice-agent controls calls through your own Asterisk server’s REST interface (ARI). Asterisk connects to your SIP trunk provider. This adapter is integration-ready but has not been verified against a live carrier."
      >
        <fieldset disabled={!admin} className="grid max-w-3xl gap-4 md:grid-cols-2">
          {field("base_url", "ARI address", "e.g. http://127.0.0.1:8088", "http://127.0.0.1:8088")}
          {field("username", "ARI username")}
          <Field label="ARI password" htmlFor="ari-pass" hint={row.has_secrets ? "Saved (encrypted). Leave blank to keep it." : "Stored encrypted."}>
            <Input id="ari-pass" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
          </Field>
          {field("app", "Stasis application name", undefined, "voice-agent")}
          {field("endpoint_template", "Dial string", "{number} is replaced with the E.164 number", "PJSIP/{number}@your-trunk")}
          {field("caller_id", "Caller ID", "Must be a number you are registered to use")}
          {field("rtp_advertise_host", "Media address Asterisk should send audio to", undefined, "127.0.0.1")}
          <label className="flex items-center gap-2.5 self-end pb-1.5 text-sm">
            <Switch checked={enabled} onCheckedChange={setEnabled} disabled={!realOn} />
            Enable this provider
          </label>
        </fieldset>
        {admin ? (
          <Button
            className="mt-5"
            onClick={async () => {
              try {
                const config: Record<string, unknown> = { ...cfg };
                for (const k of ["rtp_port_start", "rtp_port_end"]) if (config[k]) config[k] = Number(config[k]);
                await saveProvider("telephony", { provider: "asterisk_ari", config, enabled, secrets: password ? { password } : undefined });
                setPassword("");
                await qc.invalidateQueries();
                toast.success("Telephony settings saved");
              } catch (e) {
                toast.error(errorMessage(e));
              }
            }}
          >
            Save
          </Button>
        ) : null}
        <p className="mt-4 text-xs text-muted-foreground">See docs/TELEPHONY.md for the Asterisk configuration and what remains to verify with your provider.</p>
      </Panel>
    </div>
  );
}

// ---------------------------------------------------------------- admin
function Backups() {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["backups"], queryFn: () => get<{ backups: { name: string; size_bytes: number; encrypted: boolean; created_at: string }[]; restore_pending: boolean }>("/api/system/backups") });
  const [pass, setPass] = useState("");
  const [restorePass, setRestorePass] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const create = useMutation({
    mutationFn: () => {
      const fd = new FormData();
      if (pass) fd.append("passphrase", pass);
      return upload<{ name: string; size_bytes: number }>("/api/system/backups", fd);
    },
    onSuccess: (r) => {
      toast.success(`Backup created (${formatBytes(r.size_bytes)})`);
      setPass("");
      void qc.invalidateQueries({ queryKey: ["backups"] });
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Panel title="Create a backup" description="Database, recordings and uploads in one archive, saved in data/backups with owner-only permissions.">
        <Field label="Passphrase (optional)" htmlFor="bpass" hint="Encrypts the archive with AES-256. Keep it safe; it cannot be recovered.">
          <Input id="bpass" type="password" value={pass} onChange={(e) => setPass(e.target.value)} autoComplete="new-password" />
        </Field>
        <Button className="mt-4" onClick={() => create.mutate()} disabled={create.isPending || (pass.length > 0 && pass.length < 10)}>
          {create.isPending ? <Spinner /> : null} Create backup
        </Button>
      </Panel>
      <Panel title="Restore" description="The backup is verified (checksums and database integrity) and staged. It replaces current data the next time voice-agent starts; current data is kept in data/backups.">
        {list.data?.restore_pending ? <p className="mb-3 text-sm font-medium text-warn">A restore is staged. Restart voice-agent to apply it.</p> : null}
        <Field label="Passphrase, if the backup is encrypted" htmlFor="rpass">
          <Input id="rpass" type="password" value={restorePass} onChange={(e) => setRestorePass(e.target.value)} />
        </Field>
        <Button variant="outline" className="mt-4" onClick={() => fileRef.current?.click()}>
          <FileUp /> Choose backup file
        </Button>
        <input
          ref={fileRef}
          type="file"
          className="hidden"
          onChange={async (e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (!f) return;
            const fd = new FormData();
            fd.append("file", f);
            if (restorePass) fd.append("passphrase", restorePass);
            try {
              const r = await upload<{ message: string }>("/api/system/backups/restore", fd);
              toast.success(r.message);
              void qc.invalidateQueries({ queryKey: ["backups"] });
            } catch (err) {
              toast.error(errorMessage(err));
            }
          }}
        />
      </Panel>
      <Panel title="Backups on this machine" className="lg:col-span-2" bodyClassName="p-0">
        {list.data?.backups.length ? (
          <ul className="divide-y text-sm">
            {list.data.backups.map((b) => (
              <li key={b.name} className="flex items-center gap-4 px-5 py-3">
                <span className="min-w-0 flex-1 truncate font-mono text-xs">{b.name}</span>
                <span className="text-xs text-muted-foreground">{b.encrypted ? "Encrypted" : "Not encrypted"}</span>
                <span className="text-xs text-muted-foreground tabular-nums">{formatBytes(b.size_bytes)}</span>
                <span className="text-xs text-muted-foreground">{formatDateTime(b.created_at)}</span>
                <Button variant="ghost" size="xs" onClick={() => download(`/api/system/backups/${b.name}`, b.name).catch((e) => toast.error(errorMessage(e)))}>
                  <Download /> Download
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="px-5 py-6 text-sm text-muted-foreground">No backups yet.</p>
        )}
      </Panel>
    </div>
  );
}

function Users() {
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => get<{ id: string; email: string; name: string; is_instance_admin: boolean; is_active: boolean; last_login_at: string | null }[]>("/api/users") });
  const [form, setForm] = useState({ email: "", name: "", password: "" });
  return (
    <div className="grid gap-5">
      <Panel title="People who can sign in" bodyClassName="p-0">
        <ul className="divide-y text-sm">
          {users.data?.map((u) => (
            <li key={u.id} className="flex flex-wrap items-center gap-3 px-5 py-3">
              <span className="min-w-0 flex-1">
                <span className="font-medium">{u.name}</span> <span className="text-muted-foreground">{u.email}</span>
                {u.is_instance_admin ? <span className="ml-2 text-xs">Administrator</span> : null}
              </span>
              <span className="text-xs text-muted-foreground">{u.last_login_at ? `last sign-in ${formatRelative(u.last_login_at)}` : "never signed in"}</span>
              <label className="flex items-center gap-2 text-xs">
                <Switch
                  checked={u.is_active}
                  onCheckedChange={async (v) => {
                    try {
                      await patch(`/api/users/${u.id}`, { is_active: v });
                      void qc.invalidateQueries({ queryKey: ["users"] });
                    } catch (e) {
                      toast.error(errorMessage(e));
                    }
                  }}
                />
                Active
              </label>
            </li>
          ))}
        </ul>
      </Panel>
      <Panel title="Add a user" description="Then add them to workspaces from Workspace settings.">
        <form
          className="grid max-w-3xl gap-3 md:grid-cols-4"
          onSubmit={async (e) => {
            e.preventDefault();
            try {
              await post("/api/users", form);
              setForm({ email: "", name: "", password: "" });
              void qc.invalidateQueries({ queryKey: ["users"] });
              toast.success("User added");
            } catch (err) {
              toast.error(errorMessage(err));
            }
          }}
        >
          <Input placeholder="Name" aria-label="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <Input placeholder="Email" type="email" aria-label="Email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
          <Input placeholder="Temporary password" type="password" aria-label="Temporary password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
          <Button type="submit" disabled={!form.email || !form.name || form.password.length < 10}>
            Add user
          </Button>
        </form>
      </Panel>
    </div>
  );
}

function Diagnostics({ admin }: { admin: boolean }) {
  const health = useQuery({ queryKey: ["health"], queryFn: () => get<SystemHealth>("/api/system/health"), refetchInterval: 15_000 });
  const diag = useQuery({
    queryKey: ["diagnostics"],
    queryFn: () => get<{ platform: Record<string, string>; process: Record<string, number>; settings: Record<string, unknown>; model_files: Record<string, number>; events: { level: string; source: string; message: string; created_at: string }[] }>("/api/system/diagnostics"),
    enabled: admin,
  });
  return (
    <div className="grid gap-5">
      <Panel title="Components">{health.data ? <SystemStrip health={health.data} /> : health.error ? <ErrorState error={health.error} /> : <LoadingRows rows={2} />}</Panel>
      {admin && diag.data ? (
        <div className="grid gap-5 lg:grid-cols-2">
          <Panel title="Installation">
            <dl className="grid grid-cols-[11rem_1fr] gap-y-1.5 text-sm">
              {Object.entries({ ...diag.data.platform, rss_mb: diag.data.process.rss_mb }).map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="text-muted-foreground">{humanize(k)}</dt>
                  <dd className="font-mono text-xs leading-5">{String(v)}</dd>
                </div>
              ))}
              {Object.entries(diag.data.settings).map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="text-muted-foreground">{humanize(k)}</dt>
                  <dd className="truncate font-mono text-xs leading-5">{String(v)}</dd>
                </div>
              ))}
            </dl>
          </Panel>
          <Panel title="Model files">
            <ul className="grid gap-1.5 text-sm">
              {Object.entries(diag.data.model_files).map(([k, v]) => (
                <li key={k} className="flex justify-between gap-3">
                  <span className="truncate font-mono text-xs">{k}</span>
                  <span className="text-xs text-muted-foreground tabular-nums">{formatBytes(v)}</span>
                </li>
              ))}
            </ul>
          </Panel>
          <Panel title="Recent system events" className="lg:col-span-2" bodyClassName="p-0">
            {diag.data.events.length ? (
              <ul className="divide-y text-sm">
                {diag.data.events.map((e, i) => (
                  <li key={i} className="flex gap-4 px-5 py-2.5">
                    <StatusLamp state={e.level === "error" ? "unavailable" : e.level === "warning" ? "degraded" : "ok"} label={e.source} className="w-32 shrink-0 text-xs" />
                    <span className="min-w-0 flex-1">{e.message}</span>
                    <span className="text-xs text-muted-foreground">{formatDateTime(e.created_at)}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="px-5 py-6 text-sm text-muted-foreground">No events recorded.</p>
            )}
          </Panel>
        </div>
      ) : null}
    </div>
  );
}
