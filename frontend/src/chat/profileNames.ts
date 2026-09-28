/** Short human-readable name for a processing profile id. */
export const getProfileDisplayName = (profileId: string): string => {
  switch (profileId) {
    case 'default_assistant':
      return 'Assistant';
    case 'browser':
      return 'Browser';
    case 'research':
      return 'Research';
    case 'event_handler':
      return 'Events';
    default: {
      const words = profileId.replace(/_/g, ' ');
      return words.charAt(0).toUpperCase() + words.slice(1);
    }
  }
};
