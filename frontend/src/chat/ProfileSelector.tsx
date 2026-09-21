import { Bot, Globe, Search, Settings } from 'lucide-react';
import React, { useEffect } from 'react';
import { Badge } from '@/components/ui/badge';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { type ServiceProfile, useProfiles } from './profilesContext';

interface ProfileSelectorProps {
  selectedProfileId: string;
  onProfileChange: (profileId: string) => void;
  disabled?: boolean;
  onLoadingChange?: (loading: boolean) => void;
}

// Map profile IDs to icons for better visual identification
const getProfileIcon = (profileId: string) => {
  switch (profileId) {
    case 'browser':
      return <Globe className="w-4 h-4" />;
    case 'research':
      return <Search className="w-4 h-4" />;
    case 'event_handler':
      return <Settings className="w-4 h-4" />;
    default:
      return <Bot className="w-4 h-4" />;
  }
};

// Extract short name from profile ID for display
const getProfileDisplayName = (profile: ServiceProfile) => {
  switch (profile.id) {
    case 'default_assistant':
      return 'Assistant';
    case 'browser':
      return 'Browser';
    case 'research':
      return 'Research';
    case 'event_handler':
      return 'Events';
    default:
      return profile.id.charAt(0).toUpperCase() + profile.id.slice(1);
  }
};

const ProfileSelector: React.FC<ProfileSelectorProps> = ({
  selectedProfileId,
  onProfileChange,
  disabled = false,
  onLoadingChange,
}) => {
  const { profiles, defaultProfileId, isLoading: loading, error } = useProfiles();

  // Notify parent of loading state changes
  useEffect(() => {
    onLoadingChange?.(loading);
  }, [loading, onLoadingChange]);

  // If no profile is selected and we have a default, use it.
  useEffect(() => {
    if (!selectedProfileId && defaultProfileId) {
      onProfileChange(defaultProfileId);
    }
  }, [selectedProfileId, defaultProfileId, onProfileChange]);

  const selectedProfile = profiles.find((p) => p.id === selectedProfileId);

  if (loading) {
    return (
      <div
        className="flex items-center gap-2 text-sm text-muted-foreground"
        data-loading-indicator="true"
      >
        <Bot className="w-4 h-4 animate-pulse" />
        <span>Loading...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center gap-2 text-sm text-destructive">
        <Bot className="w-4 h-4" />
        <span>Error loading profiles</span>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2">
      <Select value={selectedProfileId} onValueChange={onProfileChange} disabled={disabled}>
        <SelectTrigger
          className="w-auto min-w-[120px] h-8 text-sm"
          aria-label="Processing profile"
          data-testid="profile-selector"
        >
          <div className="flex items-center gap-2">
            {selectedProfile && getProfileIcon(selectedProfile.id)}
            <SelectValue>
              {selectedProfile ? getProfileDisplayName(selectedProfile) : 'Select Profile'}
            </SelectValue>
          </div>
        </SelectTrigger>
        <SelectContent>
          {profiles.map((profile) => (
            <SelectItem key={profile.id} value={profile.id}>
              <div className="flex flex-col gap-1 py-1">
                <div className="flex items-center gap-2">
                  {getProfileIcon(profile.id)}
                  <span className="font-medium">{getProfileDisplayName(profile)}</span>
                </div>
                <div className="text-xs text-muted-foreground max-w-[250px]">
                  {profile.description}
                </div>
                {profile.llm_model && (
                  <div className="flex items-center gap-1 mt-1">
                    <Badge variant="secondary" className="text-xs">
                      {profile.llm_model}
                    </Badge>
                  </div>
                )}
                {(profile.available_tools.length > 0 || profile.enabled_mcp_servers.length > 0) && (
                  <div className="flex flex-wrap gap-1 mt-1 max-w-[250px]">
                    {profile.available_tools.slice(0, 3).map((tool) => (
                      <Badge key={tool} variant="outline" className="text-xs">
                        {tool.replace(/_/g, ' ')}
                      </Badge>
                    ))}
                    {profile.enabled_mcp_servers.slice(0, 2).map((server) => (
                      <Badge key={server} variant="outline" className="text-xs">
                        {server}
                      </Badge>
                    ))}
                    {profile.available_tools.length + profile.enabled_mcp_servers.length > 5 && (
                      <Badge variant="outline" className="text-xs">
                        +{profile.available_tools.length + profile.enabled_mcp_servers.length - 5}{' '}
                        more
                      </Badge>
                    )}
                  </div>
                )}
              </div>
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
};

export default ProfileSelector;
