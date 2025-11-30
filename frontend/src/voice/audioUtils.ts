/**
 * Audio utility functions for Gemini Live API integration.
 *
 * Handles conversion between ArrayBuffer (raw PCM audio) and base64 strings
 * required for the Gemini Live API WebSocket protocol.
 */

import { AUDIO_CONFIG } from './types';

/**
 * Convert ArrayBuffer to base64 string.
 *
 * @param buffer - The ArrayBuffer containing audio data
 * @returns Base64-encoded string
 */
export function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (let i = 0; i < bytes.byteLength; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

/**
 * Convert base64 string to ArrayBuffer.
 *
 * @param base64 - Base64-encoded string
 * @returns ArrayBuffer containing the decoded data
 */
export function base64ToArrayBuffer(base64: string): ArrayBuffer {
  const binaryString = atob(base64);
  const bytes = new Uint8Array(binaryString.length);
  for (let i = 0; i < binaryString.length; i++) {
    bytes[i] = binaryString.charCodeAt(i);
  }
  return bytes.buffer;
}

/**
 * Convert Float32Array (from Web Audio API) to Int16Array (PCM 16-bit).
 *
 * @param float32Array - Audio samples as Float32Array (range -1 to 1)
 * @returns Int16Array with PCM 16-bit samples
 */
export function float32ToInt16(float32Array: Float32Array): Int16Array {
  const int16Array = new Int16Array(float32Array.length);
  for (let i = 0; i < float32Array.length; i++) {
    // Clamp to -1 to 1 range and scale to 16-bit
    const sample = Math.max(-1, Math.min(1, float32Array[i]));
    int16Array[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return int16Array;
}

/**
 * Convert Int16Array (PCM 16-bit) to Float32Array (for Web Audio API).
 *
 * @param int16Array - PCM 16-bit audio samples
 * @returns Float32Array with samples in range -1 to 1
 */
export function int16ToFloat32(int16Array: Int16Array): Float32Array {
  const float32Array = new Float32Array(int16Array.length);
  for (let i = 0; i < int16Array.length; i++) {
    float32Array[i] = int16Array[i] / (int16Array[i] < 0 ? 0x8000 : 0x7fff);
  }
  return float32Array;
}

/**
 * Create an AudioWorklet processor script as a blob URL.
 *
 * This creates a worklet that captures audio from the microphone,
 * resamples it to 16kHz, and sends it to the main thread.
 *
 * @returns Blob URL for the AudioWorklet processor
 */
export function createAudioWorkletProcessor(): string {
  const processorCode = `
    class AudioCaptureProcessor extends AudioWorkletProcessor {
      constructor() {
        super();
        this.buffer = [];
        this.targetSampleRate = ${AUDIO_CONFIG.INPUT_SAMPLE_RATE};
        this.chunkSize = ${AUDIO_CONFIG.CHUNK_SIZE};
        // Ducking gain: 1.0 = normal, 0.1 = ducked (while AI is speaking)
        // Uses 10% (not mute) to allow barge-in and avoid iOS "double mute" bug
        this.duckingGain = 1.0;
        // Pre-allocated working buffer to avoid GC pressure on audio thread
        this.workBuffer = null;

        // Listen for ducking control messages from main thread
        this.port.onmessage = (event) => {
          if (event.data.type === 'setDucking') {
            this.duckingGain = event.data.gain;
          }
        };
      }

      process(inputs) {
        const input = inputs[0];
        if (!input || !input[0]) return true;

        // Copy samples and apply ducking BEFORE resampling
        // This suppresses echo from AI playback while preserving user's loud voice for barge-in
        const rawSamples = input[0];
        // Reuse working buffer to avoid GC pressure on audio thread
        if (!this.workBuffer || this.workBuffer.length !== rawSamples.length) {
          this.workBuffer = new Float32Array(rawSamples.length);
        }
        const samples = this.workBuffer;
        for (let i = 0; i < rawSamples.length; i++) {
          samples[i] = rawSamples[i] * this.duckingGain;
        }

        // Resample if necessary (browser might not be at 16kHz)
        const ratio = sampleRate / this.targetSampleRate;
        const outputLength = Math.ceil(samples.length / ratio);

        for (let i = 0; i < outputLength; i++) {
          const srcIndex = i * ratio;
          const srcIndexFloor = Math.floor(srcIndex);
          const srcIndexCeil = Math.min(srcIndexFloor + 1, samples.length - 1);
          const fraction = srcIndex - srcIndexFloor;

          const sample = samples[srcIndexFloor] * (1 - fraction) + samples[srcIndexCeil] * fraction;
          this.buffer.push(sample);
        }

        // Send chunks to main thread
        while (this.buffer.length >= this.chunkSize) {
          const chunk = this.buffer.splice(0, this.chunkSize);
          const float32Chunk = new Float32Array(chunk);

          // Convert to Int16 PCM
          const int16Chunk = new Int16Array(float32Chunk.length);
          for (let i = 0; i < float32Chunk.length; i++) {
            const sample = Math.max(-1, Math.min(1, float32Chunk[i]));
            int16Chunk[i] = sample < 0 ? sample * 0x8000 : sample * 0x7FFF;
          }

          this.port.postMessage({
            type: 'audio',
            data: int16Chunk.buffer
          }, [int16Chunk.buffer]);
        }

        return true;
      }
    }

    registerProcessor('audio-capture-processor', AudioCaptureProcessor);
  `;

  const blob = new Blob([processorCode], { type: 'application/javascript' });
  return URL.createObjectURL(blob);
}

/**
 * Create PCM audio data suitable for Web Audio API playback.
 *
 * @param audioContext - The AudioContext to use
 * @param pcmData - Raw PCM 16-bit audio data
 * @param sampleRate - The sample rate of the PCM data
 * @returns AudioBuffer ready for playback
 */
export function createAudioBufferFromPCM(
  audioContext: AudioContext,
  pcmData: ArrayBuffer,
  sampleRate: number = AUDIO_CONFIG.OUTPUT_SAMPLE_RATE
): AudioBuffer {
  const int16Array = new Int16Array(pcmData);
  const float32Array = int16ToFloat32(int16Array);

  const audioBuffer = audioContext.createBuffer(1, float32Array.length, sampleRate);
  audioBuffer.getChannelData(0).set(float32Array);

  return audioBuffer;
}

/**
 * Generate a unique ID for transcript entries.
 */
export function generateTranscriptId(): string {
  return `transcript_${Date.now()}_${Math.random().toString(36).substring(2, 9)}`;
}
