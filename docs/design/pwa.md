# PWA and Push Notifications Design

This document outlines the design for transforming the Family Assistant web application into a
Progressive Web App (PWA) with support for offline functionality and push notifications.

## 1. Goals

- **Installable:** The application should be installable on users' home screens on both mobile and
  desktop devices.
- **Offline Functionality:** The application should provide a basic offline experience, allowing
  users to view previously accessed content.
- **Push Notifications:** The backend should be able to send push notifications to users, even when
  the application is not active in the browser.

## 2. Architecture

The implementation will be divided into four main parts:

1. **PWA Implementation (Frontend):** Using `vite-plugin-pwa` to handle service worker and manifest
   generation.
2. **VAPID Key Management:** Generating and managing VAPID keys for push notifications.
3. **Backend Implementation (Python/FastAPI):** Adding API endpoints and services to handle push
   subscriptions and send notifications.
4. **Frontend Push Notification Integration (React):** Integrating the frontend with the backend to
   subscribe to and receive push notifications.

## 3. Detailed Design

### 3.1. PWA Implementation (Frontend)

The frontend will be converted to a PWA using the `vite-plugin-pwa` library, which is the standard
for Vite applications.

#### 3.1.1. `vite.config.js`

The `vite.config.js` file will be updated to include the `VitePWA` plugin:

```javascript
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['favicon.ico', 'apple-touch-icon.png'],
      manifest: {
        name: 'Family Assistant',
        short_name: 'FamAssist',
        description: 'Family Assistant PWA',
        theme_color: '#ffffff',
        icons: [
          {
            src: 'pwa-192x192.png',
            sizes: '192x192',
            type: 'image/png'
          },
          {
            src: 'pwa-512x512.png',
            sizes: '512x512',
            type: 'image/png'
          }
        ]
      }
    })
  ]
})
```

#### 3.1.2. Static Assets

The required icons (`pwa-192x192.png` and `pwa-512x512.png`) will be placed in the `frontend/public`
directory. Placeholder images will be used until the official logo is available.

#### 3.1.3. `index.html`

The `frontend/index.html` file will be updated to include the following in the `<head>` section:

```html
<meta name="theme-color" content="#ffffff" />
<link rel="apple-touch-icon" href="/apple-touch-icon.png" />
```

### 3.2. VAPID Key Management

VAPID keys are required to securely send push notifications.

#### 3.2.1. Key Generation

A new script will be added to `scripts/generate_vapid_keys.py` to generate the VAPID key pair using
the `py-vapid` library.

```python
from py_vapid import Vapid

vapid = Vapid.generate()
print(f"VAPID_PUBLIC_KEY={vapid.public_key}")
print(f"VAPID_PRIVATE_KEY={vapid.private_key}")
```

#### 3.2.2. Configuration

The generated keys will be provided to the application as environment variables: `VAPID_PUBLIC_KEY`
and `VAPID_PRIVATE_KEY`. This will be documented in `AGENTS.md`.

### 3.3. Backend Implementation (Python/FastAPI)

The backend will be responsible for storing push subscriptions and sending notifications.

#### 3.3.1. Database Model

A new SQLAlchemy model will be created to store push subscriptions. A new file
`src/family_assistant/storage/models/push_subscription.py` will be created:

```python
from sqlalchemy import Column, Integer, String, Text
from family_assistant.storage.models.base import Base

class PushSubscription(Base):
    __tablename__ = 'push_subscriptions'

    id = Column(Integer, primary_key=True)
    subscription_json = Column(Text, nullable=False)
    user_identifier = Column(String, nullable=False, index=True)

# Note: A traditional foreign key is not used for `user_identifier` because the application
# manages users through a session-based and token-based authentication system that does not
# rely on a central `users` table. The `user_identifier` string links the subscription to
# the user's identity from the authentication system.
```

A new Alembic migration will be created to apply this change to the database.

#### 3.3.2. Repository

A new repository will be added to `src/family_assistant/storage/repositories/push_subscription.py`
to handle database operations for the `PushSubscription` model.

#### 3.3.3. API Endpoints

A new router will be created to handle client-side configuration.

- `GET /api/client_config`: Returns a JSON object with public configuration needed by the client.
  This endpoint will require authentication. Initially, it will provide the VAPID public key:

  ```json
  {
    "vapidPublicKey": "..."
  }
  ```

A new router will be created at `src/family_assistant/web/routers/push.py`:

- `POST /api/push/subscribe`: Receives a push subscription object from the frontend and stores it in
  the database.

- `POST /api/push/unsubscribe`: Removes a push subscription from the database.

Note: Both of these endpoints will require authentication to ensure that subscriptions are
associated with the correct user.

#### 3.3.4. Push Notification Service

A new service, `PushNotificationService`, will be created. It will be initialized in the `lifespan`
function of the web app and made available through dependency injection. This service will have a
method `send_notification` that takes a user ID and a payload, retrieves the user's push
subscriptions, and sends the notification using `py-vapid`.

#### 3.3.5. Handling Stale Subscriptions

The `PushNotificationService` will include logic to handle stale or invalid subscriptions. When
sending a notification, the service will inspect the response from the push service. If the response
indicates that the subscription is no longer valid (e.g., a 410 Gone status code), the service will
delete the corresponding `PushSubscription` from the database.

This service will be called from other parts of the application, such as the `WebChatInterface`, to
send notifications when new messages are received.

### 3.4. Frontend Push Notification Integration (React)

The frontend will be updated to allow users to subscribe to push notifications.

#### 3.4.1. Conditional Feature

The push notification UI will only be displayed if the `VAPID_PUBLIC_KEY` is successfully fetched
from the backend.

#### 3.4.2. `PushNotificationButton.tsx`

A new React component will be created to handle the subscription process. This component will:

1. Fetch the client configuration from `GET /api/client_config`.
2. Extract the `vapidPublicKey` from the response.
3. Request permission from the user to show notifications using `Notification.requestPermission()`.
4. If permission is granted, get the `PushSubscription` from the service worker's `pushManager`.
5. Send the subscription object to the backend via `POST /api/push/subscribe`.

#### 3.4.3. Service Worker

The `vite-plugin-pwa` generated service worker will be customized to handle `push` events. A custom
service worker file will be used, and it will include the following logic:

```javascript
self.addEventListener('push', (event) => {
  const data = event.data.json();
  const title = data.title || 'Family Assistant';
  const options = {
    body: data.body || 'You have a new notification.',
    icon: 'pwa-192x192.png',
    badge: 'badge.png' // Placeholder
  };
  event.waitUntil(self.registration.showNotification(title, options));
});
```

This design provides a comprehensive plan for implementing PWA and push notification functionality
in a way that is consistent with the existing architecture and best practices.
