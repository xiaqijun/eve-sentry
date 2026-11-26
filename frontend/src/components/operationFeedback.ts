import { useToast } from "./ui/toast";

export function useOperationFeedback() {
  const toast = useToast();
  return {
    ...toast,
    operationError(reason: unknown, fallback: string) {
      toast.error(reason instanceof Error && reason.message ? reason.message : fallback);
    },
  };
}
