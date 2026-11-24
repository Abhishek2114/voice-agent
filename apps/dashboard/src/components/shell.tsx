"use client";

import { useQueryClient } from "@tanstack/react-query";
import {
  AudioLines,
  BarChart3,
  BookOpen,
  Bot,
  ChevronsUpDown,
  LayoutGrid,
  LogOut,
  Megaphone,
  Menu,
  Moon,
  Phone,
  Plus,
  Settings,
  ShieldCheck,
  Sun,
  Users,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import { useEffect, useState, type ReactNode } from "react";

import { TraceMark } from "@/components/auth-frame";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { post } from "@/lib/api";
import { useMe, useWorkspaceId } from "@/lib/hooks";
import { cn } from "@/lib/utils";

const NAV: { href: string; label: string; icon: typeof Bot; group: number }[] = [
  { href: "", label: "Overview", icon: LayoutGrid, group: 0 },
  { href: "/playground", label: "Voice playground", icon: AudioLines, group: 0 },
  { href: "/agents", label: "Agents", icon: Bot, group: 1 },
  { href: "/knowledge", label: "Knowledge", icon: BookOpen, group: 1 },
  { href: "/contacts", label: "Contacts", icon: Users, group: 2 },
  { href: "/campaigns", label: "Campaigns", icon: Megaphone, group: 2 },
  { href: "/calls", label: "Calls", icon: Phone, group: 2 },
  { href: "/analytics", label: "Analytics", icon: BarChart3, group: 3 },
  { href: "/compliance", label: "Compliance", icon: ShieldCheck, group: 3 },
  { href: "/settings", label: "Settings", icon: Settings, group: 3 },
];

function NavList({ onNavigate }: { onNavigate?: () => void }) {
  const ws = useWorkspaceId();
  const pathname = usePathname();
  const base = `/w/${ws}`;
  return (
    <nav className="flex flex-col gap-0.5 px-2" aria-label="Main">
      {NAV.map((item, i) => {
        const href = base + item.href;
        const active = item.href === "" ? pathname === base : pathname.startsWith(href);
        const gap = i > 0 && NAV[i - 1].group !== item.group;
        const Icon = item.icon;
        return (
          <Link
            key={item.label}
            href={href}
            onClick={onNavigate}
            aria-current={active ? "page" : undefined}
            className={cn(
              "flex h-8 items-center gap-2.5 rounded-md px-2.5 text-sm text-sidebar-foreground/75 transition-colors hover:bg-sidebar-accent hover:text-sidebar-foreground",
              gap && "mt-3",
              active && "bg-sidebar-accent font-medium text-sidebar-foreground",
            )}
          >
            <Icon className={cn("size-4", active ? "text-signal" : "opacity-70")} />
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}

function WorkspaceSwitcher() {
  const ws = useWorkspaceId();
  const { data } = useMe();
  const router = useRouter();
  const current = data?.workspaces.find((w) => w.id === ws);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <button
            type="button"
            className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-sidebar-accent"
          />
        }
      >
        <span
          className="grid size-7 shrink-0 place-items-center rounded-md text-xs font-semibold text-white"
          style={{ background: current?.brand_color ?? "#5b6865" }}
          aria-hidden
        >
          {current?.name.slice(0, 1).toUpperCase() ?? "?"}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium">{current?.name ?? "Workspace"}</span>
          <span className="block text-xs text-muted-foreground capitalize">{current?.role ?? ""}</span>
        </span>
        <ChevronsUpDown className="size-3.5 text-muted-foreground" />
      </DropdownMenuTrigger>
      <DropdownMenuContent className="w-60" align="start">
        <DropdownMenuGroup>
          <DropdownMenuLabel>Workspaces</DropdownMenuLabel>
          {data?.workspaces.map((w) => (
            <DropdownMenuItem key={w.id} onClick={() => router.push(`/w/${w.id}`)}>
              <span className="size-2 rounded-full" style={{ background: w.brand_color }} aria-hidden />
              <span className="truncate">{w.name}</span>
              {w.id === ws ? <span className="ml-auto text-xs text-muted-foreground">current</span> : null}
            </DropdownMenuItem>
          ))}
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={() => router.push(`/w/${ws}/settings?tab=workspaces`)}>
          <Plus /> New or imported workspace
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function UserMenu() {
  const { data } = useMe();
  const { resolvedTheme, setTheme } = useTheme();
  const router = useRouter();
  const qc = useQueryClient();
  const ws = useWorkspaceId();
  return (
    <div className="flex items-center gap-1 px-2">
      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <button
              type="button"
              className="flex min-w-0 flex-1 items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-sidebar-accent"
            />
          }
        >
          <span className="grid size-6 shrink-0 place-items-center rounded-full bg-secondary text-[11px] font-semibold">
            {data?.user.name.slice(0, 1).toUpperCase()}
          </span>
          <span className="truncate">{data?.user.name}</span>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-56">
          <DropdownMenuGroup>
            <DropdownMenuLabel className="truncate">{data?.user.email}</DropdownMenuLabel>
          </DropdownMenuGroup>
          <DropdownMenuItem onClick={() => router.push(`/w/${ws}/settings?tab=profile`)}>
            <Settings /> Profile and security
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            onClick={async () => {
              await post("/api/auth/logout").catch(() => undefined);
              qc.clear();
              router.replace("/login");
            }}
          >
            <LogOut /> Sign out
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label={resolvedTheme === "dark" ? "Use light theme" : "Use dark theme"}
        onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
      >
        {resolvedTheme === "dark" ? <Sun /> : <Moon />}
      </Button>
    </div>
  );
}

function Rail({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <div className="flex h-full flex-col gap-4 py-3">
      <div className="px-2">
        <WorkspaceSwitcher />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <NavList onNavigate={onNavigate} />
      </div>
      <div className="border-t border-sidebar-border pt-3">
        <UserMenu />
      </div>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const me = useMe();
  const router = useRouter();
  const ws = useWorkspaceId();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const err = me.error as { status?: number } | null;
    if (err?.status === 401) router.replace("/login");
  }, [me.error, router]);

  useEffect(() => {
    if (ws) localStorage.setItem("va:last-ws", ws);
  }, [ws]);

  useEffect(() => {
    if (me.data && !me.data.workspaces.some((w) => w.id === ws)) router.replace("/");
  }, [me.data, ws, router]);

  return (
    <div className="flex min-h-dvh">
      <aside className="sticky top-0 hidden h-dvh w-60 shrink-0 border-r border-sidebar-border bg-sidebar lg:block">
        <Rail />
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex h-12 items-center gap-2 border-b px-4 lg:hidden">
          <Button variant="ghost" size="icon-sm" aria-label="Open navigation" onClick={() => setOpen(true)}>
            <Menu />
          </Button>
          <span className="flex items-center gap-2 text-sm font-semibold">
            <TraceMark /> voice-agent
          </span>
        </div>
        <Sheet open={open} onOpenChange={setOpen}>
          <SheetContent side="left" className="w-64 bg-sidebar p-0">
            <SheetTitle className="sr-only">Navigation</SheetTitle>
            <Rail onNavigate={() => setOpen(false)} />
          </SheetContent>
        </Sheet>
        <main id="main" className="mx-auto w-full max-w-[1320px] flex-1 px-4 py-6 sm:px-6 lg:px-10 lg:py-8">
          {children}
        </main>
      </div>
    </div>
  );
}
