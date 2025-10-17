/**
 * Client for push notification API endpoints
 *
 * This module provides TypeScript interfaces and functions for interacting with
 * the backend push notification API.
 */

/**
 * Client configuration response from backend
 */
export interface ClientConfig {
  vapidPublicKey: string | null;
}

/**
 * Push subscription data from browser (as JSON)
 */
export interface PushSubscriptionJSON {
  endpoint: string;
  expirationTime?: number | null;
  keys: {
    p256dh: string;
    auth: string;
  };
}

/**
 * Response from subscribe endpoint
 */
export interface SubscribeResponse {
  status: string;
  id?: string;
  message?: string;
}

/**
 * Response from unsubscribe endpoint
 */
export interface UnsubscribeResponse {
  status: string;
  message?: string;
}

/**
 * Fetch client configuration including VAPID public key
 *
 * @returns ClientConfig with vapidPublicKey or null if not configured
 * @throws Error if the request fails
 */
export async function getClientConfig(): Promise<ClientConfig> {
  const response = await fetch('/api/client_config');
  if (!response.ok) {
    throw new Error(`Failed to fetch client config: ${response.status} ${response.statusText}`);
  }
  return response.json();
}

/**
 * Subscribe to push notifications
 *
 * Sends the browser's push subscription to the backend for storage.
 * The backend will use this subscription to send push notifications to the user.
 *
 * @param subscription - The push subscription from browser's pushManager
 * @returns SubscribeResponse with status and subscription ID
 * @throws Error if the request fails
 */
export async function subscribeToPush(
  subscription: PushSubscriptionJSON
): Promise<SubscribeResponse> {
  const response = await fetch('/api/push/subscribe', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ subscription }),
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`Failed to subscribe to push: ${response.status} - ${errorText}`);
  }

  return response.json();
}

/**
 * Unsubscribe from push notifications
 *
 * Removes the subscription endpoint from the backend.
 * After this call, the user will no longer receive push notifications.
 *
 * @param endpoint - The subscription endpoint URL to unsubscribe
 * @returns UnsubscribeResponse with status
 * @throws Error if the request fails
 */
export async function unsubscribeFromPush(endpoint: string): Promise<UnsubscribeResponse> {
  const response = await fetch('/api/push/unsubscribe', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ endpoint }),
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`Failed to unsubscribe from push: ${response.status} - ${errorText}`);
  }

  return response.json();
}
