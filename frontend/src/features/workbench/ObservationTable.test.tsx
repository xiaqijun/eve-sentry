import "@testing-library/jest-dom/vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, test } from "vitest";

import { ObservationTable } from "./ObservationTable";
import type { PilotObservation } from "./types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
  .IS_REACT_ACT_ENVIRONMENT = true;

function renderTable(observations: PilotObservation[]) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);

  act(() => {
    root.render(<ObservationTable observations={observations} />);
  });

  return {
    container,
    cleanup: () => {
      act(() => {
        root.unmount();
      });
      container.remove();
    },
  };
}

describe("ObservationTable", () => {
  test("renders clear realtime observation column labels", () => {
    const { container, cleanup } = renderTable([
      {
        id: "pilot-one",
        pilotName: "Pilot One",
        systemName: "0-UVHJ",
        systemId: 30003615,
        systemIds: [30003615],
        sources: ["预警频道", "本地OCR"],
        level: "high",
        latestSeen: "2026-07-02T12:04:00Z",
        evidenceCount: 2,
      },
    ]);

    expect(container).toHaveTextContent("飞行员");
    expect(container).toHaveTextContent("星系");
    expect(container).toHaveTextContent("来源");
    expect(container).toHaveTextContent("威胁");
    expect(container).toHaveTextContent("最近出现");
    expect(container).toHaveTextContent("次数");
    expect(container).toHaveTextContent("预警频道 / 本地OCR");
    expect(container.querySelector("tbody tr td:nth-child(6)")?.textContent).toBe("2");

    cleanup();
  });

  test("renders clear realtime empty state copy", () => {
    const { container, cleanup } = renderTable([]);

    expect(container).toHaveTextContent("暂无实时敌对目标");

    cleanup();
  });
});
