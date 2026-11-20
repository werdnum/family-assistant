/** Short human-readable name for a processing profile id. */
export const getProfileDisplayName = (profileId: string): string => {
  switch (profileId) {
    case 'default_assistant':
      return 'Assistant';
    case 'browser':
    case 'browser_profile':
      return 'Browser';
    case 'browser_visual_profile':
      return 'Visual Browser';
    case 'credential_browser_profile':
      return 'Credential Browser';
    case 'credential_browser_visual_profile':
      return 'Credential Visual Browser';
    case 'authenticated_browser_profile':
      return 'Authenticated Browser';
    case 'authenticated_browser_visual_profile':
      return 'Authenticated Visual Browser';
    case 'research':
      return 'Research';
    case 'event_handler':
      return 'Events';
    default:
      return profileId.charAt(0).toUpperCase() + profileId.slice(1);
  }
};
