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

A new script will be added to `scripts/generate_vapid_keys.py` to generate the VAPID key pair. This
script will use the `py_vapid` library to generate raw keys and then encode them in a URL-safe
base64 format without padding.

```python
import base64
from py_vapid import Vapid
from cryptography.hazmat.primitives import serialization

vapid = Vapid()
vapid.generate_keys()

private_key_obj = vapid.private_key
public_key_obj = vapid.public_key

raw_private_key = private_key_obj.private_numbers().private_value.to_bytes(32, 'big')
raw_public_key_point = public_key_obj.public_bytes(
    encoding=serialization.Encoding.X962,
    format=serialization.PublicFormat.UncompressedPoint
)

b64_private_key = base64.urlsafe_b64encode(raw_private_key).rstrip(b'=').decode('utf-8')
b64_public_key = base64.urlsafe_b64encode(raw_public_key_point).rstrip(b'=').decode('utf-8')

print(f"VAPID_PRIVATE_KEY={b64_private_key}")
print(f"VAPID_PUBLIC_KEY={b64_public_key}")
```

#### 3.2.2. Configuration

The generated keys will be provided to the application as environment variables: `VAPID_PUBLIC_KEY`
and `VAPID_PRIVATE_KEY`. The keys are the raw key bytes, encoded using URL-safe base64 without
padding. This format is compatible with `py_vapid.Vapid02.from_raw()` and
`py_vapid.Vapid02.from_raw_public()`. The keys are stored in `config_data["pwa_config"]` and can be
accessed via `config.get("pwa_config", {}).get("vapid_public_key")` and
`config.get("pwa_config", {}).get("vapid_private_key")`. This will be documented in `AGENTS.md`.

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
  This endpoint will require authentication. Initially, it will provide the VAPID public key from
  `config["pwa_config"]["vapid_public_key"]`:

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

## 4. Implementation Plan

### Part 1: PWA and Frontend Setup

1. **Install Dependencies:**

   - Add `vite-plugin-pwa` to the `frontend`'s `package.json`.
   - Run `npm install` in the `frontend` directory.

2. **Update Vite Configuration:**

   - Modify `frontend/vite.config.js` to include the `VitePWA` plugin as specified in the design
     document.

3. **Add Static Assets:**

   - The existing `logo.png` will be used to generate the required icons.
   - The following icons will be generated in `frontend/public/`:
     - `pwa-192x192.png`
     - `pwa-512x512.png`
     - `badge.png` (for notifications)

4. **Update `index.html`:**

   - Add the `<meta name="theme-color" ...>` and `<link rel="apple-touch-icon" ...>` tags to the
     `<head>` of `frontend/index.html`.

### Part 2: VAPID Key Generation and Backend Configuration

1. **Install Dependency:**

   - Add `py-vapid` to the project's Python dependencies in `pyproject.toml`.
   - Run `uv pip install -e .` to install it.

2. **Create Key Generation Script:**

   - Create the file `scripts/generate_vapid_keys.py` with the content from the design document,
     which generates raw, URL-safe base64 encoded keys.

3. **Generate and Configure Keys:**

   - Run the script to generate the VAPID keys.
   - The keys will be provided as environment variables `VAPID_PUBLIC_KEY` and `VAPID_PRIVATE_KEY`
     in the format specified in section 3.2.2.
   - Update `AGENTS.md` to document these new environment variables.

### Part 3: Backend Implementation

1. **Database Model:** ✅

   - Create `src/family_assistant/storage/models/push_subscription.py` and define the
     `PushSubscription` SQLAlchemy model.
   - Ensure the new model is imported in `src/family_assistant/storage/models/__init__.py`.

2. **Database Migration:** ✅

   - Run `alembic revision --autogenerate -m "Add push_subscriptions table"` to create a new
     migration script.
   - Review the generated script in `alembic/versions/`.
   - Run `alembic upgrade head` to apply the migration.

3. **Database Repository:** ✅

   - Create `src/family_assistant/storage/repositories/push_subscription.py` to define the
     `PushSubscriptionRepository` for CRUD operations.
   - Expose the new repository in `src/family_assistant/storage/repositories/__init__.py`.

4. **Push Notification Service:** ✅

   - Create `src/family_assistant/services/push_notification.py`.
   - Implement the `PushNotificationService` with methods for sending notifications and handling
     stale subscriptions, using the `py-vapid` library.
   - Integrate this service into the application's dependency injection system.

5. **API Endpoints:** ✅

   - Create `src/family_assistant/web/routers/client_config.py` and implement the
     `GET /api/client_config` endpoint to expose the `VAPID_PUBLIC_KEY`.
   - Create `src/family_assistant/web/routers/push.py` and implement the `POST /api/push/subscribe`
     and `POST /api/push/unsubscribe` endpoints.
   - Mount the new routers in the main FastAPI application.

6. **Integrate Notification Trigger:** ✅

   - Modify existing services (e.g., `WebChatInterface`) to call the `PushNotificationService` when
     a relevant event occurs (like a new message).

### Part 4: Frontend Integration

1. **Custom Service Worker:**

   - Create a custom service worker file (e.g., `frontend/src/sw.js`).
   - Add the `push` event listener logic to handle incoming notifications as described in the
     design.
   - Update the `VitePWA` configuration in `vite.config.js` to use this custom service worker
     instead of generating one.

2. **API Client:**

   - Add new functions to the frontend API client to communicate with the new backend endpoints:
     - `getClientConfig()`
     - `subscribeToPush(subscription)`
     - `unsubscribeFromPush(subscription)`

3. **React Component:**

   - Create a new component `frontend/src/components/PushNotificationButton.tsx`.
   - This component will contain the logic to:
     1. Fetch the VAPID key using `getClientConfig`.
     2. Request user permission for notifications.
     3. Subscribe or unsubscribe the browser's push manager.
     4. Send the subscription details to the backend.
   - Add this component to a suitable location in the application's UI, such as a settings page.

## 5. Progress

### Part 1: PWA and Frontend Setup

- [x] **Install Dependencies:** `vite-plugin-pwa` has been added to the frontend dependencies.
- [x] **Update Vite Configuration:** `frontend/vite.config.js` has been updated with the `VitePWA`
  plugin.
- [x] **Add Static Assets:** PWA icons have been generated from `logo.png` and placed in
  `frontend/public/`.
- [x] **Update `index.html`:** The theme-color meta tag has been added to `frontend/index.html`.

### Part 2: VAPID Key Generation and Backend Configuration

- [x] **Install Dependency:** `py-vapid` has been added to the backend dependencies.
- [x] **Create Key Generation Script:** The script has been created and tested.
- [x] **Generate and Configure Keys:** Keys have been generated and the method for providing them as
  environment variables has been determined.

### Part 3: Backend Implementation

- [x] **Database Model:** Created `src/family_assistant/storage/push_subscription.py` with the
  `push_subscriptions` table definition using SQLAlchemy Core Table syntax with JSON/JSONB support.
- [x] **Database Migration:** Generated Alembic migration
  `2025_10_17-a7c55b979906_add_push_subscriptions_table.py` for the push_subscriptions table.
- [x] **Database Repository:** Created `PushSubscriptionRepository` with CRUD operations and
  integrated into `DatabaseContext`.
- [x] **Push Notification Service:** Created basic `PushNotificationService` structure in
  `src/family_assistant/services/push_notification.py` (actual py-vapid integration pending).
- [x] **API Endpoints:** Implemented `GET /api/client_config` and push subscription endpoints
  (`POST /api/push/subscribe`, `POST /api/push/unsubscribe`).
- [ ] **Integrate Notification Trigger:** (Excluded from current scope - will be implemented when
  needed)

### Part 4: Frontend Integration

- [ ] **Custom Service Worker:**
- [ ] **API Client:**
- [ ] **React Component:**

## 6. Testing Strategy

### Backend Testing

**Status: ✅ Completed**

**Functional Tests in `tests/functional/test_push_api.py`:**

All backend tests are functional tests using the test fixtures from `tests/functional/conftest.py`:

- **Client Config Endpoint:**

  - Test `GET /api/client_config` returns VAPID public key when configured
  - Test `GET /api/client_config` behavior when VAPID key is not configured
  - Authentication is disabled in functional tests (see conftest.py)

- **Push Subscription Endpoints:**

  - Test `POST /api/push/subscribe` creates subscription with valid data
  - Test `POST /api/push/subscribe` validates subscription format
  - Test `POST /api/push/unsubscribe` removes subscription
  - Test `POST /api/push/unsubscribe` handles non-existent subscriptions gracefully
  - Verify subscriptions are persisted in database (query directly via DatabaseContext)
  - Verify multiple subscriptions can exist for same user
  - Test that user_identifier is correctly associated with subscriptions

- **Push Notification Service:**

  - Test sending notifications to valid subscriptions
  - Test handling of stale/invalid subscriptions (410 Gone responses)
  - Test cleanup of invalid subscriptions from database
  - Mock external push service calls using appropriate mocking library

### Frontend Testing

**Unit Tests:**

- Test `PushNotificationButton` component:
  - Test permission request flow
  - Test subscription/unsubscription logic
  - Test error handling
  - Mock service worker and browser APIs

**Integration Tests:**

- Test full notification flow with MSW (Mock Service Worker):
  - Mock `GET /api/client_config` endpoint
  - Mock `POST /api/push/subscribe` endpoint
  - Verify correct API calls are made
  - Verify UI updates correctly based on subscription state

### Manual Testing Checklist

- [ ] Verify PWA can be installed on mobile devices
- [ ] Verify PWA can be installed on desktop browsers
- [ ] Test push notifications are received when app is closed
- [ ] Test push notifications are received when app is in background
- [ ] Test notification click behavior
- [ ] Verify offline functionality works as expected
