/**
 * Custom hook for Server-Sent Events subscription for execution summary updates.
 * Provides real-time cost, timing, and node count updates from the backend.
 */

import { useEffect, useRef, useState, useCallback } from 'react';
import type { ExecutionSummary } from '../types/api';

interface UseSummarySSEOptions {
  /** Root agent ID to subscribe to. */
  rootId: string | null;
  /** Callback when summary is updated. */
  onSummary?: (summary: ExecutionSummary) => void;
  /** Callback when an error occurs. */
  onError?: (error: Event) => void;
  /** Auto-reconnect delay in milliseconds (default: 3000). */
  reconnectDelay?: number;
}

interface UseSummarySSEReturn {
  /** Current execution summary (null if not yet received). */
  summary: ExecutionSummary | null;
  /** Whether the SSE connection is active. */
  isConnected: boolean;
  /** Any error that occurred. */
  error: string | null;
  /** Manually disconnect from the stream. */
  disconnect: () => void;
  /** Manually reconnect to the stream. */
  reconnect: () => void;
}

export function useSummarySSE({
  rootId,
  onSummary,
  onError,
  reconnectDelay = 3000,
}: UseSummarySSEOptions): UseSummarySSEReturn {
  const [summary, setSummary] = useState<ExecutionSummary | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Use refs to avoid recreating the effect when callbacks change
  const onSummaryRef = useRef(onSummary);
  const onErrorRef = useRef(onError);
  const eventSourceRef = useRef<EventSource | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);
  const isMountedRef = useRef(true);

  // Update refs on every render
  onSummaryRef.current = onSummary;
  onErrorRef.current = onError;

  const disconnect = useCallback(() => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    setIsConnected(false);
  }, []);

  const connect = useCallback(() => {
    if (!rootId || !isMountedRef.current) return;

    // Close any existing connection
    disconnect();

    const eventSource = new EventSource(`/api/events/sse/${rootId}/summary`);
    eventSourceRef.current = eventSource;

    eventSource.onopen = () => {
      if (isMountedRef.current) {
        setIsConnected(true);
        setError(null);
      }
    };

    // Listen for 'summary' events
    eventSource.addEventListener('summary', (event) => {
      if (!isMountedRef.current) return;
      try {
        const data = JSON.parse(event.data) as ExecutionSummary;
        setSummary(data);
        onSummaryRef.current?.(data);
      } catch (e) {
        console.error('Failed to parse summary SSE event:', e);
        setError('Failed to parse summary data');
      }
    });

    // Handle errors
    eventSource.addEventListener('error', (event) => {
      if (!isMountedRef.current) return;

      const errorEvent = event as MessageEvent;
      const errorMessage = errorEvent.data ?? 'Connection error';
      setError(errorMessage);
      setIsConnected(false);
      onErrorRef.current?.(event);

      // Close the errored connection
      eventSource.close();
      eventSourceRef.current = null;

      // Auto-reconnect
      reconnectTimeoutRef.current = window.setTimeout(() => {
        if (isMountedRef.current) {
          connect();
        }
      }, reconnectDelay);
    });

    // Handle generic SSE errors (connection lost, etc.)
    eventSource.onerror = (event) => {
      if (!isMountedRef.current) return;

      setIsConnected(false);
      setError('Connection lost');
      onErrorRef.current?.(event);

      // Close and schedule reconnect
      eventSource.close();
      eventSourceRef.current = null;

      reconnectTimeoutRef.current = window.setTimeout(() => {
        if (isMountedRef.current) {
          connect();
        }
      }, reconnectDelay);
    };
  }, [rootId, reconnectDelay, disconnect]);

  const reconnect = useCallback(() => {
    connect();
  }, [connect]);

  useEffect(() => {
    isMountedRef.current = true;

    if (rootId) {
      connect();
    } else {
      setSummary(null);
      setIsConnected(false);
      setError(null);
    }

    return () => {
      isMountedRef.current = false;
      disconnect();
    };
  }, [rootId, connect, disconnect]);

  return { summary, isConnected, error, disconnect, reconnect };
}
