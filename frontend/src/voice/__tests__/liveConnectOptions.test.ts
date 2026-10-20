import { EndSensitivity, StartSensitivity } from '@google/genai';
import { describe, expect, it } from 'vitest';
import { configuredLiveOptions } from '../liveConnectOptions';
import type { GeminiLiveConfig } from '../types';

const defaultVad = {
  automatic: true,
  start_of_speech_sensitivity: 'DEFAULT',
  end_of_speech_sensitivity: 'DEFAULT',
  prefix_padding_ms: null,
  silence_duration_ms: null,
};

function liveConfig(overrides: Partial<GeminiLiveConfig> = {}): GeminiLiveConfig {
  return {
    model: 'gemini-3.8-live',
    voice: { name: 'Puck', language_code: null },
    session: { max_duration_minutes: 15 },
    transcription: { input_enabled: true, output_enabled: true, language_codes: [] },
    vad: defaultVad,
    car_audio_vad: defaultVad,
    affective_dialog: { enabled: false },
    proactivity: { enabled: false, proactive_audio: false },
    thinking: { include_thoughts: false },
    ...overrides,
  };
}

describe('configuredLiveOptions', () => {
  it('sends nothing for settings left at their defaults', () => {
    expect(configuredLiveOptions(liveConfig())).toEqual({});
  });

  it('sends affective dialog, proactivity and thought summaries when configured', () => {
    const options = configuredLiveOptions(
      liveConfig({
        affective_dialog: { enabled: true },
        proactivity: { enabled: true, proactive_audio: true },
        thinking: { include_thoughts: true },
      })
    );

    expect(options).toEqual({
      enableAffectiveDialog: true,
      proactivity: { proactiveAudio: true },
      thinkingConfig: { includeThoughts: true },
    });
  });

  it('sends configured activity detection in the SDK enum spelling', () => {
    const options = configuredLiveOptions(
      liveConfig({
        vad: {
          automatic: true,
          start_of_speech_sensitivity: 'HIGH',
          end_of_speech_sensitivity: 'END_SENSITIVITY_LOW',
          prefix_padding_ms: 300,
          silence_duration_ms: 800,
        },
      })
    );

    expect(options.realtimeInputConfig).toEqual({
      automaticActivityDetection: {
        startOfSpeechSensitivity: StartSensitivity.START_SENSITIVITY_HIGH,
        endOfSpeechSensitivity: EndSensitivity.END_SENSITIVITY_LOW,
        prefixPaddingMs: 300,
        silenceDurationMs: 800,
      },
    });
  });
});
