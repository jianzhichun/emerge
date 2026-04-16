import { writable } from 'svelte/store';

export const TAB_IDS = ['overview', 'monitors', 'session', 'state', 'connector'] as const;

export type TabId = (typeof TAB_IDS)[number];

export interface UiState {
  activeTab: TabId;
  activeModal: string | null;
  connectorPanels: Record<string, boolean>;
}

const initialState: UiState = {
  activeTab: 'overview',
  activeModal: null,
  connectorPanels: {}
};

function normalizeTab(tab: string | null | undefined): TabId {
  if (tab && TAB_IDS.includes(tab as TabId)) {
    return tab as TabId;
  }
  return initialState.activeTab;
}

function createUiStore() {
  const { subscribe, update, set } = writable<UiState>(initialState);

  return {
    subscribe,
    reset: () => set(initialState),
    setTab: (tab: string | null | undefined) =>
      update((state) => ({
        ...state,
        activeTab: normalizeTab(tab)
      })),
    setModal: (modal: string | null) =>
      update((state) => ({
        ...state,
        activeModal: modal
      })),
    setConnectorPanel: (connector: string, expanded: boolean) =>
      update((state) => ({
        ...state,
        connectorPanels: {
          ...state.connectorPanels,
          [connector]: expanded
        }
      })),
    toggleConnectorPanel: (connector: string) =>
      update((state) => ({
        ...state,
        connectorPanels: {
          ...state.connectorPanels,
          [connector]: !state.connectorPanels[connector]
        }
      }))
  };
}

export const uiStore = createUiStore();
