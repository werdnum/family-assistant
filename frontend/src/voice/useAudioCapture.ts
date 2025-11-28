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

  const audioContextRef = useRef<AudioContext | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const workletNodeRef = useRef<AudioWorkletNode | null>(null);
  const workletUrlRef = useRef<string | null>(null);

  const startCapture = useCallback(async () => {
    if (isCapturing) {
      return;
    }

    try {
      setError(null);

      // Request microphone access
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate: AUDIO_CONFIG.INPUT_SAMPLE_RATE,
          channelCount: AUDIO_CONFIG.CHANNELS,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      mediaStreamRef.current = stream;

      // Create audio context
      const audioContext = new AudioContext({
        sampleRate: AUDIO_CONFIG.INPUT_SAMPLE_RATE,
      });
      audioContextRef.current = audioContext;

      // Create and register the AudioWorklet processor
      const workletUrl = createAudioWorkletProcessor();
      workletUrlRef.current = workletUrl;

      await audioContext.audioWorklet.addModule(workletUrl);

      // Create worklet node
      const workletNode = new AudioWorkletNode(audioContext, 'audio-capture-processor');
      workletNodeRef.current = workletNode;

      // Handle audio data from the worklet
      workletNode.port.onmessage = (event: MessageEvent) => {
        if (event.data.type === 'audio') {
          onAudioData(event.data.data);
        }
      };

      // Connect the media stream to the worklet
      const source = audioContext.createMediaStreamSource(stream);
      source.connect(workletNode);
      // Don't connect to destination - we don't want to hear ourselves

      setIsCapturing(true);
    } catch (err) {
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
    }
  }, [isCapturing, onAudioData, onError]);

  const stopCapture = useCallback(() => {
    // Disconnect and clean up worklet node
    if (workletNodeRef.current) {
      workletNodeRef.current.disconnect();
      workletNodeRef.current = null;
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
  }, []);

  return {
    isCapturing,
    error,
    startCapture,
    stopCapture,
  };
}
