/**
 * Custom hook for Server-Sent Events subscription.
 * Provides real-time event updates from the backend.
 */

import { useEffect, useRef, useState } from 'react';
import type { DomainEvent } from '../types/api';

interface UseSSEOptions {
  rootId: string | null;
  onEvent: (event: DomainEvent) => void;
  onError?: (error: Event) => void;
}

interface UseSSEReturn {
  isConnected: boolean;
}

export function useSSE({ rootId, onEvent, onError }: UseSSEOptions): UseSSEReturn {
  const [isConnected, setIsConnected] = useState(false);

  // Use refs to avoid recreating the effect when callbacks change
  const onEventRef = useRef(onEvent);
  const onErrorRef = useRef(onError);

  // Update refs on every render (but don't trigger effect re-run)
  onEventRef.current = onEvent;
  onErrorRef.current = onError;

  useEffect(() => {
    // Don't connect if no rootId
    if (!rootId) {
      setIsConnected(false);
      return;
    }

    let eventSource: EventSource | null = null;
    let reconnectTimeout: number | null = null;
    let isMounted = true;

    function connect() {
      if (!isMounted) return;

      eventSource = new EventSource(`/api/events/sse/${rootId}`);

      eventSource.onopen = () => {
        if (isMounted) {
          setIsConnected(true);
        }
      };

      eventSource.onmessage = (event) => {
        if (!isMounted) return;
        try {
          const data = JSON.parse(event.data) as DomainEvent;
          onEventRef.current(data);
        } catch (e) {
          console.error('Failed to parse SSE event:', e);
        }
      };

      eventSource.onerror = (error) => {
        if (!isMounted) return;

        setIsConnected(false);
        onErrorRef.current?.(error);

        // Close the errored connection
        eventSource?.close();
        eventSource = null;

        // Auto-reconnect after 3 seconds
        reconnectTimeout = window.setTimeout(() => {
          if (isMounted) {
            connect();
          }
        }, 3000);
      };
    }

    connect();

    return () => {
      isMounted = false;
      if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
      }
      if (eventSource) {
        eventSource.close();
      }
    };
  }, [rootId]); // Only depend on rootId - callbacks are accessed via refs

  return { isConnected };
}
