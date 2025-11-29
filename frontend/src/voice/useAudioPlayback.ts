/**
 * React hook for playing audio received from the Gemini Live API.
 *
 * Manages an audio queue for gapless playback of PCM audio chunks
 * at 24kHz sample rate as returned by Gemini.
 */

import { useCallback, useRef, useState } from 'react';
import type { AudioPlaybackState } from './types';
import { AUDIO_CONFIG } from './types';
import { createAudioBufferFromPCM } from './audioUtils';

interface QueuedAudio {
  buffer: AudioBuffer;
}

/**
 * Hook for playing audio from the Gemini Live API.
 *
 * @returns Audio playback state and control functions
 */
export function useAudioPlayback(): AudioPlaybackState {
  const [isPlaying, setIsPlaying] = useState(false);

  const audioContextRef = useRef<AudioContext | null>(null);
  const audioQueueRef = useRef<QueuedAudio[]>([]);
  const activeSourcesRef = useRef<Set<AudioBufferSourceNode>>(new Set());
  const nextStartTimeRef = useRef<number>(0);
  const isProcessingRef = useRef(false);

  /**
   * Get or create the AudioContext.
   * AudioContext must be created/resumed after user interaction.
   */
  const getAudioContext = useCallback((): AudioContext => {
    if (!audioContextRef.current) {
      audioContextRef.current = new AudioContext({
        sampleRate: AUDIO_CONFIG.OUTPUT_SAMPLE_RATE,
      });
    }

    // Resume if suspended (happens after page load without user interaction)
    if (audioContextRef.current.state === 'suspended') {
      audioContextRef.current.resume();
    }

    return audioContextRef.current;
  }, []);

  /**
   * Process the audio queue and schedule playback.
   */
  const processQueue = useCallback(() => {
    if (isProcessingRef.current) {
      return;
    }
    isProcessingRef.current = true;

    const audioContext = getAudioContext();
    const currentTime = audioContext.currentTime;

    // Update next start time if it's in the past
    if (nextStartTimeRef.current < currentTime) {
      nextStartTimeRef.current = currentTime;
    }

    // Schedule all queued audio
    while (audioQueueRef.current.length > 0) {
      const item = audioQueueRef.current.shift();
      if (!item) {
        break;
      }

      const source = audioContext.createBufferSource();
      source.buffer = item.buffer;
      source.connect(audioContext.destination);

      // Track active source for stopping later
      activeSourcesRef.current.add(source);

      // Schedule playback
      source.start(nextStartTimeRef.current);

      // Update next start time for gapless playback
      nextStartTimeRef.current += item.buffer.duration;

      // Track playing state and cleanup
      source.onended = () => {
        activeSourcesRef.current.delete(source);
        // Check if all audio finished
        if (audioQueueRef.current.length === 0 && activeSourcesRef.current.size === 0) {
          setIsPlaying(false);
        }
      };
    }

    setIsPlaying(true);
    isProcessingRef.current = false;
  }, [getAudioContext]);

  /**
   * Queue audio data for playback.
   * Audio will be played in order, gaplessly.
   */
  const queueAudio = useCallback(
    (audioData: ArrayBuffer) => {
      const audioContext = getAudioContext();

      try {
        // Convert PCM data to AudioBuffer
        const audioBuffer = createAudioBufferFromPCM(
          audioContext,
          audioData,
          AUDIO_CONFIG.OUTPUT_SAMPLE_RATE
        );

        // Add to queue
        audioQueueRef.current.push({ buffer: audioBuffer });

        // Process the queue
        processQueue();
      } catch (error) {
        console.error('Error queueing audio:', error);
      }
    },
    [getAudioContext, processQueue]
  );

  /**
   * Clear the audio queue without stopping current playback.
   */
  const clearQueue = useCallback(() => {
    audioQueueRef.current = [];
  }, []);

  /**
   * Stop all playback and clear the queue.
   */
  const stopPlayback = useCallback(() => {
    // Stop all active sources (currently playing or scheduled)
    activeSourcesRef.current.forEach((source) => {
      try {
        source.stop();
      } catch {
        // Ignore errors if already stopped
      }
    });
    activeSourcesRef.current.clear();

    // Clear the queue
    audioQueueRef.current = [];

    // Reset timing
    nextStartTimeRef.current = 0;

    setIsPlaying(false);
  }, []);

  return {
    isPlaying,
    queueAudio,
    clearQueue,
    stopPlayback,
  };
}
