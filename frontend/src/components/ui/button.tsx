import { cva, type VariantProps } from "class-variance-authority";
import { LoaderCircle } from "lucide-react";
import { Slot } from "radix-ui";
import type { ComponentProps, ReactNode } from "react";
import { cn } from "../../lib/utils";

const buttonVariants = cva(
  "ui-btn inline-flex max-w-full items-center justify-center gap-2 rounded-lg text-sm font-medium whitespace-normal transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:cursor-not-allowed disabled:opacity-50 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "ui-btn-primary bg-primary text-primary-foreground hover:bg-primary-hover",
        outline: "ui-btn-outline border border-input bg-card text-foreground hover:bg-accent hover:text-accent-foreground",
        secondary: "bg-secondary text-secondary-foreground hover:bg-accent",
        ghost: "ui-btn-text text-foreground hover:bg-accent hover:text-accent-foreground",
      },
      size: {
        default: "min-h-10 px-4 py-2",
        sm: "min-h-8 px-3 py-1",
        xs: "min-h-7 px-2 py-1 text-xs",
      },
    },
    defaultVariants: { variant: "secondary", size: "default" },
  },
);

type ButtonProps = ComponentProps<"button"> & VariantProps<typeof buttonVariants> & {
  asChild?: boolean;
  icon?: ReactNode;
  loading?: boolean;
  long?: boolean;
  shape?: "circle" | "square";
  status?: "danger";
};

export function Button({
  asChild = false, icon, loading, long, shape, status,
  variant = "secondary", size = "default", type = "button", className, children, disabled, ...props
}: ButtonProps) {
  const classes = cn(buttonVariants({ variant, size }), long && "w-full", shape === "circle" && "rounded-full",
    status === "danger" && "ui-btn-status-danger text-red-700 dark:text-red-300",
    disabled && "ui-btn-disabled", loading && "ui-btn-loading", className);
  if (asChild) return <Slot.Root className={classes} {...props}>{children}</Slot.Root>;
  return (
    <button type={type} className={classes} disabled={disabled || loading} aria-busy={loading || undefined} {...props}>
      {loading ? <LoaderCircle aria-hidden="true" className="ui-btn-icon animate-spin" size={16} /> :
        icon ? <span className="ui-btn-icon" aria-hidden="true">{icon}</span> : null}
      {children}
    </button>
  );
}
