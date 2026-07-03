import { create } from "zustand";

type TabKey = "reports" | "alerts";

interface WorkbenchState {
  filterText: string;
  activeTab: TabKey;
  selectedSystemId: number | null;
  expandedAlertIds: string[];
  setFilterText: (value: string) => void;
  setActiveTab: (value: TabKey) => void;
  setSelectedSystemId: (value: number | null) => void;
  toggleAlertExpanded: (alertId: string) => void;
}

export const useWorkbenchStore = create<WorkbenchState>((set) => ({
  filterText: "",
  activeTab: "alerts",
  selectedSystemId: null,
  expandedAlertIds: [],
  setFilterText: (value) => set({ filterText: value }),
  setActiveTab: (value) => set({ activeTab: value }),
  setSelectedSystemId: (value) => set({ selectedSystemId: value }),
  toggleAlertExpanded: (alertId) =>
    set((state) => ({
      expandedAlertIds: state.expandedAlertIds.includes(alertId)
        ? state.expandedAlertIds.filter((item) => item !== alertId)
        : [...state.expandedAlertIds, alertId],
    })),
}));
