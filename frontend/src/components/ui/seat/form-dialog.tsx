import { useId, type FormEventHandler, type ReactNode } from "react";
import { msg } from "@/lib/i18n";
import { Modal } from "./dialog";
import { Button } from "./button";

// Short business forms share the existing Modal and keep actions outside scrolling content.
export function FormDialog({
  title,
  formLabel = title,
  close,
  busy = false,
  disabled = false,
  submitLabel = msg("保存"),
  destructive = false,
  size = "compact",
  className,
  children,
  extraActions,
  onSubmit,
  showSubmit = true,
}: {
  title: string;
  formLabel?: string;
  close: () => void;
  busy?: boolean;
  disabled?: boolean;
  submitLabel?: string;
  destructive?: boolean;
  size?: "compact" | "form";
  className?: string;
  children: ReactNode;
  extraActions?: ReactNode;
  onSubmit: FormEventHandler<HTMLFormElement>;
  showSubmit?: boolean;
}) {
  const id = useId();
  return (
    <Modal
      title={title}
      close={close}
      busy={busy}
      size={size}
      footer={
        <>
          {extraActions}
          <Button
            type="button"
            variant="outline"
            disabled={busy}
            autoFocus={destructive}
            onClick={close}
          >
            {msg("取消")}
          </Button>
          {showSubmit && (
            <Button
              type="submit"
              form={id}
              variant={destructive ? "destructive" : "default"}
              disabled={busy || disabled}
              aria-busy={busy}
            >
              {busy ? msg("正在提交") : submitLabel}
            </Button>
          )}
        </>
      }
    >
      <form
        id={id}
        className={className}
        aria-label={formLabel}
        onSubmit={(e) => {
          e.preventDefault();
          if (!busy && !disabled) onSubmit(e);
        }}
      >
        {children}
      </form>
    </Modal>
  );
}
