import { describe, it, expect } from 'vitest';
import { server } from '../../test/setup.js';
import { http, HttpResponse } from 'msw';
import { getClientConfig, subscribeToPush, unsubscribeFromPush } from '../pushClient';

describe('pushClient', () => {
  describe('getClientConfig', () => {
    it('should fetch client config successfully', async () => {
      const config = await getClientConfig();

      // The MSW handler returns a valid VAPID key
      expect(config.vapidPublicKey).toBeDefined();
      expect(typeof config.vapidPublicKey).toBe('string');
    });

    it('should return null VAPID key when not configured', async () => {
      server.use(
        http.get('/api/client_config', () => {
          return HttpResponse.json({
            vapidPublicKey: null,
          });
        })
      );

      const config = await getClientConfig();

      expect(config.vapidPublicKey).toBeNull();
    });

    it('should throw error on fetch failure', async () => {
      server.use(
        http.get('/api/client_config', () => {
          return HttpResponse.json({ error: 'Not Found' }, { status: 404 });
        })
      );

      await expect(getClientConfig()).rejects.toThrow('Failed to fetch client config: 404');
    });

    it('should throw error on network failure', async () => {
      server.use(
        http.get('/api/client_config', () => {
          return HttpResponse.error();
        })
      );

      await expect(getClientConfig()).rejects.toThrow();
    });
  });

  describe('subscribeToPush', () => {
    it('should subscribe to push successfully', async () => {
      const mockSubscription = {
        endpoint: 'https://push.example.com/subscription/abc123',
        keys: {
          p256dh: 'test-p256dh',
          auth: 'test-auth',
        },
      };

      const result = await subscribeToPush(mockSubscription);

      // MSW handler returns a success response
      expect(result.status).toBe('success');
      expect(result.id).toBeDefined();
    });

    it('should throw error on subscription failure', async () => {
      const mockSubscription = {
        endpoint: 'https://push.example.com/subscription/abc123',
        keys: {
          p256dh: 'test-p256dh',
          auth: 'test-auth',
        },
      };

      server.use(
        http.post('/api/push/subscribe', () => {
          return HttpResponse.json(
            { status: 'error', message: 'Subscription error' },
            { status: 500 }
          );
        })
      );

      await expect(subscribeToPush(mockSubscription)).rejects.toThrow(
        'Failed to subscribe to push: 500'
      );
    });

    it('should throw error with server message on bad subscription', async () => {
      const mockSubscription = {
        endpoint: 'invalid',
        keys: {
          p256dh: '',
          auth: '',
        },
      };

      server.use(
        http.post('/api/push/subscribe', () => {
          return HttpResponse.json(
            { status: 'error', message: 'Invalid subscription format' },
            { status: 400 }
          );
        })
      );

      await expect(subscribeToPush(mockSubscription)).rejects.toThrow(
        'Failed to subscribe to push: 400'
      );
    });

    it('should throw error on network failure', async () => {
      const mockSubscription = {
        endpoint: 'https://push.example.com/test',
        keys: { p256dh: 'key', auth: 'auth' },
      };

      server.use(
        http.post('/api/push/subscribe', () => {
          return HttpResponse.error();
        })
      );

      await expect(subscribeToPush(mockSubscription)).rejects.toThrow();
    });
  });

  describe('unsubscribeFromPush', () => {
    it('should unsubscribe from push successfully', async () => {
      const endpoint = 'https://push.example.com/subscription/abc123';

      const result = await unsubscribeFromPush(endpoint);

      // MSW handler returns a success response
      expect(result.status).toBe('success');
    });

    it('should handle already unsubscribed case', async () => {
      const endpoint = 'https://push.example.com/subscription/unknown';

      server.use(
        http.post('/api/push/unsubscribe', () => {
          return HttpResponse.json({
            status: 'not_found',
          });
        })
      );

      const result = await unsubscribeFromPush(endpoint);

      expect(result.status).toBe('not_found');
    });

    it('should throw error on unsubscribe failure', async () => {
      const endpoint = 'https://push.example.com/subscription/abc123';

      server.use(
        http.post('/api/push/unsubscribe', () => {
          return HttpResponse.json({ status: 'error', message: 'Server error' }, { status: 500 });
        })
      );

      await expect(unsubscribeFromPush(endpoint)).rejects.toThrow(
        'Failed to unsubscribe from push: 500'
      );
    });

    it('should throw error on network failure', async () => {
      const endpoint = 'https://push.example.com/subscription/abc123';

      server.use(
        http.post('/api/push/unsubscribe', () => {
          return HttpResponse.error();
        })
      );

      await expect(unsubscribeFromPush(endpoint)).rejects.toThrow();
    });
  });
});
