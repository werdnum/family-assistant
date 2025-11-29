/**
 * Visual status indicator for voice mode.
 *
 * Displays a pulsing orb that changes color and animation based on
 * the current voice session state.
 */

import type { VoiceActivityState, VoiceConnectionState } from './types';

interface StatusIndicatorProps {
  connectionState: VoiceConnectionState;
  activityState: VoiceActivityState;
  /** Detailed status message during connection (e.g., "Fetching token...", "Connecting to Gemini...") */
  connectingStatus?: string;
}

/**
 * Get the status label for display.
 */
function getStatusLabel(
  connectionState: VoiceConnectionState,
  activityState: VoiceActivityState,
  connectingStatus?: string
): string {
  switch (connectionState) {
    case 'disconnected':
      return 'Ready to connect';
    case 'connecting':
      return connectingStatus || 'Connecting...';
    case 'error':
      return 'Connection error';
    case 'connected':
      switch (activityState) {
        case 'listening':
          return 'Listening...';
        case 'processing':
          return 'Processing...';
        case 'speaking':
          return 'Speaking...';
        default:
          return 'Connected';
      }
    default:
      return 'Unknown state';
  }
}

/**
 * Get the CSS classes for the orb based on state.
 */
function getOrbClasses(
  connectionState: VoiceConnectionState,
  activityState: VoiceActivityState
): string {
  const baseClasses = 'w-24 h-24 rounded-full transition-all duration-300';

  switch (connectionState) {
    case 'disconnected':
      return `${baseClasses} bg-gray-400`;
    case 'connecting':
      return `${baseClasses} bg-yellow-400 animate-pulse`;
    case 'error':
      return `${baseClasses} bg-red-500`;
    case 'connected':
      switch (activityState) {
        case 'listening':
          return `${baseClasses} bg-green-500 animate-pulse`;
        case 'processing':
          return `${baseClasses} bg-blue-500 animate-bounce`;
        case 'speaking':
          return `${baseClasses} bg-purple-500 animate-ping-slow`;
        default:
          return `${baseClasses} bg-green-400`;
      }
    default:
      return baseClasses;
  }
}

/**
 * Status indicator component showing a visual orb with state label.
 */
export function StatusIndicator({
  connectionState,
  activityState,
  connectingStatus,
}: StatusIndicatorProps) {
  const statusLabel = getStatusLabel(connectionState, activityState, connectingStatus);
  const orbClasses = getOrbClasses(connectionState, activityState);

  return (
    <div className="flex flex-col items-center gap-4">
      <div className="relative">
        {/* Outer glow effect for active states */}
        {connectionState === 'connected' && (
          <div
            className={`absolute inset-0 w-24 h-24 rounded-full opacity-30 blur-md ${
              activityState === 'listening'
                ? 'bg-green-400'
                : activityState === 'speaking'
                  ? 'bg-purple-400'
                  : activityState === 'processing'
                    ? 'bg-blue-400'
                    : 'bg-green-300'
            }`}
          />
        )}

        {/* Main orb */}
        <div className={`${orbClasses} relative`}>
          {/* Inner highlight */}
          <div className="absolute top-2 left-2 w-6 h-6 rounded-full bg-white opacity-30" />
        </div>

        {/* Sound wave animation for listening state */}
        {connectionState === 'connected' && activityState === 'listening' && (
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="flex gap-1">
              {[...Array(4)].map((_, i) => (
                <div
                  key={i}
                  className="w-1 bg-white rounded-full animate-sound-wave"
                  style={{
                    height: '20px',
                    animationDelay: `${i * 0.15}s`,
                  }}
                />
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Status label */}
      <span
        className={`text-lg font-medium ${
          connectionState === 'error' ? 'text-red-600' : 'text-gray-700 dark:text-gray-300'
        }`}
      >
        {statusLabel}
      </span>
    </div>
  );
}

export default StatusIndicator;
