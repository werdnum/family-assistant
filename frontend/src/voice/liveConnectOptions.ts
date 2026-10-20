/**
 * The Live session settings the backend configures, as LiveConnectConfig fields.
 *
 * Kept apart from the session hook so each configured setting can be checked
 * against the wire shape without opening a socket.
 */

import {
  EndSensitivity,
  type LiveConnectConfig,
  type RealtimeInputConfig,
  StartSensitivity,
} from '@google/genai';
import type { GeminiLiveConfig, GeminiLiveVADConfig } from './types';

type ConfiguredLiveOptions = Pick<
  LiveConnectConfig,
  'enableAffectiveDialog' | 'proactivity' | 'thinkingConfig' | 'realtimeInputConfig'
>;

function startSensitivity(raw: string): StartSensitivity | null | undefined {
  switch (raw.toUpperCase()) {
    case '':
    case 'DEFAULT':
    case 'START_SENSITIVITY_UNSPECIFIED':
      return null;
    case 'LOW':
    case 'START_SENSITIVITY_LOW':
      return StartSensitivity.START_SENSITIVITY_LOW;
    case 'HIGH':
    case 'START_SENSITIVITY_HIGH':
      return StartSensitivity.START_SENSITIVITY_HIGH;
    default:
      return undefined;
  }
}

function endSensitivity(raw: string): EndSensitivity | null | undefined {
  switch (raw.toUpperCase()) {
    case '':
    case 'DEFAULT':
    case 'END_SENSITIVITY_UNSPECIFIED':
      return null;
    case 'LOW':
    case 'END_SENSITIVITY_LOW':
      return EndSensitivity.END_SENSITIVITY_LOW;
    case 'HIGH':
    case 'END_SENSITIVITY_HIGH':
      return EndSensitivity.END_SENSITIVITY_HIGH;
    default:
      return undefined;
  }
}

/**
 * Automatic activity detection, omitting every setting left at Gemini's default.
 *
 * Manual detection is not offered here: it needs the client to send its own
 * activity start and end signals, which this client does not do, so turning
 * automatic detection off would leave the model never hearing a turn end.
 */
export function realtimeInputConfig(vad: GeminiLiveVADConfig): RealtimeInputConfig | undefined {
  const detection: NonNullable<RealtimeInputConfig['automaticActivityDetection']> = {};
  if (!vad.automatic) {
    console.warn('Gemini Live: manual activity detection is unsupported in the web client');
  }
  const start = startSensitivity(vad.start_of_speech_sensitivity);
  if (start === undefined) {
    console.warn(
      `Gemini Live: unknown start_of_speech_sensitivity '${vad.start_of_speech_sensitivity}'`
    );
  } else if (start !== null) {
    detection.startOfSpeechSensitivity = start;
  }
  const end = endSensitivity(vad.end_of_speech_sensitivity);
  if (end === undefined) {
    console.warn(
      `Gemini Live: unknown end_of_speech_sensitivity '${vad.end_of_speech_sensitivity}'`
    );
  } else if (end !== null) {
    detection.endOfSpeechSensitivity = end;
  }
  if (vad.prefix_padding_ms !== null) {
    detection.prefixPaddingMs = vad.prefix_padding_ms;
  }
  if (vad.silence_duration_ms !== null) {
    detection.silenceDurationMs = vad.silence_duration_ms;
  }
  return Object.keys(detection).length > 0 ? { automaticActivityDetection: detection } : undefined;
}

export function configuredLiveOptions(config: GeminiLiveConfig): ConfiguredLiveOptions {
  const options: ConfiguredLiveOptions = {};
  if (config.affective_dialog.enabled) {
    options.enableAffectiveDialog = true;
  }
  if (config.proactivity.enabled) {
    options.proactivity = { proactiveAudio: config.proactivity.proactive_audio };
  }
  if (config.thinking.include_thoughts) {
    options.thinkingConfig = { includeThoughts: true };
  }
  const realtime = realtimeInputConfig(config.vad);
  if (realtime) {
    options.realtimeInputConfig = realtime;
  }
  return options;
}
