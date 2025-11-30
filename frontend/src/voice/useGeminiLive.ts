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

import { GoogleGenAI, Modality, type LiveServerMessage, type Session } from '@google/genai';
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
 * Callbacks for handling messages from Gemini Live session.
 * Used by both real SDK and test mocks.
 */
export interface GeminiLiveCallbacks {
  onMessage: (message: LiveServerMessage) => void;
  onError: (error: Error) => void;
  onClose: () => void;
}

/**
 * Test seam: allows injecting a mock session factory for integration tests.
 * When set, this factory is called instead of using the real GoogleGenAI SDK.
 *
 * The factory receives the token data and callbacks, and should return a mock Session object.
 * The mock should call the callbacks to simulate Gemini messages.
 *
 * @example
 * // In Playwright test:
 * await page.addInitScript(() => {
 *   window.__TEST_GEMINI_SESSION_FACTORY__ = async (tokenData, callbacks) => {
 *     // Simulate a tool call after 500ms
 *     setTimeout(() => {
 *       callbacks.onMessage({ toolCall: { functionCalls: [...] } });
 *     }, 500);
 *     return mockSession;
 *   };
 * });
 */
declare global {
  interface Window {
    __TEST_GEMINI_SESSION_FACTORY__?: (
      tokenData: EphemeralTokenResponse,
      callbacks: GeminiLiveCallbacks
    ) => Promise<Session>;
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
  const [connectingStatus, setConnectingStatus] = useState<string | undefined>(undefined);

  // Refs for mutable state
  const sessionRef = useRef<Session | null>(null);
  const clientRef = useRef<GoogleGenAI | null>(null);
  const sessionTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const duckingTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Track last transcription for accumulation (speaker, timestamp, entry ID)
  const lastTranscriptRef = useRef<{
    role: 'user' | 'assistant';
    timestamp: number;
    entryId: string;
  } | null>(null);
  // Gap threshold in ms - start new entry if gap exceeds this
  const TRANSCRIPTION_GAP_MS = 2000;

  // Audio hooks
  const audioPlayback = useAudioPlayback();

  // Store audio control functions in refs to avoid dependency issues with disconnect
  const stopCaptureRef = useRef<() => void>(() => {});
  const stopPlaybackRef = useRef<() => void>(() => {});
  const setDuckingRef = useRef<(isDucked: boolean) => void>(() => {});

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
   * Accumulates text for the same speaker, creates new entry on speaker change or time gap.
   */
  const handleTranscription = useCallback((text: string, role: 'user' | 'assistant') => {
    if (!text) {
      return;
    }

    const now = Date.now();
    const last = lastTranscriptRef.current;

    // Check if we should append to existing entry or create new one
    const shouldAppend =
      last !== null && last.role === role && now - last.timestamp < TRANSCRIPTION_GAP_MS;

    if (shouldAppend && last) {
      // Append to existing entry
      setTranscripts((prev) => {
        const updated = [...prev];
        const lastIndex = updated.findIndex((t) => t.id === last.entryId);
        if (lastIndex >= 0) {
          updated[lastIndex] = {
            ...updated[lastIndex],
            text: updated[lastIndex].text + text,
          };
        }
        return updated;
      });
      lastTranscriptRef.current = { role, timestamp: now, entryId: last.entryId };
    } else {
      // Create new entry
      const newId = generateTranscriptId();
      const newEntry: TranscriptEntry = {
        id: newId,
        role,
        text: text,
        timestamp: new Date(),
        isFinal: true,
      };
      setTranscripts((prev) => [...prev, newEntry]);
      lastTranscriptRef.current = { role, timestamp: now, entryId: newId };
    }
  }, []);

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

      // Check if session was closed while awaiting tool calls
      if (!sessionRef.current) {
        return;
      }

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
  setDuckingRef.current = audioCapture.setDucking;

  /**
   * Handle incoming messages from the Gemini session (callback-based API).
   */
  const handleSessionMessage = useCallback(
    (message: LiveServerMessage) => {
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
              handleTranscription(part.text, 'assistant');
            }
          }
        }

        // Handle turn completion
        if (content.turnComplete) {
          setActivityState('listening');
        }

        // Handle interruption
        if (content.interrupted) {
          audioPlayback.stopPlayback();
          setActivityState('listening');
        }

        // Handle input transcription (user speech) - append in real-time
        if (content.inputTranscription?.text) {
          handleTranscription(content.inputTranscription.text, 'user');
        }

        // Handle output transcription (assistant speech) - append in real-time
        if (content.outputTranscription?.text) {
          handleTranscription(content.outputTranscription.text, 'assistant');
        }
      }

      // Handle tool calls
      if (message.toolCall?.functionCalls) {
        const toolCalls: GeminiToolCall[] = message.toolCall.functionCalls.map((fc) => ({
          id: fc.id || generateTranscriptId(),
          name: fc.name || '',
          args: (fc.args as Record<string, unknown>) || {},
        }));
        // Handle tool calls asynchronously
        handleToolCalls(toolCalls).catch((err) => {
          console.error('Error handling tool calls:', err);
        });
      }

      // Handle go away (session ending)
      if (message.goAway) {
        console.warn('Received GoAway from Gemini, session ending');
        // Could implement reconnection here
      }
    },
    [handleAudioResponse, handleTranscription, handleToolCalls, audioPlayback]
  );

  /**
   * Handle session errors (callback-based API).
   */
  const handleSessionError = useCallback((err: Error) => {
    console.error('Gemini session error:', err);
    setError(err.message);
    setConnectionState('error');
  }, []);

  /**
   * Handle session close (callback-based API).
   */
  const handleSessionClose = useCallback(() => {
    // Session was closed, could be normal disconnect or unexpected close
    if (connectionState === 'connected') {
      console.warn('Gemini session closed unexpectedly');
    }
  }, [connectionState]);

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
        setConnectingStatus('Fetching token...');
        setError(null);
        setTranscripts([]);
        lastTranscriptRef.current = null;

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

        setConnectingStatus('Connecting to Gemini...');

        // Create callbacks object for the session
        const callbacks: GeminiLiveCallbacks = {
          onMessage: handleSessionMessage,
          onError: handleSessionError,
          onClose: handleSessionClose,
        };

        let session: Session;

        // Create a promise that resolves when WebSocket is actually open
        // This is necessary because live.connect() returns before the WebSocket is ready
        let resolveOpen: () => void;
        let rejectOpen: (error: Error) => void;
        const openPromise = new Promise<void>((resolve, reject) => {
          resolveOpen = resolve;
          rejectOpen = reject;
        });

        // Check for test seam - allows injecting mock session for integration tests
        if (typeof window !== 'undefined' && window.__TEST_GEMINI_SESSION_FACTORY__) {
          session = await window.__TEST_GEMINI_SESSION_FACTORY__(tokenData, callbacks);
          // For tests, resolve immediately since there's no real WebSocket
          resolveOpen!();
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

          // Create live session with callback-based message handling
          session = await client.live.connect({
            model: tokenData.model,
            callbacks: {
              onopen: () => {
                resolveOpen!();
              },
              onmessage: handleSessionMessage,
              onerror: (e: ErrorEvent) => {
                const error = new Error(e.message || 'WebSocket error');
                rejectOpen!(error);
                handleSessionError(error);
              },
              onclose: (e: CloseEvent) => {
                // Reject openPromise if connection closes before opening
                rejectOpen!(new Error(e.reason || 'Connection closed before opening'));
                handleSessionClose();
              },
            },
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
              // Enable transcription for UI display
              inputAudioTranscription: {},
              outputAudioTranscription: {},
            },
          });
        }
        sessionRef.current = session;

        // Wait for WebSocket to actually be open before starting audio capture
        // This prevents "WebSocket is already in CLOSING or CLOSED state" errors
        setConnectingStatus('Waiting for connection...');
        await openPromise;

        // Start audio capture now that connection is ready
        setConnectingStatus('Starting microphone...');
        await audioCapture.startCapture();

        // Set up session state
        setConnectionState('connected');
        setActivityState('listening');
        setSessionStartTime(Date.now());
        setConnectingStatus(undefined); // Clear connecting status

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
        setConnectingStatus(undefined); // Clear connecting status on error
      }
    },
    [connectionState, audioCapture, handleSessionMessage, handleSessionError, handleSessionClose]
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

  // Toggle mic ducking based on activity state to suppress echo while AI is speaking
  // Uses 200ms delay after AI stops to let room echo die down
  useEffect(() => {
    if (activityState === 'speaking') {
      // AI is speaking - duck mic immediately
      if (duckingTimeoutRef.current) {
        clearTimeout(duckingTimeoutRef.current);
        duckingTimeoutRef.current = null;
      }
      setDuckingRef.current(true);
    } else if (activityState === 'listening') {
      // AI stopped speaking - wait 200ms for room echo to die down before restoring mic
      duckingTimeoutRef.current = setTimeout(() => {
        setDuckingRef.current(false);
        duckingTimeoutRef.current = null;
      }, 200);
    }

    // Cleanup timeout on unmount or state change
    return () => {
      if (duckingTimeoutRef.current) {
        clearTimeout(duckingTimeoutRef.current);
        duckingTimeoutRef.current = null;
      }
    };
  }, [activityState]);

  // Build session state object
  const sessionState: VoiceSessionState = {
    connection: connectionState,
    activity: activityState,
    error,
    sessionStartTime,
    sessionDuration,
    connectingStatus,
  };

  return {
    sessionState,
    transcripts,
    connect,
    disconnect,
    sendAudio,
  };
}
