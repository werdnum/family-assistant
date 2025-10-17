/* eslint-disable no-undef */
/**
 * Custom Service Worker for Family Assistant PWA
 *
 * This service worker handles:
 * - Push event reception from backend
 * - Notification display with proper formatting
 * - Notification click handling for app focus/navigation
 *
 * Note: This file runs in a Service Worker context where 'self' and 'clients'
 * are global objects provided by the browser.
 */

// Handle push events from the backend
self.addEventListener('push', (event) => {
  try {
    const data = event.data ? event.data.json() : {};
    const title = data.title || 'Family Assistant';
    const options = {
      body: data.body || 'You have a new notification.',
      icon: '/pwa-192x192.png',
      badge: '/badge.png',
      tag: data.tag || 'general', // Group notifications by conversation
      data: data.data || {}, // Preserve custom data for click handling
      requireInteraction: false,
      vibrate: [200, 100, 200], // Mobile vibration pattern
      timestamp: data.timestamp || Date.now(),
    };

    event.waitUntil(self.registration.showNotification(title, options));
  } catch (error) {
    console.error('Error handling push event:', error);
    // Fallback notification
    event.waitUntil(
      self.registration.showNotification('Family Assistant', {
        body: 'You have a new message',
        icon: '/pwa-192x192.png',
      })
    );
  }
});

// Handle notification clicks
self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  // Extract conversation ID or URL from notification data
  const data = event.notification.data || {};
  const conversationId = data.conversationId;
  const urlToOpen = conversationId ? `/chat?conversation_id=${conversationId}` : '/chat';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      // Try to focus an existing window with matching URL
      for (const client of clientList) {
        if (client.url.includes('/chat') && 'focus' in client) {
          // If target conversation specified, update URL
          if (conversationId && !client.url.includes(`conversation_id=${conversationId}`)) {
            // The app will handle URL-based routing
            client.navigate(urlToOpen);
          }
          return client.focus();
        }
      }
      // Open new window if none found
      if (clients.openWindow) {
        return clients.openWindow(urlToOpen);
      }
    })
  );
});

// Handle notification close events (optional)
self.addEventListener('notificationclose', (_event) => {
  // Can be used for analytics or cleanup
  // Currently unused but available for future enhancements
});
