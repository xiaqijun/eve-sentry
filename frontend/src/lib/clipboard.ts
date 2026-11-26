export async function copyToClipboard(value: string): Promise<void> {
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }

  const focused = document.activeElement;
  const field = document.createElement("textarea");
  field.value = value;
  field.readOnly = true;
  field.style.position = "fixed";
  field.style.opacity = "0";
  field.setAttribute("aria-hidden", "true");
  document.body.append(field);
  try {
    field.select();
    if (!document.execCommand("copy")) throw new DOMException("Clipboard write failed", "NotAllowedError");
  } finally {
    field.remove();
    if (focused instanceof HTMLElement) focused.focus();
  }
}
