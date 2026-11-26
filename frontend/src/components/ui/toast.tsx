import { CircleAlert, CircleCheck, Info, TriangleAlert, X } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

type Tone = "success" | "error" | "warning" | "info";
type Notice = { id: number; tone: Tone; message: string; duration: number };
type ShowToast = (tone: Tone, message: string) => void;
const ToastContext = createContext<ShowToast | null>(null);

export function useToast() {
  const show = useContext(ToastContext);
  if (!show) throw new Error("useToast must be used inside ToastProvider");
  return useMemo(() => ({
    success: (message: string) => show("success", message),
    error: (message: string) => show("error", message),
    warning: (message: string) => show("warning", message),
    info: (message: string) => show("info", message),
  }), [show]);
}

const TOAST_ICONS = { success: CircleCheck, warning: TriangleAlert, error: CircleAlert, info: Info };

function Toast({ item, dismiss }: { item: Notice; dismiss: (id: number) => void }) {
  useEffect(() => {
    const timer = window.setTimeout(() => dismiss(item.id), item.duration);
    return () => window.clearTimeout(timer);
  }, [item, dismiss]);
  const Icon = TOAST_ICONS[item.tone];
  return <div className={`toast toast-${item.tone}`} role={item.tone === "error" ? "alert" : "status"}>
    <Icon className="toast-icon" size={18} aria-hidden="true" />
    <span>{item.message}</span>
    <button type="button" aria-label="关闭通知" className="toast-close" onClick={() => dismiss(item.id)}><X aria-hidden="true" size={16} /></button>
  </div>;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [notices, setNotices] = useState<Notice[]>([]);
  const sequence = useRef(0);
  const dismiss = useCallback((id: number) => setNotices((items) => items.filter((item) => item.id !== id)), []);
  const show = useCallback<ShowToast>((tone, message) => {
    const item = { id: sequence.current++, tone, message, duration: tone === "error" || tone === "warning" ? 5000 : 3000 };
    setNotices((items) => [...items.slice(-2), item]);
  }, []);
  return <ToastContext.Provider value={show}>
    {children}
    {createPortal(<div className="toast-viewport" aria-label="通知">
      {notices.map((item) => <Toast key={item.id} item={item} dismiss={dismiss} />)}
    </div>, document.body)}
  </ToastContext.Provider>;
}
