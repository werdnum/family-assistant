/**
 * React hook for capturing audio from the microphone.
 *
 * Uses the Web Audio API with AudioWorklet for real-time audio capture
 * at 16kHz 16-bit PCM, as required by the Gemini Live API.
 */

import { useCallback, useRef, useState } from 'react';
import type { AudioCaptureState } from './types';
import { AUDIO_CONFIG } from './types';
import { createAudioWorkletProcessor } from './audioUtils';

interface UseAudioCaptureOptions {
  /** Callback called with each audio chunk (PCM 16-bit data) */
  onAudioData: (audioData: ArrayBuffer) => void;
  /** Callback called when an error occurs */
  onError?: (error: string) => void;
}

/**
 * Hook for capturing audio from the microphone.
 *
 * @param options - Configuration options
 * @returns Audio capture state and control functions
 */
export function useAudioCapture({
  onAudioData,
  onError,
}: UseAudioCaptureOptions): AudioCaptureState {
  const [isCapturing, setIsCapturing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [audioLevel, setAudioLevel] = useState(0);
  const [lastAudioFrameAt, setLastAudioFrameAt] = useState<number | null>(null);

  const audioContextRef = useRef<AudioContext | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const workletNodeRef = useRef<AudioWorkletNode | null>(null);
  const silentOutputRef = useRef<GainNode | null>(null);
  const workletUrlRef = useRef<string | null>(null);
  const sourceNodeRef = useRef<MediaStreamAudioSourceNode | null>(null);

  const startCapture = useCallback(async () => {
    if (isCapturing) {
      return;
    }

    try {
      setError(null);
      setAudioLevel(0);
      setLastAudioFrameAt(null);

      // Create and resume the context before any network waits consume the
      // original tap/click activation. iOS Safari is strict about this.
      const audioContext = new AudioContext();
      audioContextRef.current = audioContext;
      if (audioContext.state === 'suspended') {
        await audioContext.resume();
      }

      // Request microphone access
      // NOTE: Do NOT force sampleRate - iOS AEC breaks when forced to 16kHz
      // Let the browser capture at native rate (44.1k/48k), AudioWorklet resamples to 16kHz
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          // REMOVED: sampleRate - breaks iOS AEC
          channelCount: AUDIO_CONFIG.CHANNELS, // Mono - prevents iOS "Music Mode" which disables AEC
          // Standard AEC/noise processing
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          // Chrome/Chromium-specific aggressive AEC constraints (ignored on iOS WebKit)
          // These are non-standard WebRTC constraints supported by Chrome but not in TypeScript's
          // MediaTrackConstraints interface. They enable more aggressive echo cancellation.
          // See: https://developer.chrome.com/docs/web-platform/webrtc/constraints
          ...({
            googEchoCancellation: true,
            googExperimentalEchoCancellation: true,
            googAutoGainControl: true,
            googNoiseSuppression: true,
            googHighpassFilter: true,
          } as MediaTrackConstraints),
        },
      });
      mediaStreamRef.current = stream;

      if (audioContext.state === 'suspended') {
        await audioContext.resume();
      }

      // Create and register the AudioWorklet processor
      const workletUrl = createAudioWorkletProcessor();
      workletUrlRef.current = workletUrl;

      await audioContext.audioWorklet.addModule(workletUrl);

      // Create worklet node
      const workletNode = new AudioWorkletNode(audioContext, 'audio-capture-processor');
      workletNodeRef.current = workletNode;
      let hasReceivedFirstFrame = false;
      let resolveFirstFrame: (() => void) | null = null;
      const firstFramePromise = new Promise<void>((resolve) => {
        resolveFirstFrame = resolve;
      });

      // Handle audio data from the worklet
      workletNode.port.onmessage = (event: MessageEvent) => {
        if (event.data.type === 'audio') {
          onAudioData(event.data.data);
          setAudioLevel(event.data.level ?? 0);
          setLastAudioFrameAt(Date.now());
          if (!hasReceivedFirstFrame) {
            hasReceivedFirstFrame = true;
            resolveFirstFrame?.();
          }
        }
      };

      // Connect the media stream to the worklet
      const source = audioContext.createMediaStreamSource(stream);
      sourceNodeRef.current = source;
      source.connect(workletNode);

      // Safari will construct a worklet that never runs unless the graph is
      // connected to an output. A zero-gain destination keeps processing alive
      // without playing the microphone back to the user.
      const silentOutput = audioContext.createGain();
      silentOutput.gain.value = 0;
      silentOutputRef.current = silentOutput;
      workletNode.connect(silentOutput);
      silentOutput.connect(audioContext.destination);

      await Promise.race([
        firstFramePromise,
        new Promise<void>((_, reject) => {
          setTimeout(() => {
            reject(
              new Error(
                'Microphone started, but no audio frames were received. Try reloading the page and allowing microphone access again.'
              )
            );
          }, 2000);
        }),
      ]);

      setIsCapturing(true);
    } catch (err) {
      if (sourceNodeRef.current) {
        sourceNodeRef.current.disconnect();
        sourceNodeRef.current = null;
      }
      if (workletNodeRef.current) {
        workletNodeRef.current.disconnect();
        workletNodeRef.current = null;
      }
      if (silentOutputRef.current) {
        silentOutputRef.current.disconnect();
        silentOutputRef.current = null;
      }
      if (mediaStreamRef.current) {
        mediaStreamRef.current.getTracks().forEach((track) => track.stop());
        mediaStreamRef.current = null;
      }
      if (audioContextRef.current) {
        await audioContextRef.current.close();
        audioContextRef.current = null;
      }
      if (workletUrlRef.current) {
        URL.revokeObjectURL(workletUrlRef.current);
        workletUrlRef.current = null;
      }
      setIsCapturing(false);
      setAudioLevel(0);
      setLastAudioFrameAt(null);

      const errorMessage = err instanceof Error ? err.message : 'Failed to start audio capture';

      // Handle specific error types
      if (err instanceof DOMException) {
        if (err.name === 'NotAllowedError') {
          setError('Microphone access denied. Please allow microphone access to use voice mode.');
        } else if (err.name === 'NotFoundError') {
          setError('No microphone found. Please connect a microphone and try again.');
        } else {
          setError(`Audio error: ${err.message}`);
        }
      } else {
        setError(errorMessage);
      }

      onError?.(errorMessage);
      throw err instanceof Error ? err : new Error(errorMessage);
    }
  }, [isCapturing, onAudioData, onError]);

  const stopCapture = useCallback(() => {
    if (sourceNodeRef.current) {
      sourceNodeRef.current.disconnect();
      sourceNodeRef.current = null;
    }

    // Disconnect and clean up worklet node
    if (workletNodeRef.current) {
      workletNodeRef.current.disconnect();
      workletNodeRef.current = null;
    }

    if (silentOutputRef.current) {
      silentOutputRef.current.disconnect();
      silentOutputRef.current = null;
    }

    // Stop media stream tracks
    if (mediaStreamRef.current) {
      mediaStreamRef.current.getTracks().forEach((track) => track.stop());
      mediaStreamRef.current = null;
    }

    // Close audio context
    if (audioContextRef.current) {
      audioContextRef.current.close();
      audioContextRef.current = null;
    }

    // Revoke blob URL
    if (workletUrlRef.current) {
      URL.revokeObjectURL(workletUrlRef.current);
      workletUrlRef.current = null;
    }

    setIsCapturing(false);
    setAudioLevel(0);
    setLastAudioFrameAt(null);
  }, []);

  /**
   * Set ducking level to suppress echo while AI is speaking.
   * Uses 10% volume (not mute) to allow barge-in and avoid iOS "double mute" bug.
   */
  const setDucking = useCallback((isDucked: boolean) => {
    workletNodeRef.current?.port.postMessage({
      type: 'setDucking',
      gain: isDucked ? 0.1 : 1.0, // 10% when ducking, 100% otherwise
    });
  }, []);

  return {
    isCapturing,
    error,
    audioLevel,
    lastAudioFrameAt,
    startCapture,
    stopCapture,
    setDucking,
  };
}
