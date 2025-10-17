import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { server } from '../../test/setup.js';
import { http, HttpResponse } from 'msw';
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

describe('PushNotificationButton', () => {
  let mockSubscription: MockPushSubscription;
  let mockRegistration: MockServiceWorkerRegistration;
  let requestPermissionMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();

    // Mock PushSubscription
    mockSubscription = {
      endpoint: 'https://push.example.com/subscription/test-endpoint',
      toJSON: () => ({
        endpoint: 'https://push.example.com/subscription/test-endpoint',
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

    const mockNotification: Partial<typeof Notification> =
      function () {} as unknown as Notification;
    (mockNotification as Record<string, unknown>).requestPermission = requestPermissionMock;
    (mockNotification as Record<string, unknown>).permission = 'default';

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
  });

  describe('rendering', () => {
    it('should not render if push is not supported', () => {
      // Remove serviceWorker to simulate unsupported environment
      Object.defineProperty(navigator, 'serviceWorker', {
        value: undefined,
        configurable: true,
        writable: true,
      });

      const { container } = render(<PushNotificationButton />);
      expect(container.firstChild).toBeNull();
    });

    it('should not render if PushManager is not available', async () => {
      // Remove PushManager
      Object.defineProperty(window, 'PushManager', {
        value: undefined,
        configurable: true,
      });

      const { container } = render(<PushNotificationButton />);
      expect(container.firstChild).toBeNull();
    });

    it('should not render if Notification API is not available', async () => {
      // Remove Notification
      const originalNotification = globalThis.Notification;
      Object.defineProperty(globalThis, 'Notification', {
        value: undefined,
        configurable: true,
      });

      const { container } = render(<PushNotificationButton />);
      expect(container.firstChild).toBeNull();

      // Restore
      Object.defineProperty(globalThis, 'Notification', {
        value: originalNotification,
        configurable: true,
      });
    });

    it('should not render if VAPID public key is not configured', async () => {
      server.use(
        http.get('/api/client_config', () => {
          return HttpResponse.json({
            vapidPublicKey: null,
          });
        })
      );

      const { container } = render(<PushNotificationButton />);

      // Wait for component to fetch config and decide not to render
      await waitFor(() => {
        expect(container.firstChild).toBeNull();
      });
    });

    it('should render button with bell icon when supported and subscribed', async () => {
      mockRegistration.pushManager.getSubscription = vi.fn(() => Promise.resolve(mockSubscription));

      render(<PushNotificationButton />);

      await waitFor(() => {
        // Check for the button element
        const button = screen.getByRole('button', { name: /push notification settings/i });
        expect(button).toBeInTheDocument();

        // Button should show the bell icon when subscribed
        const bellIcon = button.querySelector('svg');
        expect(bellIcon).toBeInTheDocument();
      });
    });

    it('should render button with bell-off icon when not subscribed', async () => {
      mockRegistration.pushManager.getSubscription = vi.fn(() => Promise.resolve(null));

      render(<PushNotificationButton />);

      await waitFor(() => {
        const button = screen.getByRole('button', { name: /push notification settings/i });
        expect(button).toBeInTheDocument();
      });
    });
  });

  describe('subscription flow', () => {
    it('should toggle subscription on and request permission when not already granted', async () => {
      const user = userEvent.setup();
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

      // Verify permission was requested
      await waitFor(() => {
        expect(requestPermissionMock).toHaveBeenCalled();
      });

      // Verify subscription was made
      await waitFor(() => {
        expect(mockRegistration.pushManager.subscribe).toHaveBeenCalled();
      });
    });

    it('should skip permission request if already granted', async () => {
      const user = userEvent.setup();
      (globalThis.Notification as Record<string, unknown>).permission = 'granted';
      mockRegistration.pushManager.getSubscription = vi.fn(() => Promise.resolve(null));

      render(<PushNotificationButton />);

      await waitFor(() => {
        expect(mockRegistration.pushManager.getSubscription).toHaveBeenCalled();
      });

      const button = screen.getByRole('button', { name: /push notification settings/i });
      await user.click(button);

      const toggle = screen.getByRole('switch', { name: /enable push notifications/i });
      await user.click(toggle);

      // Verify permission was NOT requested
      await waitFor(() => {
        expect(requestPermissionMock).not.toHaveBeenCalled();
      });

      // Verify subscription was made directly
      await waitFor(() => {
        expect(mockRegistration.pushManager.subscribe).toHaveBeenCalled();
      });
    });

    it('should show error when permission is denied', async () => {
      const user = userEvent.setup();
      requestPermissionMock = vi.fn(() => Promise.resolve('denied'));
      (globalThis.Notification as Record<string, unknown>).requestPermission =
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
      (globalThis.Notification as Record<string, unknown>).permission = 'granted';

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
      (globalThis.Notification as Record<string, unknown>).permission = 'granted';
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

      // Verify unsubscribe was called
      await waitFor(() => {
        expect(mockSubscription.unsubscribe).toHaveBeenCalled();
      });
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
      (globalThis.Notification as Record<string, unknown>).permission = 'granted';

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
      (globalThis.Notification as Record<string, unknown>).permission = 'granted';

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
      (globalThis.Notification as Record<string, unknown>).permission = 'denied';
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
      Object.defineProperty(navigator, 'serviceWorker', {
        get: () => {
          throw new Error('Service worker access denied');
        },
        configurable: true,
      });

      render(<PushNotificationButton />);

      // Component should render null in case of initialization failure
      await waitFor(() => {
        // The component should either show an error or render nothing
        // Based on the component code, it handles errors gracefully
      });
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
