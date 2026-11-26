import { LoaderCircle } from "lucide-react";
import type { ComponentProps, ReactNode } from "react";
import { cn } from "../../lib/utils";
import { Button as SeatButton } from "./seat/button";

type ButtonProps = ComponentProps<typeof SeatButton> & {
  icon?: ReactNode;
  loading?: boolean;
  long?: boolean;
  shape?: "circle" | "square";
  status?: "danger";
};

const consoleVariants: Partial<Record<NonNullable<ButtonProps["variant"]>, string>> = {
  default: "ui-btn-primary",
  outline: "ui-btn-outline",
  ghost: "ui-btn-text",
};

export function Button({
  asChild = false, icon, loading, long, shape, status,
  variant = "secondary", size = "default", type = "button", className, children, disabled, ...props
}: ButtonProps) {
  const variantClass = variant && consoleVariants[variant];
  const classes = cn("ui-btn", variantClass, long && "w-full", shape === "circle" && "rounded-full",
    status === "danger" && "ui-btn-status-danger text-red-700 dark:text-red-300",
    disabled && "ui-btn-disabled", loading && "ui-btn-loading", className);
  const actualSize = icon && !children
    ? size === "xs" ? "icon-xs" : size === "sm" ? "icon-sm" : size === "default" ? "icon" : size
    : size;
  return (
    <SeatButton asChild={asChild} type={asChild ? undefined : type} variant={variant} size={actualSize}
      className={classes} disabled={disabled || loading} aria-busy={loading || undefined} {...props}>
      {asChild ? children : <>
        {loading ? <LoaderCircle aria-hidden="true" className="ui-btn-icon animate-spin" size={16} /> :
          icon ? <span className="ui-btn-icon" aria-hidden="true">{icon}</span> : null}
        {children}
      </>}
    </SeatButton>
  );
}
