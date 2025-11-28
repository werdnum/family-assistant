/**
 * Voice controls for starting and ending voice sessions.
 */

import type { VoiceConnectionState } from './types';

interface VoiceControlsProps {
  connectionState: VoiceConnectionState;
  onStartCall: () => void;
  onEndCall: () => void;
  disabled?: boolean;
}

/**
 * Voice control buttons for managing voice sessions.
 */
export function VoiceControls({
  connectionState,
  onStartCall,
  onEndCall,
  disabled = false,
}: VoiceControlsProps) {
  const isConnected = connectionState === 'connected';
  const isConnecting = connectionState === 'connecting';

  return (
    <div className="flex justify-center gap-4">
      {!isConnected && !isConnecting ? (
        <button
          type="button"
          onClick={onStartCall}
          disabled={disabled}
          className={`
            px-8 py-4 rounded-full text-lg font-semibold
            transition-all duration-200
            ${
              disabled
                ? 'bg-gray-300 text-gray-500 cursor-not-allowed'
                : 'bg-green-500 hover:bg-green-600 text-white shadow-lg hover:shadow-xl active:scale-95'
            }
          `}
        >
          <span className="flex items-center gap-2">
            <MicrophoneIcon />
            Start Call
          </span>
        </button>
      ) : (
        <button
          type="button"
          onClick={onEndCall}
          disabled={isConnecting}
          className={`
            px-8 py-4 rounded-full text-lg font-semibold
            transition-all duration-200
            ${
              isConnecting
                ? 'bg-gray-300 text-gray-500 cursor-not-allowed'
                : 'bg-red-500 hover:bg-red-600 text-white shadow-lg hover:shadow-xl active:scale-95'
            }
          `}
        >
          <span className="flex items-center gap-2">
            <PhoneOffIcon />
            End Call
          </span>
        </button>
      )}
    </div>
  );
}

/**
 * Microphone icon component.
 */
function MicrophoneIcon() {
  return (
    <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={2}
        d="M19 11a7 7 0 01-7 7m0 0a7 7 0 01-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 01-3-3V5a3 3 0 116 0v6a3 3 0 01-3 3z"
      />
    </svg>
  );
}

/**
 * Phone off icon component.
 */
function PhoneOffIcon() {
  return (
    <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={2}
        d="M16 8l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2M5 3a2 2 0 00-2 2v1c0 8.284 6.716 15 15 15h1a2 2 0 002-2v-3.28a1 1 0 00-.684-.948l-4.493-1.498a1 1 0 00-1.21.502l-1.13 2.257a11.042 11.042 0 01-5.516-5.517l2.257-1.128a1 1 0 00.502-1.21L9.228 3.683A1 1 0 008.279 3H5z"
      />
    </svg>
  );
}

export default VoiceControls;
