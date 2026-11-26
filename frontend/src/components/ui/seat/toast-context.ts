import { createContext, useContext } from "react";

export type ToastTone = "success" | "error" | "info";
export type ToastInput = {
  message: string;
  title?: string;
  tone?: ToastTone;
  duration?: number;
};
export type ToastApi = {
  show: (input: ToastInput) => string;
  success: (message: string, title?: string) => string;
  error: (message: string, title?: string) => string;
  info: (message: string, title?: string) => string;
  dismiss: (id: string) => void;
};

export const ToastContext = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const value = useContext(ToastContext);
  if (!value) throw new Error("useToast must be used inside ToastProvider");
  return value;
}
