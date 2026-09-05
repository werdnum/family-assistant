import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';

const PROFILES_API_ENDPOINT = '/api/v1/profiles';

/** A named model recipe a profile lets the user select between. */
export interface ModelTier {
  id: string;
  label: string;
  description: string | null;
}

export interface ServiceProfile {
  id: string;
  description: string;
  llm_model?: string | null;
  available_tools: string[];
  enabled_mcp_servers: string[];
  /**
   * Tiers this profile permits the user to choose, in configuration order.
   * Empty for profiles whose model is pinned.
   */
  model_tiers?: ModelTier[];
  /** The tier used when the user chooses nothing. */
  default_model_tier?: string | null;
}

export interface ProfilesResponse {
  profiles: ServiceProfile[];
  default_profile_id: string;
}

interface ProfilesContextValue {
  profiles: ServiceProfile[];
  profilesById: Record<string, ServiceProfile>;
  defaultProfileId: string | null;
  /**
   * Tier id to label across every profile, for naming a tier recorded on a past
   * message whose profile is not the one currently selected.
   */
  tierLabels: Record<string, string>;
  isLoading: boolean;
  error: string | null;
}

const EMPTY_CONTEXT: ProfilesContextValue = {
  profiles: [],
  profilesById: {},
  defaultProfileId: null,
  tierLabels: {},
  isLoading: true,
  error: null,
};

const ProfilesContext = createContext<ProfilesContextValue>(EMPTY_CONTEXT);

export const useProfiles = () => useContext(ProfilesContext);

/**
 * Fetches the profile list once and shares it with every consumer.
 *
 * One fetch on mount, with no callback or selection in its dependencies: the
 * picker, the intelligence control and the per-message badges all read the same
 * result, so a callback that changes identity every turn can no longer refetch
 * the list and blank the controls mid-turn.
 */
export const ProfilesProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [profiles, setProfiles] = useState<ServiceProfile[]>([]);
  const [defaultProfileId, setDefaultProfileId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchProfiles = async () => {
      try {
        const response = await fetch(PROFILES_API_ENDPOINT);
        if (!response.ok) {
          throw new Error(`Failed to fetch profiles: ${response.statusText}`);
        }
        const data: ProfilesResponse = await response.json();
        setProfiles(data.profiles);
        setDefaultProfileId(data.default_profile_id ?? null);
        setError(null);
      } catch (err) {
        console.error('Error fetching profiles:', err);
        setError(err instanceof Error ? err.message : 'Failed to load profiles');
      } finally {
        setIsLoading(false);
      }
    };

    void fetchProfiles();
  }, []);

  const value = useMemo<ProfilesContextValue>(() => {
    const profilesById: Record<string, ServiceProfile> = {};
    const tierLabels: Record<string, string> = {};
    for (const profile of profiles) {
      profilesById[profile.id] = profile;
      for (const tier of profile.model_tiers ?? []) {
        tierLabels[tier.id] ??= tier.label;
      }
    }
    return { profiles, profilesById, defaultProfileId, tierLabels, isLoading, error };
  }, [profiles, defaultProfileId, isLoading, error]);

  return <ProfilesContext.Provider value={value}>{children}</ProfilesContext.Provider>;
};
