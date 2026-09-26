import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { server } from '../../test/setup.js';
import { PushNotificationButton } from '../PushNotificationButton';

// Mock PushSubscription and ServiceWorkerRegistration
interface MockPushSubscription {
  endpoint: string;
  toJSON: () => { endpoint: string; keys: { p256dh: string; auth: string } };
  unsubscribe: () => Promise<boolean>;
}

interface MockServiceWorkerRegistration {
  pushManager: {
    subscribe: (options: {
      userVisibleOnly: boolean;
      applicationServerKey: Uint8Array;
    }) => Promise<MockPushSubscription>;
    getSubscription: () => Promise<MockPushSubscription | null>;
  };
}

const TEST_ENDPOINT = 'https://push.example.com/subscription/test-endpoint';

function recordPushBackendRequests(): { subscribe: unknown[]; unsubscribe: unknown[] } {
  const bodies: { subscribe: unknown[]; unsubscribe: unknown[] } = {
    subscribe: [],
    unsubscribe: [],
  };
  server.use(
    http.post('/api/push/subscribe', async ({ request }) => {
      bodies.subscribe.push(await request.json());
      return HttpResponse.json({ status: 'success', id: 'sub_test' });
    }),
    http.post('/api/push/unsubscribe', async ({ request }) => {
      bodies.unsubscribe.push(await request.json());
      return HttpResponse.json({ status: 'success' });
    })
  );
  return bodies;
}

describe('PushNotificationButton', () => {
  let mockSubscription: MockPushSubscription;
  let mockRegistration: MockServiceWorkerRegistration;
  let requestPermissionMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();

    // Mock PushSubscription
    mockSubscription = {
      endpoint: TEST_ENDPOINT,
      toJSON: () => ({
        endpoint: TEST_ENDPOINT,
        keys: {
          p256dh: 'test-p256dh-key',
          auth: 'test-auth-key',
        },
      }),
      unsubscribe: vi.fn(() => Promise.resolve(true)),
    };

    // Mock ServiceWorkerRegistration
    mockRegistration = {
      pushManager: {
        subscribe: vi.fn(() => Promise.resolve(mockSubscription)),
        getSubscription: vi.fn(() => Promise.resolve(null)),
      },
    };

    // Create global Notification mock if it doesn't exist
    requestPermissionMock = vi.fn(() => Promise.resolve('granted'));

    const mockNotification = function () {} as unknown as Record<string, unknown>;
    mockNotification.requestPermission = requestPermissionMock;
    mockNotification.permission = 'default';

    // Set Notification on globalThis
    Object.defineProperty(globalThis, 'Notification', {
      value: mockNotification,
      configurable: true,
      writable: true,
    });

    // Mock navigator.serviceWorker
    Object.defineProperty(navigator, 'serviceWorker', {
      value: {
        ready: Promise.resolve(mockRegistration),
        controller: null,
      },
      configurable: true,
      writable: true,
    });

    // Ensure window has required APIs
    Object.defineProperty(window, 'PushManager', {
      value: {},
      configurable: true,
    });
  });

  afterEach(() => {
    server.resetHandlers();
    vi.restoreAllMocks();
  });

  describe('rendering', () => {
    // The unsupported cases assert that no request was made synchronously after
    // mount; this control proves a supported mount does make it by then.
    it('should request push settings as soon as it mounts in a supported browser', async () => {
      const fetchSpy = vi.spyOn(globalThis, 'fetch');

      render(<PushNotificationButton />);

      expect(fetchSpy).toHaveBeenCalledWith('/api/client_config');
      expect(
        await screen.findByRole('button', { name: /push notification settings/i })
      ).toBeInTheDocument();
    });

    it.each([
      ['service workers', () => Reflect.deleteProperty(navigator, 'serviceWorker')],
      ['the Push API', () => Reflect.deleteProperty(window, 'PushManager')],
      ['the Notification API', () => Reflect.deleteProperty(globalThis, 'Notification')],
    ])(
      'should not render or request push settings when the browser lacks %s',
      (_api: string, removeApi: () => boolean) => {
        const fetchSpy = vi.spyOn(globalThis, 'fetch');
        removeApi();

        const { container } = render(<PushNotificationButton />);

        expect(fetchSpy).not.toHaveBeenCalled();
        expect(container).toBeEmptyDOMElement();
      }
    );

    it('should not render if VAPID public key is not configured', async () => {
      server.use(
        http.get('/api/client_config', () => {
          return HttpResponse.json({
            vapidPublicKey: null,
          });
        })
      );

      const { container } = render(<PushNotificationButton />);

      await waitFor(() => {
        expect(mockRegistration.pushManager.getSubscription).toHaveBeenCalled();
      });
      expect(container).toBeEmptyDOMElement();
    });

    it.each([
      { state: 'subscribed', existing: () => mockSubscription, icon: 'lucide-bell', checked: true },
      { state: 'not subscribed', existing: () => null, icon: 'lucide-bell-off', checked: false },
    ])(
      'should reflect the existing subscription in the icon and toggle when $state',
      async ({
        existing,
        icon,
        checked,
      }: {
        existing: () => MockPushSubscription | null;
        icon: string;
        checked: boolean;
      }) => {
        const user = userEvent.setup();
        mockRegistration.pushManager.getSubscription = vi.fn(() => Promise.resolve(existing()));

        render(<PushNotificationButton />);

        await waitFor(() => {
          expect(mockRegistration.pushManager.getSubscription).toHaveBeenCalled();
        });

        const button = screen.getByRole('button', { name: /push notification settings/i });
        expect(button.querySelector('svg')).toHaveClass(icon);

        await user.click(button);

        expect(screen.getByRole('switch', { name: /enable push notifications/i })).toHaveAttribute(
          'aria-checked',
          String(checked)
        );
      }
    );
  });

  describe('subscription flow', () => {
    it('should toggle subscription on and request permission when not already granted', async () => {
      const user = userEvent.setup();
      const backend = recordPushBackendRequests();
      mockRegistration.pushManager.getSubscription = vi.fn(() => Promise.resolve(null));

      render(<PushNotificationButton />);

      // Wait for component to initialize
      await waitFor(() => {
        expect(mockRegistration.pushManager.getSubscription).toHaveBeenCalled();
      });

      // Click the button to open dropdown
      const button = screen.getByRole('button', { name: /push notification settings/i });
      await user.click(button);

      // Find and click the toggle switch
      const toggle = screen.getByRole('switch', { name: /enable push notifications/i });
      await user.click(toggle);

      await waitFor(() => {
        expect(backend.subscribe).toEqual([
          {
            subscription: {
              endpoint: TEST_ENDPOINT,
              keys: { p256dh: 'test-p256dh-key', auth: 'test-auth-key' },
            },
          },
        ]);
      });
      expect(requestPermissionMock).toHaveBeenCalled();
    });

    it('should skip permission request if already granted', async () => {
      const user = userEvent.setup();
      (globalThis.Notification as unknown as Record<string, unknown>).permission = 'granted';
      mockRegistration.pushManager.getSubscription = vi.fn(() => Promise.resolve(null));

      render(<PushNotificationButton />);

      await waitFor(() => {
        expect(mockRegistration.pushManager.getSubscription).toHaveBeenCalled();
      });

      const button = screen.getByRole('button', { name: /push notification settings/i });
      await user.click(button);

      const toggle = screen.getByRole('switch', { name: /enable push notifications/i });
      await user.click(toggle);

      await waitFor(() => {
        expect(mockRegistration.pushManager.subscribe).toHaveBeenCalled();
      });
      expect(requestPermissionMock).not.toHaveBeenCalled();
    });

    it('should show error when permission is denied', async () => {
      const user = userEvent.setup();
      requestPermissionMock = vi.fn(() => Promise.resolve('denied'));
      (globalThis.Notification as unknown as Record<string, unknown>).requestPermission =
        requestPermissionMock;
      mockRegistration.pushManager.getSubscription = vi.fn(() => Promise.resolve(null));

      render(<PushNotificationButton />);

      await waitFor(() => {
        expect(mockRegistration.pushManager.getSubscription).toHaveBeenCalled();
      });

      const button = screen.getByRole('button', { name: /push notification settings/i });
      await user.click(button);

      const toggle = screen.getByRole('switch', { name: /enable push notifications/i });
      await user.click(toggle);

      // Verify error message is shown
      await waitFor(() => {
        expect(screen.getByText(/notification permission denied/i)).toBeInTheDocument();
      });

      // Verify subscription was NOT made
      expect(mockRegistration.pushManager.subscribe).not.toHaveBeenCalled();
    });

    it('should handle subscription API errors gracefully', async () => {
      const user = userEvent.setup();
      (globalThis.Notification as unknown as Record<string, unknown>).permission = 'granted';

      const subscribeError = new Error('Failed to subscribe');
      mockRegistration.pushManager.subscribe = vi.fn(() => Promise.reject(subscribeError));
      mockRegistration.pushManager.getSubscription = vi.fn(() => Promise.resolve(null));

      render(<PushNotificationButton />);

      await waitFor(() => {
        expect(mockRegistration.pushManager.getSubscription).toHaveBeenCalled();
      });

      const button = screen.getByRole('button', { name: /push notification settings/i });
      await user.click(button);

      const toggle = screen.getByRole('switch', { name: /enable push notifications/i });
      await user.click(toggle);

      // Verify error message is shown
      await waitFor(() => {
        expect(screen.getByText(/failed to subscribe/i)).toBeInTheDocument();
      });
    });

    it('should update status badge to Active after successful subscription', async () => {
      const user = userEvent.setup();
      (globalThis.Notification as unknown as Record<string, unknown>).permission = 'granted';
      mockRegistration.pushManager.getSubscription = vi.fn(() => Promise.resolve(null));

      render(<PushNotificationButton />);

      await waitFor(() => {
        expect(mockRegistration.pushManager.getSubscription).toHaveBeenCalled();
      });

      const button = screen.getByRole('button', { name: /push notification settings/i });
      await user.click(button);

      // Status should initially be Inactive
      expect(screen.getByText('Inactive')).toBeInTheDocument();

      const toggle = screen.getByRole('switch', { name: /enable push notifications/i });
      await user.click(toggle);

      // Status should change to Active after successful subscription
      await waitFor(() => {
        expect(screen.getByText('Active')).toBeInTheDocument();
      });
    });
  });

  describe('unsubscription flow', () => {
    it('should unsubscribe when toggle is turned off', async () => {
      const user = userEvent.setup();
      const backend = recordPushBackendRequests();
      mockRegistration.pushManager.getSubscription = vi.fn(() => Promise.resolve(mockSubscription));

      render(<PushNotificationButton />);

      await waitFor(() => {
        expect(mockRegistration.pushManager.getSubscription).toHaveBeenCalled();
      });

      const button = screen.getByRole('button', { name: /push notification settings/i });
      await user.click(button);

      // Status should be Active when subscribed
      expect(screen.getByText('Active')).toBeInTheDocument();

      const toggle = screen.getByRole('switch', { name: /enable push notifications/i });
      await user.click(toggle);

      await waitFor(() => {
        expect(backend.unsubscribe).toEqual([{ endpoint: TEST_ENDPOINT }]);
      });
      expect(mockSubscription.unsubscribe).toHaveBeenCalled();
    });

    it('should update status badge to Inactive after unsubscription', async () => {
      const user = userEvent.setup();
      mockRegistration.pushManager.getSubscription = vi.fn(() => Promise.resolve(mockSubscription));

      render(<PushNotificationButton />);

      await waitFor(() => {
        expect(mockRegistration.pushManager.getSubscription).toHaveBeenCalled();
      });

      const button = screen.getByRole('button', { name: /push notification settings/i });
      await user.click(button);

      expect(screen.getByText('Active')).toBeInTheDocument();

      const toggle = screen.getByRole('switch', { name: /enable push notifications/i });
      await user.click(toggle);

      // Status should change back to Inactive
      await waitFor(() => {
        expect(screen.getByText('Inactive')).toBeInTheDocument();
      });
    });

    it('should handle unsubscription errors gracefully', async () => {
      const user = userEvent.setup();
      const unsubscribeError = new Error('Unsubscribe failed');
      mockSubscription.unsubscribe = vi.fn(() => Promise.reject(unsubscribeError));
      mockRegistration.pushManager.getSubscription = vi.fn(() => Promise.resolve(mockSubscription));

      render(<PushNotificationButton />);

      await waitFor(() => {
        expect(mockRegistration.pushManager.getSubscription).toHaveBeenCalled();
      });

      const button = screen.getByRole('button', { name: /push notification settings/i });
      await user.click(button);

      const toggle = screen.getByRole('switch', { name: /enable push notifications/i });
      await user.click(toggle);

      // Verify error message is shown
      await waitFor(() => {
        expect(screen.getByText(/unsubscribe failed/i)).toBeInTheDocument();
      });
    });
  });

  describe('loading and error states', () => {
    it('should show loading state while subscribing', async () => {
      const user = userEvent.setup();
      (globalThis.Notification as unknown as Record<string, unknown>).permission = 'granted';

      // Make subscribe take a while
      let resolveSubscribe: () => void;
      const subscribePromise = new Promise<MockPushSubscription>((resolve) => {
        resolveSubscribe = () => resolve(mockSubscription);
      });
      mockRegistration.pushManager.subscribe = vi.fn(() => subscribePromise);
      mockRegistration.pushManager.getSubscription = vi.fn(() => Promise.resolve(null));

      render(<PushNotificationButton />);

      await waitFor(() => {
        expect(mockRegistration.pushManager.getSubscription).toHaveBeenCalled();
      });

      const button = screen.getByRole('button', { name: /push notification settings/i });
      await user.click(button);

      const toggle = screen.getByRole('switch', { name: /enable push notifications/i });
      await user.click(toggle);

      // Toggle should be disabled while loading
      await waitFor(() => {
        expect(toggle).toBeDisabled();
      });

      // Resolve the subscription
      resolveSubscribe!();

      // Toggle should be enabled after loading
      await waitFor(() => {
        expect(toggle).not.toBeDisabled();
      });
    });

    it('should show error when backend subscription fails', async () => {
      const user = userEvent.setup();
      (globalThis.Notification as unknown as Record<string, unknown>).permission = 'granted';

      // Mock backend error
      server.use(
        http.post('/api/push/subscribe', () => {
          return HttpResponse.json(
            { status: 'error', message: 'Backend subscription failed' },
            { status: 500 }
          );
        })
      );

      mockRegistration.pushManager.getSubscription = vi.fn(() => Promise.resolve(null));

      render(<PushNotificationButton />);

      await waitFor(() => {
        expect(mockRegistration.pushManager.getSubscription).toHaveBeenCalled();
      });

      const button = screen.getByRole('button', { name: /push notification settings/i });
      await user.click(button);

      const toggle = screen.getByRole('switch', { name: /enable push notifications/i });
      await user.click(toggle);

      // Verify error message is shown
      await waitFor(() => {
        expect(screen.getByText(/failed to subscribe to push/i)).toBeInTheDocument();
      });
    });

    it('should disable toggle when permission is denied', async () => {
      const user = userEvent.setup();
      (globalThis.Notification as unknown as Record<string, unknown>).permission = 'denied';
      mockRegistration.pushManager.getSubscription = vi.fn(() => Promise.resolve(null));

      render(<PushNotificationButton />);

      await waitFor(() => {
        expect(mockRegistration.pushManager.getSubscription).toHaveBeenCalled();
      });

      const button = screen.getByRole('button', { name: /push notification settings/i });
      await user.click(button);

      const toggle = screen.getByRole('switch', { name: /enable push notifications/i });

      // Toggle should be disabled when permission is denied
      expect(toggle).toBeDisabled();
    });

    it('should show error when initialization fails', async () => {
      const user = userEvent.setup();
      Object.defineProperty(navigator, 'serviceWorker', {
        get: () => {
          throw new Error('Service worker access denied');
        },
        configurable: true,
      });

      render(<PushNotificationButton />);

      await user.click(await screen.findByRole('button', { name: /push notification settings/i }));

      expect(
        await screen.findByText('Failed to load push notification settings')
      ).toBeInTheDocument();
    });
  });

  describe('dropdown menu', () => {
    it('should display help text and explanations', async () => {
      const user = userEvent.setup();
      mockRegistration.pushManager.getSubscription = vi.fn(() => Promise.resolve(null));

      render(<PushNotificationButton />);

      await waitFor(() => {
        expect(mockRegistration.pushManager.getSubscription).toHaveBeenCalled();
      });

      const button = screen.getByRole('button', { name: /push notification settings/i });
      await user.click(button);

      // Check for help text - text may be split across elements
      expect(screen.getByText(/allow you to receive messages/i)).toBeInTheDocument();
      expect(screen.getByText(/requires browser notification permissions/i)).toBeInTheDocument();
    });

    it('should show status label and badge', async () => {
      const user = userEvent.setup();
      mockRegistration.pushManager.getSubscription = vi.fn(() => Promise.resolve(mockSubscription));

      render(<PushNotificationButton />);

      await waitFor(() => {
        expect(mockRegistration.pushManager.getSubscription).toHaveBeenCalled();
      });

      const button = screen.getByRole('button', { name: /push notification settings/i });
      await user.click(button);

      // Check for status elements
      expect(screen.getByText('Status')).toBeInTheDocument();
      expect(screen.getByText('Active')).toBeInTheDocument();
    });
  });
});
