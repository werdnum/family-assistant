import { HttpResponse, http } from 'msw';
import { afterEach, describe, expect, it } from 'vitest';
import { server } from '../../test/setup.js';
import { resolveVoiceProfileId } from '../VoicePage';

describe('resolveVoiceProfileId', () => {
  afterEach(() => {
    localStorage.clear();
  });

  it('uses a valid stored profile', async () => {
    localStorage.setItem('selectedProfileId', 'research');
    server.use(
      http.get('/api/v1/profiles', () =>
        HttpResponse.json({
          profiles: [{ id: 'configured-default' }, { id: 'research' }],
          default_profile_id: 'configured-default',
        })
      )
    );

    await expect(resolveVoiceProfileId()).resolves.toBe('research');
  });

  it('replaces a stale stored profile with the configured default', async () => {
    localStorage.setItem('selectedProfileId', 'default_assistant');
    server.use(
      http.get('/api/v1/profiles', () =>
        HttpResponse.json({
          profiles: [{ id: 'configured-default' }],
          default_profile_id: 'configured-default',
        })
      )
    );

    await expect(resolveVoiceProfileId()).resolves.toBe('configured-default');
  });

  it.each([
    ['a network error', () => HttpResponse.error()],
    ['a server error', () => HttpResponse.json({ detail: 'boom' }, { status: 500 })],
  ])('defers to the server default when profile discovery fails with %s', async (_, respond) => {
    localStorage.setItem('selectedProfileId', 'research');
    server.use(http.get('/api/v1/profiles', respond));

    await expect(resolveVoiceProfileId()).resolves.toBeUndefined();
  });
});
