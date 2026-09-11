'use client';

import { useEffect, useRef, useState } from 'react';
import { API_BASE } from '@/lib/api';

export interface LiveEvent {
  type: string;
  ts?: string;
  [key: string]: unknown;
}

/**
 * WebSocket subscription with bounded exponential backoff.
 *
 * The socket is a push channel for freshness, not the source of truth: pages
 * still render from their SWR data, and an event simply triggers a revalidate.
 */
export function useLiveUpdates(onEvent?: (event: LiveEvent) => void) {
  const [connected, setConnected] = useState(false);
  const [lastEvent, setLastEvent] = useState<LiveEvent | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const attemptsRef = useRef(0);
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;

  useEffect(() => {
    let disposed = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let pingTimer: ReturnType<typeof setInterval> | undefined;

    const url = API_BASE.replace(/^http/, 'ws').replace(/\/api\/v1$/, '') + '/ws';

    const connect = () => {
      if (disposed) return;
      let socket: WebSocket;
      try {
        socket = new WebSocket(url);
      } catch {
        scheduleRetry();
        return;
      }
      socketRef.current = socket;

      socket.onopen = () => {
        if (disposed) return;
        attemptsRef.current = 0;
        setConnected(true);
        pingTimer = setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) socket.send('ping');
        }, 30000);
      };

      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(message.data) as LiveEvent;
          if (event.type === 'pong') return;
          setLastEvent(event);
          handlerRef.current?.(event);
        } catch {
          /* ignore frames that are not JSON */
        }
      };

      socket.onclose = () => {
        setConnected(false);
        if (pingTimer) clearInterval(pingTimer);
        scheduleRetry();
      };

      socket.onerror = () => socket.close();
    };

    const scheduleRetry = () => {
      if (disposed) return;
      attemptsRef.current += 1;
      const delay = Math.min(30000, 1000 * 2 ** Math.min(attemptsRef.current, 5));
      retryTimer = setTimeout(connect, delay);
    };

    connect();
    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
      if (pingTimer) clearInterval(pingTimer);
      socketRef.current?.close();
    };
  }, []);

  return { connected, lastEvent };
}
