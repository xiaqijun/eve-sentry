import { msg } from "@/lib/i18n";
import { useState, type ReactNode } from "react";
import { AlertDialog, Dialog } from "radix-ui";
import { X } from "lucide-react";
import { Button } from "./button";
import { IconAction } from "./icon-action";
import { cn } from "@/lib/utils";
import "./dialog.css";

function useReturnFocus() {
  const [opener] = useState(() => document.activeElement);
  return (event: Event) => {
    if (opener instanceof HTMLElement && opener.isConnected) {
      event.preventDefault();
      opener.focus();
    }
  };
}

export function Modal({
  title,
  close,
  children,
  footer,
  busy = false,
  className,
  size = "form",
  onEscapeKeyDown,
}: {
  title: string;
  close: () => void;
  children: ReactNode;
  footer?: ReactNode;
  busy?: boolean;
  className?: string;
  size?: "compact" | "form";
  onEscapeKeyDown?: (event: KeyboardEvent) => void;
}) {
  const restoreFocus = useReturnFocus();
  return (
    <Dialog.Root
      open
      onOpenChange={(open) => {
        if (!open && !busy) close();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="ui-dialog-overlay" />
        <Dialog.Content
          className={cn("ui-dialog-surface ui-modal", className)}
          data-size={size}
          aria-describedby={undefined}
          aria-busy={busy}
          onCloseAutoFocus={restoreFocus}
          onEscapeKeyDown={(e) => {
            if (busy) e.preventDefault();
            else onEscapeKeyDown?.(e);
          }}
          onPointerDownOutside={(e) => {
            if (busy) e.preventDefault();
          }}
        >
          <header className="ui-modal-header">
            <Dialog.Title>{title}</Dialog.Title>
            <IconAction label={msg("关闭")} onClick={close} disabled={busy}>
              <X size={18} />
            </IconAction>
          </header>
          <div className="ui-modal-body">{children}</div>
          {footer && <footer className="ui-modal-footer">{footer}</footer>}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function ConfirmDialog({
  title,
  description,
  busy = false,
  error,
  onConfirm,
  onClose,
  confirmLabel = msg("确认"),
}: {
  title: string;
  description: ReactNode;
  busy?: boolean;
  error?: string;
  onConfirm: () => void;
  onClose: () => void;
  confirmLabel?: string;
}) {
  const restoreFocus = useReturnFocus();
  return (
    <AlertDialog.Root
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="ui-dialog-overlay" />
        <AlertDialog.Content
          className="ui-dialog-surface ui-confirm"
          onCloseAutoFocus={restoreFocus}
          aria-busy={busy}
          onEscapeKeyDown={(e) => {
            if (busy) e.preventDefault();
          }}
        >
          <AlertDialog.Title>{title}</AlertDialog.Title>
          <AlertDialog.Description className="ui-dialog-description">
            {description}
          </AlertDialog.Description>
          {error && (
            <p role="alert" className="ui-dialog-error">
              {error}
            </p>
          )}
          <div className="ui-dialog-actions">
            <AlertDialog.Cancel asChild>
              <Button variant="outline" disabled={busy}>
                {msg("取消")}
              </Button>
            </AlertDialog.Cancel>
            <Button
              variant="destructive"
              disabled={busy}
              aria-busy={busy}
              onClick={onConfirm}
            >
              {busy ? msg("正在处理") : confirmLabel}
            </Button>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
