import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useNotifications } from '../useNotifications';

describe('useNotifications', () => {
  let originalNotification: typeof Notification;
  let mockNotification: typeof Notification & {
    permission: NotificationPermission;
    requestPermission: () => Promise<NotificationPermission>;
  };

  beforeEach(() => {
    // Save original Notification API
    originalNotification = global.Notification;

    // Mock Notification API
    mockNotification = vi.fn() as unknown as typeof mockNotification;
    mockNotification.permission = 'default';
    mockNotification.requestPermission = vi
      .fn()
      .mockResolvedValue('granted' as NotificationPermission);

    global.Notification = mockNotification;

    // Mock BroadcastChannel
    // @ts-expect-error - simplified mock for testing
    global.BroadcastChannel = vi.fn(() => ({
      addEventListener: vi.fn(),
      postMessage: vi.fn(),
      close: vi.fn(),
    }));
  });

  afterEach(() => {
    // Restore original Notification API
    global.Notification = originalNotification;
  });

  it('should initialize with default permission state', () => {
    const { result } = renderHook(() =>
      useNotifications({
        enabled: true,
        conversationId: 'test-conv',
        onNotificationClick: vi.fn(),
      })
    );

    expect(result.current.isSupported).toBe(true);
    expect(result.current.permission).toBe('default');
    expect(result.current.isEnabled).toBe(true);
  });

  it('should request notification permission', async () => {
    const { result } = renderHook(() =>
      useNotifications({
        enabled: true,
        conversationId: 'test-conv',
        onNotificationClick: vi.fn(),
      })
    );

    const success = await result.current.requestPermission();

    expect(success).toBe(true);
    await waitFor(() => {
      expect(result.current.permission).toBe('granted');
    });
  });

  it('should show notification when enabled and permission granted', async () => {
    mockNotification.permission = 'granted';

    const { result } = renderHook(() =>
      useNotifications({
        enabled: true,
        conversationId: 'test-conv',
        onNotificationClick: vi.fn(),
      })
    );

    // Wait for leader election
    await waitFor(() => {
      expect(result.current.isLeaderTab).toBe(true);
    });

    // Show notification
    result.current.showNotification({
      conversationId: 'test-conv-2',
      messageId: 'msg-1',
      preview: 'Test message preview',
      timestamp: new Date().toISOString(),
    });

    // Verify Notification constructor was called
    await waitFor(() => {
      expect(mockNotification).toHaveBeenCalledWith(
        'New message from Family Assistant',
        expect.objectContaining({
          body: 'Test message preview',
          tag: 'test-conv-2',
        })
      );
    });
  });

  it('should not show notification when disabled', async () => {
    mockNotification.permission = 'granted';

    const { result } = renderHook(() =>
      useNotifications({
        enabled: false, // Disabled
        conversationId: 'test-conv',
        onNotificationClick: vi.fn(),
      })
    );

    // Show notification
    result.current.showNotification({
      conversationId: 'test-conv-2',
      messageId: 'msg-1',
      preview: 'Test message preview',
      timestamp: new Date().toISOString(),
    });

    // Verify Notification constructor was NOT called
    expect(mockNotification).not.toHaveBeenCalled();
  });

  it('should not show notification for current visible conversation', async () => {
    mockNotification.permission = 'granted';
    Object.defineProperty(document, 'visibilityState', {
      writable: true,
      configurable: true,
      value: 'visible',
    });

    const { result } = renderHook(() =>
      useNotifications({
        enabled: true,
        conversationId: 'test-conv',
        onNotificationClick: vi.fn(),
      })
    );

    // Wait for leader election
    await waitFor(() => {
      expect(result.current.isLeaderTab).toBe(true);
    });

    // Show notification for the SAME conversation
    result.current.showNotification({
      conversationId: 'test-conv', // Same as current
      messageId: 'msg-1',
      preview: 'Test message preview',
      timestamp: new Date().toISOString(),
    });

    // Verify Notification constructor was NOT called
    expect(mockNotification).not.toHaveBeenCalled();
  });

  it('should deduplicate notifications for the same message', async () => {
    mockNotification.permission = 'granted';

    const { result } = renderHook(() =>
      useNotifications({
        enabled: true,
        conversationId: 'test-conv',
        onNotificationClick: vi.fn(),
      })
    );

    // Wait for leader election
    await waitFor(() => {
      expect(result.current.isLeaderTab).toBe(true);
    });

    // Show the same notification twice
    const notificationData = {
      conversationId: 'test-conv-2',
      messageId: 'msg-1',
      preview: 'Test message preview',
      timestamp: new Date().toISOString(),
    };

    result.current.showNotification(notificationData);
    result.current.showNotification(notificationData);

    // Wait for async operations
    await waitFor(() => {
      // Should only be called once due to deduplication
      expect(mockNotification).toHaveBeenCalledTimes(1);
    });
  });
});
