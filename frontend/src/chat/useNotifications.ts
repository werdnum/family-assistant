import { useEffect, useRef, useCallback, useState } from 'react';

interface NotificationOptions {
  enabled: boolean;
  conversationId: string | null;
  onNotificationClick?: (conversationId: string) => void;
}

interface NotificationData {
  conversationId: string;
  messageId: string;
  preview: string;
  timestamp: string;
}

// Multi-tab coordination using BroadcastChannel
class NotificationCoordinator {
  private channel: BroadcastChannel | null = null;
  private isLeader = false;
  private leaderId: string | null = null;
  private myId: string;
  private heartbeatInterval: ReturnType<typeof setInterval> | null = null;
  private leaderCheckInterval: ReturnType<typeof setInterval> | null = null;
  private onLeaderChange: (isLeader: boolean) => void;

  constructor(onLeaderChange: (isLeader: boolean) => void) {
    this.myId = `tab_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
    this.onLeaderChange = onLeaderChange;

    // Check if BroadcastChannel is supported
    if (typeof BroadcastChannel !== 'undefined') {
      this.channel = new BroadcastChannel('family-assistant-notifications');
      this.setupChannel();
      this.electLeader();
    } else {
      // Fallback: single tab, always leader
      this.isLeader = true;
      this.onLeaderChange(true);
    }
  }

  private setupChannel() {
    if (!this.channel) {
      return;
    }

    this.channel.addEventListener('message', (event) => {
      const { type, tabId, timestamp } = event.data;

      if (type === 'heartbeat' && tabId !== this.myId) {
        // Another tab is alive
        if (tabId === this.leaderId) {
          // Current leader is alive
          this.lastLeaderHeartbeat = timestamp;
        }
      } else if (type === 'leader-claim' && tabId !== this.myId) {
        // Another tab claimed leadership
        if (!this.leaderId || timestamp > (this.leaderTimestamp || 0)) {
          this.leaderId = tabId;
          this.leaderTimestamp = timestamp;
          this.isLeader = false;
          this.onLeaderChange(false);
        }
      } else if (type === 'leader-check') {
        // Another tab is checking for leader, respond if we're leader
        if (this.isLeader) {
          this.sendLeaderClaim();
        }
      }
    });
  }

  private lastLeaderHeartbeat = Date.now();
  private leaderTimestamp: number | null = null;

  private electLeader() {
    // Request current leader status
    this.channel?.postMessage({ type: 'leader-check', tabId: this.myId });

    // Wait a bit for responses, then decide
    setTimeout(() => {
      if (!this.leaderId) {
        // No leader found, claim leadership
        this.claimLeadership();
      }
    }, 200);

    // Start checking if leader is still alive
    this.leaderCheckInterval = setInterval(() => {
      const now = Date.now();
      if (!this.isLeader && this.leaderId && now - this.lastLeaderHeartbeat > 10000) {
        // Leader hasn't sent heartbeat in 10 seconds, assume dead
        this.claimLeadership();
      }
    }, 5000);
  }

  private claimLeadership() {
    this.isLeader = true;
    this.leaderId = this.myId;
    this.leaderTimestamp = Date.now();
    this.sendLeaderClaim();
    this.onLeaderChange(true);
    this.startHeartbeat();
  }

  private sendLeaderClaim() {
    this.channel?.postMessage({
      type: 'leader-claim',
      tabId: this.myId,
      timestamp: this.leaderTimestamp,
    });
  }

  private startHeartbeat() {
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval);
    }

    this.heartbeatInterval = setInterval(() => {
      if (this.isLeader) {
        this.channel?.postMessage({
          type: 'heartbeat',
          tabId: this.myId,
          timestamp: Date.now(),
        });
      }
    }, 5000);
  }

  public getIsLeader(): boolean {
    return this.isLeader;
  }

  public destroy() {
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval);
    }
    if (this.leaderCheckInterval) {
      clearInterval(this.leaderCheckInterval);
    }
    this.channel?.close();
  }
}

export function useNotifications({
  enabled,
  conversationId,
  onNotificationClick,
}: NotificationOptions) {
  const [permission, setPermission] = useState<NotificationPermission>('default');
  const [isLeaderTab, setIsLeaderTab] = useState(false);
  const coordinatorRef = useRef<NotificationCoordinator | null>(null);
  const shownNotificationsRef = useRef<Set<string>>(new Set());

  // Check if Notification API is supported
  const isSupported = typeof Notification !== 'undefined';

  // Initialize coordinator
  useEffect(() => {
    if (!isSupported || !enabled) {
      return;
    }

    const coordinator = new NotificationCoordinator((isLeader) => {
      setIsLeaderTab(isLeader);
    });
    coordinatorRef.current = coordinator;

    return () => {
      coordinator.destroy();
    };
  }, [isSupported, enabled]);

  // Update permission state
  useEffect(() => {
    if (isSupported) {
      setPermission(Notification.permission);
    }
  }, [isSupported]);

  // Request notification permission
  const requestPermission = useCallback(async () => {
    if (!isSupported) {
      return false;
    }

    try {
      const result = await Notification.requestPermission();
      setPermission(result);
      return result === 'granted';
    } catch (error) {
      console.error('Error requesting notification permission:', error);
      return false;
    }
  }, [isSupported]);

  // Show a notification
  const showNotification = useCallback(
    ({ conversationId: notifConvId, messageId, preview, timestamp }: NotificationData) => {
      // Check all conditions before showing notification
      if (!isSupported) {
        return;
      }
      if (!enabled) {
        return;
      }
      if (permission !== 'granted') {
        return;
      }
      if (!isLeaderTab) {
        return;
      } // Only leader tab shows notifications

      // Dedupe: check if we've already shown this notification
      const notificationKey = `${notifConvId}:${messageId}`;
      if (shownNotificationsRef.current.has(notificationKey)) {
        return;
      }

      // Additional check: don't notify for current conversation if page is visible
      const isPageVisible = document.visibilityState === 'visible';
      const isSameConversation = notifConvId === conversationId;

      if (isPageVisible && isSameConversation) {
        // User is actively viewing this conversation, don't notify
        return;
      }

      // Mark as shown
      shownNotificationsRef.current.add(notificationKey);

      // Clean up old entries (keep last 100)
      if (shownNotificationsRef.current.size > 100) {
        const entries = Array.from(shownNotificationsRef.current);
        entries.slice(0, entries.length - 100).forEach((key) => {
          shownNotificationsRef.current.delete(key);
        });
      }

      // Show the notification
      try {
        const notification = new Notification('New message from Family Assistant', {
          body: preview,
          icon: '/favicon.ico',
          tag: notifConvId, // Group by conversation
          requireInteraction: false,
          timestamp: new Date(timestamp).getTime(),
        });

        // Handle click to focus conversation
        notification.onclick = () => {
          window.focus();
          if (onNotificationClick && notifConvId) {
            onNotificationClick(notifConvId);
          }
          notification.close();
        };
      } catch (error) {
        console.error('Error showing notification:', error);
      }
    },
    [isSupported, enabled, permission, isLeaderTab, conversationId, onNotificationClick]
  );

  return {
    isSupported,
    permission,
    isEnabled: enabled,
    isLeaderTab,
    requestPermission,
    showNotification,
  };
}
