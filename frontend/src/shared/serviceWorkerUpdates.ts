export const SERVICE_WORKER_UPDATE_INTERVAL_MS = 60 * 60 * 1000;

/**
 * Ask the browser to re-check the service worker script.
 *
 * update() rejects with InvalidStateError while the registration is being
 * uninstalled or the page is frozen, and with a TypeError when the script
 * fetch fails offline. None of these need attention: the next check, or the
 * browser's own navigation-time check, picks the update up. They are logged
 * as warnings so they do not reach the unhandled-rejection reporter.
 */
export async function checkForServiceWorkerUpdate(
  registration: Pick<ServiceWorkerRegistration, 'update'>
): Promise<void> {
  if (document.hidden) {
    return;
  }
  try {
    await registration.update();
  } catch (error) {
    console.warn('Service worker update check failed:', error);
  }
}

export function scheduleServiceWorkerUpdates(
  registration: Pick<ServiceWorkerRegistration, 'update'>,
  intervalMs: number = SERVICE_WORKER_UPDATE_INTERVAL_MS
): number {
  return window.setInterval(() => {
    void checkForServiceWorkerUpdate(registration);
  }, intervalMs);
}
