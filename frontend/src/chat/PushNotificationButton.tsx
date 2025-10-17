import React, { useState, useEffect, useCallback } from 'react';
import { Bell, BellOff, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Switch } from '@/components/ui/switch';
import { Label } from '@/components/ui/label';
import {
  getClientConfig,
  subscribeToPush,
  unsubscribeFromPush,
  type PushSubscriptionJSON,
} from '@/api/pushClient';

interface PushNotificationButtonProps {
  className?: string;
}

/**
 * PushNotificationButton component for managing push notification subscriptions
 *
 * Provides UI for users to:
 * - Request browser notification permissions
 * - Subscribe/unsubscribe from push notifications
 * - View current subscription status
 *
 * Note: This is different from browser notifications (Notification API)
 * Push notifications work even when the app is completely closed
 */
export const PushNotificationButton: React.FC<PushNotificationButtonProps> = ({ className }) => {
  const [vapidPublicKey, setVapidPublicKey] = useState<string | null>(null);
  const [isSubscribed, setIsSubscribed] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [permission, setPermission] = useState<NotificationPermission>('default');

  // Check if Push API and Service Workers are supported
  const isSupported =
    typeof navigator !== 'undefined' &&
    'serviceWorker' in navigator &&
    'PushManager' in window &&
    typeof Notification !== 'undefined';

  // Fetch VAPID key and check subscription status on mount
  useEffect(() => {
    async function initialize() {
      if (!isSupported) {
        return;
      }

      try {
        // Fetch VAPID key from backend config
        const config = await getClientConfig();
        if (config.vapidPublicKey) {
          setVapidPublicKey(config.vapidPublicKey);
        }

        // Check current notification permission
        setPermission(Notification.permission);

        // Check if already subscribed to push notifications
        const registration = await navigator.serviceWorker.ready;
        const subscription = await registration.pushManager.getSubscription();
        setIsSubscribed(subscription !== null);
      } catch (err) {
        console.error('Failed to initialize push notifications:', err);
        setError('Failed to load push notification settings');
      }
    }

    initialize();
  }, [isSupported]);

  /**
   * Convert URL-safe base64 VAPID key to Uint8Array for pushManager.subscribe()
   */
  const urlBase64ToUint8Array = (base64String: string): Uint8Array => {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');

    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);

    for (let i = 0; i < rawData.length; i += 1) {
      outputArray[i] = rawData.charCodeAt(i);
    }

    return outputArray;
  };

  /**
   * Handle subscription toggle (enable/disable push notifications)
   */
  const handleToggle = useCallback(
    async (enabled: boolean) => {
      if (!isSupported || !vapidPublicKey) {
        return;
      }

      setIsLoading(true);
      setError(null);

      try {
        const registration = await navigator.serviceWorker.ready;

        if (enabled) {
          // Request notification permission if needed
          if (permission !== 'granted') {
            const result = await Notification.requestPermission();
            setPermission(result);

            if (result !== 'granted') {
              setError(
                'Notification permission denied. Please enable it in your browser settings.'
              );
              setIsLoading(false);
              return;
            }
          }

          // Subscribe to push notifications
          const subscription = await registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
          });

          // Send subscription to backend
          await subscribeToPush(subscription.toJSON() as PushSubscriptionJSON);
          setIsSubscribed(true);
        } else {
          // Unsubscribe from push notifications
          const subscription = await registration.pushManager.getSubscription();
          if (subscription) {
            await subscription.unsubscribe();
            await unsubscribeFromPush(subscription.endpoint);
            setIsSubscribed(false);
          }
        }
      } catch (err) {
        console.error('Failed to toggle push subscription:', err);
        const errorMessage = err instanceof Error ? err.message : 'Failed to update subscription';
        setError(errorMessage);
      } finally {
        setIsLoading(false);
      }
    },
    [isSupported, vapidPublicKey, permission]
  );

  // Don't render if push notifications aren't available or VAPID key is not configured
  if (!isSupported || !vapidPublicKey) {
    return null;
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="sm" className={className} title="Push notifications">
          {isLoading ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : isSubscribed ? (
            <Bell className="h-4 w-4" />
          ) : (
            <BellOff className="h-4 w-4" />
          )}
          <span className="sr-only">Push notification settings</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80">
        <DropdownMenuLabel>Push Notifications</DropdownMenuLabel>
        <DropdownMenuSeparator />

        <div className="p-2 space-y-4">
          {/* Enable/Disable Toggle */}
          <div className="flex items-center justify-between">
            <Label htmlFor="push-enabled" className="text-sm font-medium">
              Enable Push Notifications
            </Label>
            <Switch
              id="push-enabled"
              checked={isSubscribed}
              onCheckedChange={handleToggle}
              disabled={isLoading || permission === 'denied'}
            />
          </div>

          {/* Status Badge */}
          <div className="flex items-center justify-between">
            <span className="text-sm text-muted-foreground">Status</span>
            {isSubscribed ? (
              <Badge variant="default" className="bg-green-600">
                Active
              </Badge>
            ) : (
              <Badge variant="secondary">Inactive</Badge>
            )}
          </div>

          {/* Error Message */}
          {error && <div className="text-xs text-destructive">{error}</div>}

          {/* Help Text */}
          <DropdownMenuSeparator />
          <div className="text-xs text-muted-foreground space-y-1">
            <p>
              <strong>Push Notifications</strong> allow you to receive messages even when the app is
              completely closed or running in the background.
            </p>
            <p className="pt-2 text-xs">
              Note: This requires browser notification permissions. You&apos;ll get a prompt to
              allow notifications when you enable this feature.
            </p>
          </div>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
};
