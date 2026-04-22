export interface RefreshScheduler {
  epoch: number;
  schedule: () => void;
}

export function createRefreshScheduler(run: () => Promise<void>): RefreshScheduler {
  let epoch = 0;
  let inFlight = false;
  let pending = false;

  async function pump(): Promise<void> {
    if (inFlight) {
      pending = true;
      return;
    }
    inFlight = true;
    do {
      pending = false;
      epoch += 1;
      await run();
    } while (pending);
    inFlight = false;
  }

  return {
    get epoch() {
      return epoch;
    },
    schedule: () => {
      void pump();
    }
  };
}
