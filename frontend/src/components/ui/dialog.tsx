import { useRef, type CSSProperties, type ReactNode } from "react";
import { Dialog as Primitive } from "radix-ui";
import { X } from "lucide-react";
import { cn } from "../../lib/utils";
import { Button } from "./button";

type DialogProps = {
  open: boolean;
  title: ReactNode;
  label?: string;
  id?: string;
  children: ReactNode;
  footer?: ReactNode;
  className?: string;
  style?: CSSProperties;
  width?: string | number;
  busy?: boolean;
  onClose: () => void;
  restoreFocus?: () => void;
};

export function Drawer({ open, title, label, id, children, footer, className, style, width, busy, onClose, restoreFocus, placement = "right" }:
  DialogProps & { placement?: "left" | "right" }) {
  const opener = useRef<HTMLElement | null>(null);
  const titleContent = label ? <><Primitive.Title className="sr-only">{label}</Primitive.Title>{title}</> :
    <Primitive.Title className="ui-dialog-title">{title}</Primitive.Title>;
  return <Primitive.Root open={open} onOpenChange={(next) => { if (!next && !busy) onClose(); }}>
    <Primitive.Portal>
      <Primitive.Overlay className="ui-dialog-overlay" />
      <Primitive.Content id={id} aria-label={label ?? (typeof title === "string" ? title : undefined)}
        aria-modal="true" aria-describedby={undefined} aria-busy={busy || undefined}
        className={cn("ui-dialog-surface ui-drawer", className)}
        data-placement={placement} style={{ width, ...style }}
        onOpenAutoFocus={() => { opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; }}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          if (restoreFocus) restoreFocus();
          else if (opener.current?.isConnected) opener.current.focus();
        }}
        onPointerDownOutside={(event) => { if (busy) event.preventDefault(); }}
        onEscapeKeyDown={(event) => { if (busy) event.preventDefault(); }}>
        <div className="ui-drawer-content">
          <header className="ui-dialog-header ui-drawer-header">
            {titleContent}
            <Primitive.Close asChild>
              <Button aria-label="关闭" title="关闭" variant="ghost" size="sm" disabled={busy} icon={<X size={18} />} />
            </Primitive.Close>
          </header>
          <div className="ui-dialog-body ui-drawer-body">{children}</div>
          {footer ? <div className="ui-dialog-footer">{footer}</div> : null}
        </div>
      </Primitive.Content>
    </Primitive.Portal>
  </Primitive.Root>;
}
