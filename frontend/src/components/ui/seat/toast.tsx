import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { AlertCircle, CheckCircle2, Info, X } from "lucide-react";
import { msg } from "@/lib/i18n";
import { ToastContext, type ToastInput } from "./toast-context";
import "./toast.css";

type ToastItem = Required<Pick<ToastInput, "message" | "tone" | "duration">> & {
  id: string;
  title?: string;
};
export function ToastProvider({ children }: { children: ReactNode }) {
  const sequence = useRef(0);
  const [items, setItems] = useState<ToastItem[]>([]);
  const dismiss = useCallback((id: string) => {
    setItems((current) => current.filter((item) => item.id !== id));
  }, []);
  const show = useCallback((input: ToastInput) => {
    const tone = input.tone ?? "info";
    const duration = input.duration ?? (tone === "error" ? 6000 : 4000);
    const id = `toast-${Date.now()}-${sequence.current++}`;
    setItems((current) => [...current.slice(-3), { ...input, id, tone, duration }]);
    return id;
  }, []);
  const success = useCallback((message: string, title?: string) => show({ message, title, tone: "success" }), [show]);
  const error = useCallback((message: string, title?: string) => show({ message, title, tone: "error" }), [show]);
  const info = useCallback((message: string, title?: string) => show({ message, title, tone: "info" }), [show]);
  const value = useMemo(() => ({ show, success, error, info, dismiss }), [show, success, error, info, dismiss]);

  useEffect(() => {
    const timers = items.map((item) => window.setTimeout(() => dismiss(item.id), item.duration));
    return () => timers.forEach((timer) => window.clearTimeout(timer));
  }, [items, dismiss]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-viewport" aria-label={msg("通知")}>
        {items.map((item) => {
          const Icon = item.tone === "success" ? CheckCircle2 : item.tone === "error" ? AlertCircle : Info;
          return (
            <div className={`toast toast-${item.tone}`} key={item.id} role={item.tone === "error" ? "alert" : "status"}>
              <Icon className="toast-icon" size={18} aria-hidden="true" />
              <div className="toast-copy">
                {item.title && <strong>{item.title}</strong>}
                <span>{item.message}</span>
              </div>
              <button className="toast-close" type="button" aria-label={msg("关闭通知")} onClick={() => dismiss(item.id)}>
                <X size={15} aria-hidden="true" />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}
