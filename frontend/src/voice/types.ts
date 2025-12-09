/**
 * Voice mode TypeScript types for Gemini Live API integration.
 */

import type { Tool } from '@google/genai';

/**
 * Connection states for the voice session.
 */
export type VoiceConnectionState = 'disconnected' | 'connecting' | 'connected' | 'error';

/**
 * Activity states for the voice session.
 */
export type VoiceActivityState = 'idle' | 'listening' | 'processing' | 'speaking';

/**
 * Combined state for the voice session.
 */
export interface VoiceSessionState {
  connection: VoiceConnectionState;
  activity: VoiceActivityState;
  error: string | null;
  sessionStartTime: number | null;
  /** Session duration in seconds */
  sessionDuration: number;
  /** Detailed status message during connection (e.g., "Fetching token...", "Connecting to Gemini...") */
  connectingStatus?: string;
}

/**
 * Transcript entry for voice conversation.
 */
export interface TranscriptEntry {
  id: string;
  role: 'user' | 'assistant' | 'tool';
  text: string;
  timestamp: Date;
  /** Whether this is a final or interim transcript */
  isFinal: boolean;
  /** Tool-specific fields (only present when role === 'tool') */
  toolName?: string;
  toolArgs?: Record<string, unknown>;
  toolStatus?: 'running' | 'complete' | 'error';
  toolResult?: unknown;
}

/**
 * Tool call received from Gemini.
 */
export interface GeminiToolCall {
  id: string;
  name: string;
  args: Record<string, unknown>;
}

/**
 * Tool response to send back to Gemini.
 */
export interface GeminiToolResponse {
  id: string;
  name: string;
  response: {
    result?: unknown;
    error?: string;
  };
}

/**
 * Gemini Live voice configuration from backend.
 */
export interface GeminiLiveVoiceConfig {
  name: string;
}

/**
 * Gemini Live session configuration from backend.
 */
export interface GeminiLiveSessionConfig {
  max_duration_minutes: number;
}

/**
 * Gemini Live transcription configuration from backend.
 */
export interface GeminiLiveTranscriptionConfig {
  input_enabled: boolean;
  output_enabled: boolean;
}

/**
 * Gemini Live VAD configuration from backend.
 */
export interface GeminiLiveVADConfig {
  automatic: boolean;
  start_of_speech_sensitivity: string;
  end_of_speech_sensitivity: string;
  prefix_padding_ms: number | null;
  silence_duration_ms: number | null;
}

/**
 * Gemini Live affective dialog configuration from backend.
 */
export interface GeminiLiveAffectiveDialogConfig {
  enabled: boolean;
}

/**
 * Gemini Live proactivity configuration from backend.
 */
export interface GeminiLiveProactivityConfig {
  enabled: boolean;
  proactive_audio: boolean;
}

/**
 * Gemini Live thinking configuration from backend.
 */
export interface GeminiLiveThinkingConfig {
  include_thoughts: boolean;
}

/**
 * Full Gemini Live configuration from backend.
 */
export interface GeminiLiveConfig {
  model: string;
  voice: GeminiLiveVoiceConfig;
  session: GeminiLiveSessionConfig;
  transcription: GeminiLiveTranscriptionConfig;
  vad: GeminiLiveVADConfig;
  affective_dialog: GeminiLiveAffectiveDialogConfig;
  proactivity: GeminiLiveProactivityConfig;
  thinking: GeminiLiveThinkingConfig;
}

/**
 * Ephemeral token response from backend.
 * Uses SDK's Tool type directly to ensure type compatibility.
 */
export interface EphemeralTokenResponse {
  token: string;
  expires_at: string;
  tools: Tool[];
  system_instruction: string;
  model: string;
  config: GeminiLiveConfig;
}

/**
 * Audio capture hook return type.
 */
export interface AudioCaptureState {
  isCapturing: boolean;
  error: string | null;
  startCapture: () => Promise<void>;
  stopCapture: () => void;
  /** Set ducking level (true = 10% volume to suppress echo while AI speaks) */
  setDucking: (isDucked: boolean) => void;
}

/**
 * Audio playback hook return type.
 */
export interface AudioPlaybackState {
  isPlaying: boolean;
  queueAudio: (audioData: ArrayBuffer) => void;
  clearQueue: () => void;
  stopPlayback: () => void;
}

/**
 * Gemini Live connection hook return type.
 */
export interface GeminiLiveState {
  sessionState: VoiceSessionState;
  transcripts: TranscriptEntry[];
  connect: (profileId?: string) => Promise<void>;
  disconnect: () => void;
  sendAudio: (audioData: ArrayBuffer) => void;
}

/**
 * Audio configuration constants.
 */
export const AUDIO_CONFIG = {
  /** Input sample rate for microphone (Gemini requires 16kHz) */
  INPUT_SAMPLE_RATE: 16000,
  /** Output sample rate from Gemini (24kHz) */
  OUTPUT_SAMPLE_RATE: 24000,
  /** Bits per sample for PCM audio */
  BITS_PER_SAMPLE: 16,
  /** Number of audio channels (mono) */
  CHANNELS: 1,
  /** Chunk size in samples for audio processing */
  CHUNK_SIZE: 4096,
} as const;

/**
 * Session configuration constants.
 */
export const SESSION_CONFIG = {
  /** Maximum session duration in minutes */
  MAX_DURATION_MINUTES: 15,
  /** Token validity in minutes */
  TOKEN_VALIDITY_MINUTES: 30,
  /** Reconnect attempt delay in ms */
  RECONNECT_DELAY_MS: 1000,
  /** Maximum reconnect attempts */
  MAX_RECONNECT_ATTEMPTS: 3,
} as const;
