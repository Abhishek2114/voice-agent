"use client";

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
} from "recharts";

/** Shared, recessive chart chrome. Text uses text tokens; colour only marks identity. */
const axis = { stroke: "var(--muted-foreground)", tick: { fontSize: 11, fill: "var(--muted-foreground)" }, tickLine: false, axisLine: false };

function TooltipBox({ active, payload, label }: TooltipContentProps<number, string>) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border bg-popover px-3 py-2 text-xs shadow-sm">
      {label !== undefined ? <p className="mb-1 font-medium">{String(label)}</p> : null}
      <ul className="grid gap-0.5">
        {payload.map((p) => (
          <li key={String(p.dataKey)} className="flex items-center gap-2">
            <span className="size-2 rounded-full" style={{ background: p.color }} aria-hidden />
            <span className="text-muted-foreground">{p.name}</span>
            <span className="ml-auto pl-3 font-medium tabular-nums">{p.value}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function Legend({ items }: { items: { label: string; color: string; value?: string | number }[] }) {
  return (
    <ul className="flex flex-wrap gap-x-5 gap-y-1 text-xs">
      {items.map((i) => (
        <li key={i.label} className="flex items-center gap-1.5">
          <span className="h-2 w-3 rounded-[2px]" style={{ background: i.color }} aria-hidden />
          <span className="text-muted-foreground">{i.label}</span>
          {i.value !== undefined ? <span className="font-medium tabular-nums">{i.value}</span> : null}
        </li>
      ))}
    </ul>
  );
}

export function TrendChart({
  data,
  series,
  height = 240,
}: {
  data: Record<string, string | number>[];
  series: { key: string; label: string; color: string }[];
  height?: number;
}) {
  return (
    <div style={{ height }} role="img" aria-label={`Trend of ${series.map((s) => s.label).join(" and ")} by day`}>
      <ResponsiveContainer>
        <AreaChart data={data} margin={{ top: 8, right: 12, left: -16, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke="var(--border)" strokeDasharray="2 4" />
          <XAxis dataKey="label" {...axis} minTickGap={24} />
          <YAxis {...axis} allowDecimals={false} width={44} />
          <Tooltip content={(p) => <TooltipBox {...(p as TooltipContentProps<number, string>)} />} cursor={{ stroke: "var(--muted-foreground)", strokeDasharray: "3 3" }} />
          {series.map((s, i) => (
            <Area
              key={s.key}
              type="monotone"
              dataKey={s.key}
              name={s.label}
              stroke={s.color}
              strokeWidth={2}
              fill={s.color}
              fillOpacity={i === 0 ? 0.12 : 0}
              dot={false}
              activeDot={{ r: 4, stroke: "var(--panel)", strokeWidth: 2 }}
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

export function BarsChart({
  data,
  label,
  color = "var(--chart-1)",
  height = 220,
  layout = "horizontal",
  categoryWidth = 130,
}: {
  data: { name: string; value: number; fill?: string }[];
  label: string;
  color?: string;
  height?: number;
  layout?: "horizontal" | "vertical";
  categoryWidth?: number;
}) {
  const vertical = layout === "vertical";
  return (
    <div style={{ height }} role="img" aria-label={label}>
      <ResponsiveContainer>
        <BarChart data={data} layout={vertical ? "vertical" : "horizontal"} margin={{ top: 8, right: 16, left: vertical ? 0 : -16, bottom: 0 }} barCategoryGap={vertical ? 6 : "18%"}>
          <CartesianGrid horizontal={!vertical} vertical={vertical} stroke="var(--border)" strokeDasharray="2 4" />
          {vertical ? (
            <>
              <XAxis type="number" {...axis} allowDecimals={false} />
              <YAxis type="category" dataKey="name" {...axis} width={categoryWidth} tick={{ fontSize: 12, fill: "var(--foreground)" }} />
            </>
          ) : (
            <>
              <XAxis dataKey="name" {...axis} />
              <YAxis {...axis} allowDecimals={false} width={44} />
            </>
          )}
          <Tooltip content={(p) => <TooltipBox {...(p as TooltipContentProps<number, string>)} />} cursor={{ fill: "var(--muted)", opacity: 0.6 }} />
          <Bar dataKey="value" name={label} fill={color} radius={vertical ? [0, 4, 4, 0] : [4, 4, 0, 0]} maxBarSize={vertical ? 18 : 42} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
