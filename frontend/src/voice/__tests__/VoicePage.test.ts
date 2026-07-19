import { afterEach, describe, expect, it, vi } from 'vitest';
import { resolveVoiceProfileId } from '../VoicePage';

describe('resolveVoiceProfileId', () => {
  afterEach(() => {
    localStorage.clear();
    vi.unstubAllGlobals();
  });

  it('uses a valid stored profile', async () => {
    localStorage.setItem('selectedProfileId', 'research');
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          profiles: [{ id: 'configured-default' }, { id: 'research' }],
          default_profile_id: 'configured-default',
        }),
      })
    );

    await expect(resolveVoiceProfileId()).resolves.toBe('research');
  });

  it('replaces a stale stored profile with the configured default', async () => {
    localStorage.setItem('selectedProfileId', 'default_assistant');
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          profiles: [{ id: 'configured-default' }],
          default_profile_id: 'configured-default',
        }),
      })
    );

    await expect(resolveVoiceProfileId()).resolves.toBe('configured-default');
  });

  it('defers to the server default when profile discovery fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));

    await expect(resolveVoiceProfileId()).resolves.toBeUndefined();
  });
});
