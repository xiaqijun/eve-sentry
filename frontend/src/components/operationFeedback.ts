import { useToast } from "./ui/seat/toast-context";

export function useOperationFeedback() {
  const toast = useToast();
  return {
    success(message: string) {
      toast.show({ message, tone: "success", duration: 3000 });
    },
    warning(message: string) {
      toast.show({ message, tone: "info", duration: 5000 });
    },
    operationError(reason: unknown, fallback: string) {
      toast.show({
        message: reason instanceof Error && reason.message ? reason.message : fallback,
        tone: "error",
        duration: 5000,
      });
    },
  };
}
