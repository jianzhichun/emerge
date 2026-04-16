import { writable } from 'svelte/store';

export interface UiState {
  activeModal: string | null;
}

const initialState: UiState = {
  activeModal: null
};

function createUiStore() {
  const { subscribe, update } = writable<UiState>(initialState);

  return {
    subscribe,
    setModal: (modal: string | null) =>
      update((state) => ({
        ...state,
        activeModal: modal
      }))
  };
}

export const uiStore = createUiStore();
