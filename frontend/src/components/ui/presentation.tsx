import { CircleAlert, Info, LoaderCircle, PackageOpen } from "lucide-react";
import { Tooltip as TooltipPrimitive } from "radix-ui";
import type { ComponentProps, CSSProperties, ReactElement, ReactNode } from "react";
import { cn } from "../../lib/utils";
import {
  Card as SeatCard, CardAction, CardContent, CardHeader, CardTitle,
} from "./seat/card";

type ContentProps = { children?: ReactNode; className?: string; style?: CSSProperties };

export function Card({ title, extra, loading, children, className, ...props }:
  Omit<ComponentProps<"div">, "title"> & { title?: ReactNode; extra?: ReactNode; loading?: boolean }) {
  return (
    <SeatCard className={cn("ui-card gap-0 py-0", className)} aria-busy={loading || undefined} {...props}>
      {title != null || extra != null ? (
        <CardHeader className="ui-card-header flex min-w-0 items-center justify-between gap-3 border-b border-border p-4">
          <CardTitle className="ui-card-header-title min-w-0">{title}</CardTitle>
          {extra != null ? <CardAction className="ui-card-extra self-center">{extra}</CardAction> : null}
        </CardHeader>
      ) : null}
      <CardContent className="ui-card-body min-w-0 p-4">
        {loading && !children ? <Spin /> : children}
      </CardContent>
    </SeatCard>
  );
}

export function Space({ direction = "horizontal", size = "small", wrap, className, style, children, ...props }:
  ContentProps & { direction?: "horizontal" | "vertical"; size?: number | "mini" | "small" | "medium" | "large"; wrap?: boolean; align?: "start" | "center" | "end"; }) {
  const gap = typeof size === "number" ? size : { mini: 4, small: 8, medium: 12, large: 20 }[size];
  return <div className={cn("ui-space", direction === "vertical" ? "ui-space-vertical" : "ui-space-horizontal", className)}
    style={{ display: "flex", flexDirection: direction === "vertical" ? "column" : "row",
      flexWrap: wrap ? "wrap" : undefined, alignItems: props.align ?? (direction === "vertical" ? "stretch" : "center"), gap, ...style }}>
    {children}
  </div>;
}

type TextProps = ContentProps & {
  type?: "secondary" | "warning" | "error" | "success";
  bold?: boolean;
  code?: boolean;
  ellipsis?: boolean | { showTooltip?: boolean };
  "aria-label"?: string;
};

function Text({ children, className, type, code, bold, ellipsis, ...props }: TextProps) {
  const Element = code ? "code" : "span";
  return <Element className={cn("ui-typography", type && `ui-typography-${type}`, bold && "font-semibold", code && "ui-typography-code", ellipsis && "inline-block max-w-full truncate", className)}
    title={typeof ellipsis === "object" && ellipsis.showTooltip && typeof children === "string" ? children : undefined} {...props}>{children}</Element>;
}

function Paragraph({ type, className, ...props }: TextProps) {
  return <p className={cn("ui-typography ui-typography-paragraph", type && `ui-typography-${type}`, className)} {...props} />;
}

function Title({ heading = 2, className, ...props }: ContentProps & { heading?: 1 | 2 | 3 | 4 | 5 | 6 }) {
  const Heading = `h${heading}` as const;
  return <Heading className={cn("ui-typography ui-typography-title", className)} {...props} />;
}

export const Typography = { Text, Paragraph, Title };

export function Tag({ color = "gray", className, children }: ContentProps & { color?: string }) {
  return <span className={cn("ui-tag inline-flex items-center rounded-md px-2 py-1 text-xs", className)} data-tone={color}>{children}</span>;
}

export function Avatar({ size = 32, children, className }: ContentProps & { size?: number }) {
  return <span className={cn("ui-avatar inline-grid shrink-0 place-items-center rounded-full bg-accent text-accent-foreground", className)}
    style={{ width: size, height: size }}>{children}</span>;
}

export function Alert({ type = "info", content, className, ...props }: { content: ReactNode; type?: "info" | "warning" | "error" | "success"; className?: string; closable?: false }) {
  const Icon = type === "error" || type === "warning" ? CircleAlert : Info;
  return <div className={cn("ui-alert flex items-start gap-2 rounded-lg border px-3 py-2", className)} data-tone={type}
    role={props.closable === false ? undefined : type === "error" ? "alert" : undefined}>
    <Icon size={16} aria-hidden="true" className="mt-0.5 shrink-0" /><div className="min-w-0">{content}</div>
  </div>;
}

export function Empty({ description }: { description?: ReactNode }) {
  return <div className="ui-empty grid justify-items-center gap-2 text-sm text-muted-foreground">
    <PackageOpen aria-hidden="true" size={32} /><div className="ui-empty-description">{description ?? "暂无数据"}</div>
  </div>;
}

export function Spin({ tip }: { tip?: ReactNode }) {
  return <span className="ui-spin inline-flex items-center gap-2 text-muted-foreground" role="status">
    <LoaderCircle aria-hidden="true" size={18} className="animate-spin" />{tip ?? <span className="sr-only">加载中</span>}
  </span>;
}

export function Divider() {
  return <hr className="ui-divider my-4 border-border" />;
}

export function Progress({ percent, showText, label }: { percent: number; showText?: boolean; label: string }) {
  const value = Math.max(0, Math.min(100, percent));
  return <div className="flex items-center gap-2">
    <progress aria-label={label} className="ui-progress flex-1" max={100} value={value} />
    {showText ? <span>{value}%</span> : null}
  </div>;
}

export function Tooltip({ content, children }: { content: ReactNode; children: ReactElement; trigger?: ("hover" | "focus")[] }) {
  return <TooltipPrimitive.Provider delayDuration={300}><TooltipPrimitive.Root>
    <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content sideOffset={5} className="ui-tooltip z-[1200] max-w-72 rounded-md border border-border bg-card px-2 py-1 text-xs text-foreground shadow-md">{content}</TooltipPrimitive.Content>
    </TooltipPrimitive.Portal>
  </TooltipPrimitive.Root></TooltipPrimitive.Provider>;
}

function Row({ gutter = 0, className, children }: ContentProps & { gutter?: number | [number, number] }) {
  const [horizontal, vertical] = typeof gutter === "number" ? [gutter, gutter] : gutter;
  return <div className={cn("ui-row", className)} style={{ "--grid-horizontal": `${horizontal}px`, "--grid-vertical": `${vertical}px` } as CSSProperties}>{children}</div>;
}

function Col({ span, xs = span ?? 24, sm = xs, md = sm, lg = md, className, children }: ContentProps &
  { span?: number; xs?: number; sm?: number; md?: number; lg?: number; flex?: string }) {
  return <div className={cn("ui-col", className)} style={{ "--cols-xs": xs, "--cols-sm": sm, "--cols-md": md, "--cols-lg": lg } as CSSProperties}>{children}</div>;
}

export const Grid = { Row, Col };

export function Statistic({ title, value, prefix, suffix, extra }: { title: ReactNode; value: ReactNode; prefix?: ReactNode; suffix?: ReactNode; extra?: ReactNode }) {
  return <div className="ui-statistic">
    <div className="ui-statistic-title text-sm text-muted-foreground">{title}</div>
    <div className="ui-statistic-value inline-flex items-baseline gap-1 text-2xl font-semibold tabular-nums">{prefix}{value}{suffix}</div>
    {extra != null ? <div className="ui-statistic-extra">{extra}</div> : null}
  </div>;
}
