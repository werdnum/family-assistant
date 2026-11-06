import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { checkForServiceWorkerUpdate, scheduleServiceWorkerUpdates } from '../serviceWorkerUpdates';

function setHidden(hidden: boolean): void {
  Object.defineProperty(document, 'hidden', {
    configurable: true,
    get: () => hidden,
  });
}

describe('service worker update checks', () => {
  beforeEach(() => {
    setHidden(false);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
    setHidden(false);
  });

  it('calls update when the page is visible', async () => {
    const update = vi.fn().mockResolvedValue(undefined);

    await checkForServiceWorkerUpdate({ update });

    expect(update).toHaveBeenCalledTimes(1);
  });

  it('skips the update while the page is hidden', async () => {
    setHidden(true);
    const update = vi.fn().mockResolvedValue(undefined);

    await checkForServiceWorkerUpdate({ update });

    expect(update).not.toHaveBeenCalled();
  });

  it('logs an InvalidStateError rejection as a warning instead of rethrowing', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const error = new DOMException('Failed to update a ServiceWorker', 'InvalidStateError');
    const update = vi.fn().mockRejectedValue(error);

    await expect(checkForServiceWorkerUpdate({ update })).resolves.toBeUndefined();

    expect(warn).toHaveBeenCalledWith('Service worker update check failed:', error);
  });

  it('checks on each interval tick', async () => {
    vi.useFakeTimers();
    const update = vi.fn().mockResolvedValue(undefined);

    const id = scheduleServiceWorkerUpdates({ update }, 1000);
    await vi.advanceTimersByTimeAsync(2500);
    window.clearInterval(id);

    expect(update).toHaveBeenCalledTimes(2);
  });
});
