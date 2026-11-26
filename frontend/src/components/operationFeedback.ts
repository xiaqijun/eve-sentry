import { Message } from "@arco-design/web-react";

export function showOperationError(reason: unknown, fallback: string) {
  Message.error({
    content: reason instanceof Error && reason.message ? reason.message : fallback,
    duration: 5000,
  });
}
