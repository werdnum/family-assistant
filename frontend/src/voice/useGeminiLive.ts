/**
 * React hook for managing Gemini Live API connection.
 *
 * Handles:
 * - Ephemeral token fetching from backend
 * - WebSocket connection to Gemini Live API
 * - Audio streaming (send/receive)
 * - Transcription handling
 * - Tool call routing to backend
 */

import { GoogleGenAI, Modality, type Session } from '@google/genai';
import { useCallback, useEffect, useRef, useState } from 'react';
import { arrayBufferToBase64, base64ToArrayBuffer, generateTranscriptId } from './audioUtils';
import type {
  EphemeralTokenResponse,
  GeminiLiveState,
  GeminiToolCall,
  TranscriptEntry,
  VoiceActivityState,
  VoiceConnectionState,
  VoiceSessionState,
} from './types';
import { SESSION_CONFIG } from './types';
import { useAudioCapture } from './useAudioCapture';
import { useAudioPlayback } from './useAudioPlayback';

const GEMINI_API_HOST = 'generativelanguage.googleapis.com';

/**
 * Test seam: allows injecting a mock session factory for integration tests.
 * When set, this factory is called instead of using the real GoogleGenAI SDK.
 *
 * The factory receives the token data and should return a mock Session object that:
 * - Is async iterable (yields LiveServerMessage objects)
 * - Has sendToolResponse(), sendRealtimeInput(), close() methods
 *
 * @example
 * // In Playwright test:
 * await page.addInitScript(() => {
 *   window.__TEST_GEMINI_SESSION_FACTORY__ = async (tokenData) => {
 *     // Return mock session
 *   };
 * });
 */
declare global {
  interface Window {
    __TEST_GEMINI_SESSION_FACTORY__?: (tokenData: EphemeralTokenResponse) => Promise<Session>;
  }
}

/**
 * Hook for managing Gemini Live API connection and voice interaction.
 *
 * @returns Gemini Live state and control functions
 */
export function useGeminiLive(): GeminiLiveState {
  // Session state
  const [connectionState, setConnectionState] = useState<VoiceConnectionState>('disconnected');
  const [activityState, setActivityState] = useState<VoiceActivityState>('idle');
  const [error, setError] = useState<string | null>(null);
  const [sessionStartTime, setSessionStartTime] = useState<number | null>(null);
  const [sessionDuration, setSessionDuration] = useState(0);
  const [transcripts, setTranscripts] = useState<TranscriptEntry[]>([]);

  // Refs for mutable state
  const sessionRef = useRef<Session | null>(null);
  const clientRef = useRef<GoogleGenAI | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const sessionTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const currentTranscriptRef = useRef<TranscriptEntry | null>(null);

  // Audio hooks
  const audioPlayback = useAudioPlayback();

  // Store stop functions in refs to avoid dependency issues with disconnect
  const stopCaptureRef = useRef<() => void>(() => {});
  const stopPlaybackRef = useRef<() => void>(() => {});

  /**
   * Handle incoming audio data from Gemini.
   */
  const handleAudioResponse = useCallback(
    (audioData: string) => {
      const pcmData = base64ToArrayBuffer(audioData);
      audioPlayback.queueAudio(pcmData);
      setActivityState('speaking');
    },
    [audioPlayback]
  );

  /**
   * Handle transcription updates from Gemini.
   */
  const handleTranscription = useCallback(
    (text: string, role: 'user' | 'assistant', isFinal: boolean) => {
      if (!text.trim()) {
        return;
      }

      if (isFinal) {
        // Create or finalize transcript entry
        const newEntry: TranscriptEntry = {
          id: generateTranscriptId(),
          role,
          text: text.trim(),
          timestamp: new Date(),
          isFinal: true,
        };
        setTranscripts((prev) => [...prev, newEntry]);
        currentTranscriptRef.current = null;
      } else {
        // Update interim transcript
        if (currentTranscriptRef.current?.role === role) {
          currentTranscriptRef.current.text = text.trim();
        } else {
          currentTranscriptRef.current = {
            id: generateTranscriptId(),
            role,
            text: text.trim(),
            timestamp: new Date(),
            isFinal: false,
          };
        }
      }
    },
    []
  );

  /**
   * Execute a tool call via the backend.
   */
  const executeToolCall = useCallback(async (toolCall: GeminiToolCall) => {
    try {
      const response = await fetch(`/api/tools/execute/${toolCall.name}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ arguments: toolCall.args }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        return {
          id: toolCall.id,
          name: toolCall.name,
          response: {
            error: errorData.detail || `Tool execution failed with status ${response.status}`,
          },
        };
      }

      const result = await response.json();
      return {
        id: toolCall.id,
        name: toolCall.name,
        response: { result },
      };
    } catch (err) {
      return {
        id: toolCall.id,
        name: toolCall.name,
        response: {
          error: err instanceof Error ? err.message : 'Unknown error executing tool',
        },
      };
    }
  }, []);

  /**
   * Handle tool calls from Gemini.
   */
  const handleToolCalls = useCallback(
    async (toolCalls: GeminiToolCall[]) => {
      if (!sessionRef.current) {
        return;
      }

      setActivityState('processing');

      const responses = await Promise.all(toolCalls.map(executeToolCall));

      // Send tool responses back to Gemini
      try {
        const functionResponses = responses.map((r) => ({
          id: r.id,
          name: r.name,
          response: r.response,
        }));

        await sessionRef.current.sendToolResponse({ functionResponses });
      } catch (err) {
        console.error('Error sending tool responses:', err);
      }
    },
    [executeToolCall]
  );

  /**
   * Send audio data to Gemini.
   */
  const sendAudio = useCallback((audioData: ArrayBuffer) => {
    if (!sessionRef.current) {
      return;
    }

    try {
      const base64Audio = arrayBufferToBase64(audioData);
      sessionRef.current.sendRealtimeInput({
        audio: {
          data: base64Audio,
          mimeType: 'audio/pcm;rate=16000',
        },
      });
    } catch (err) {
      console.error('Error sending audio:', err);
    }
  }, []);

  // Audio capture with callback to send to Gemini
  const audioCapture = useAudioCapture({
    onAudioData: sendAudio,
    onError: (err) => setError(err),
  });

  // Keep refs up to date for stable disconnect function
  stopCaptureRef.current = audioCapture.stopCapture;
  stopPlaybackRef.current = audioPlayback.stopPlayback;

  /**
   * Process messages from the Gemini session.
   */
  const processSessionMessages = useCallback(async () => {
    if (!sessionRef.current) {
      return;
    }

    try {
      for await (const message of sessionRef.current) {
        // Handle different message types
        if (message.serverContent) {
          const content = message.serverContent;

          // Handle model turn (assistant speaking)
          if (content.modelTurn?.parts) {
            for (const part of content.modelTurn.parts) {
              if (part.inlineData?.data) {
                handleAudioResponse(part.inlineData.data);
              }
              if (part.text) {
                handleTranscription(part.text, 'assistant', false);
              }
            }
          }

          // Handle turn completion
          if (content.turnComplete) {
            setActivityState('listening');
            if (currentTranscriptRef.current?.role === 'assistant') {
              handleTranscription(currentTranscriptRef.current.text, 'assistant', true);
            }
          }

          // Handle interruption
          if (content.interrupted) {
            audioPlayback.stopPlayback();
            setActivityState('listening');
          }

          // Handle input transcription
          if (content.inputTranscription?.text) {
            handleTranscription(content.inputTranscription.text, 'user', true);
          }

          // Handle output transcription
          if (content.outputTranscription?.text) {
            handleTranscription(content.outputTranscription.text, 'assistant', true);
          }
        }

        // Handle tool calls
        if (message.toolCall?.functionCalls) {
          const toolCalls: GeminiToolCall[] = message.toolCall.functionCalls.map((fc) => ({
            id: fc.id || generateTranscriptId(),
            name: fc.name || '',
            args: (fc.args as Record<string, unknown>) || {},
          }));
          await handleToolCalls(toolCalls);
        }

        // Handle go away (session ending)
        if (message.goAway) {
          console.warn('Received GoAway from Gemini, session ending');
          // Could implement reconnection here
        }
      }
    } catch (err) {
      if (err instanceof Error && err.message.includes('closed')) {
        // Session was closed, this is expected on disconnect
        return;
      }
      console.error('Error processing session messages:', err);
      setError(err instanceof Error ? err.message : 'Error processing messages');
      setConnectionState('error');
    }
  }, [handleAudioResponse, handleTranscription, handleToolCalls, audioPlayback]);

  /**
   * Connect to Gemini Live API.
   */
  const connect = useCallback(
    async (profileId?: string) => {
      if (connectionState === 'connecting' || connectionState === 'connected') {
        return;
      }

      try {
        setConnectionState('connecting');
        setError(null);
        setTranscripts([]);

        // Fetch ephemeral token from backend
        const tokenResponse = await fetch('/api/gemini/ephemeral-token', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ profile_id: profileId }),
        });

        if (!tokenResponse.ok) {
          const errorData = await tokenResponse.json().catch(() => ({}));
          throw new Error(errorData.detail || 'Failed to get ephemeral token');
        }

        const tokenData: EphemeralTokenResponse = await tokenResponse.json();

        let session: Session;

        // Check for test seam - allows injecting mock session for integration tests
        if (typeof window !== 'undefined' && window.__TEST_GEMINI_SESSION_FACTORY__) {
          session = await window.__TEST_GEMINI_SESSION_FACTORY__(tokenData);
        } else {
          // Create Gemini client with ephemeral token
          const client = new GoogleGenAI({
            apiKey: tokenData.token,
            httpOptions: {
              apiVersion: 'v1alpha',
              baseUrl: `https://${GEMINI_API_HOST}`,
            },
          });
          clientRef.current = client;

          // Create live session
          session = await client.live.connect({
            model: tokenData.model,
            config: {
              responseModalities: [Modality.AUDIO],
              systemInstruction: {
                parts: [{ text: tokenData.system_instruction }],
              },
              tools: tokenData.tools,
              speechConfig: {
                voiceConfig: {
                  prebuiltVoiceConfig: {
                    voiceName: 'Puck',
                  },
                },
              },
            },
          });
        }
        sessionRef.current = session;

        // Start processing messages
        processSessionMessages();

        // Start audio capture
        await audioCapture.startCapture();

        // Set up session state
        setConnectionState('connected');
        setActivityState('listening');
        setSessionStartTime(Date.now());
        reconnectAttemptsRef.current = 0;

        // Start session timer
        sessionTimerRef.current = setInterval(() => {
          setSessionDuration((prev) => {
            const newDuration = prev + 1;
            // Auto-disconnect at max duration
            if (newDuration >= SESSION_CONFIG.MAX_DURATION_MINUTES * 60) {
              disconnect();
            }
            return newDuration;
          });
        }, 1000);
      } catch (err) {
        console.error('Error connecting to Gemini:', err);
        setError(err instanceof Error ? err.message : 'Failed to connect');
        setConnectionState('error');
      }
    },
    [connectionState, audioCapture, processSessionMessages]
  );

  /**
   * Disconnect from Gemini Live API.
   * Uses refs for audio stop functions to avoid dependency on objects that change on state updates.
   */
  const disconnect = useCallback(() => {
    // Stop session timer
    if (sessionTimerRef.current) {
      clearInterval(sessionTimerRef.current);
      sessionTimerRef.current = null;
    }

    // Stop audio capture (using ref for stable reference)
    stopCaptureRef.current();

    // Stop audio playback (using ref for stable reference)
    stopPlaybackRef.current();

    // Close session
    if (sessionRef.current) {
      try {
        sessionRef.current.close();
      } catch {
        // Ignore close errors
      }
      sessionRef.current = null;
    }

    clientRef.current = null;

    // Reset state
    setConnectionState('disconnected');
    setActivityState('idle');
    setSessionStartTime(null);
    setSessionDuration(0);
  }, []); // No dependencies - uses refs for stable behavior

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      disconnect();
    };
  }, [disconnect]);

  // Build session state object
  const sessionState: VoiceSessionState = {
    connection: connectionState,
    activity: activityState,
    error,
    sessionStartTime,
    sessionDuration,
  };

  return {
    sessionState,
    transcripts,
    connect,
    disconnect,
    sendAudio,
  };
}
