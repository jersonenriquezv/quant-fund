"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { fetchApi, wsUrl, type WSMessage } from "./api";

export function usePolling<T>(path: string, intervalMs: number = 5000): {
  data: T | null;
  error: string | null;
  loading: boolean;
} {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;

    const poll = async () => {
      try {
        const result = await fetchApi<T>(path);
        if (mounted) {
          setData(result);
          setError(null);
        }
      } catch (e) {
        if (mounted) setError(String(e));
      } finally {
        if (mounted) setLoading(false);
      }
    };

    poll();
    const id = setInterval(poll, intervalMs);
    return () => {
      mounted = false;
      clearInterval(id);
    };
  }, [path, intervalMs]);

  return { data, error, loading };
}

export function useWebSocket(): WSMessage | null {
  const [msg, setMsg] = useState<WSMessage | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const connect = useCallback(() => {
    const ws = new WebSocket(wsUrl());
    wsRef.current = ws;

    ws.onmessage = (e) => {
      try {
        setMsg(JSON.parse(e.data));
      } catch {}
    };

    ws.onclose = () => {
      setTimeout(connect, 3000);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, []);

  useEffect(() => {
    connect();
    return () => {
      wsRef.current?.close();
    };
  }, [connect]);

  return msg;
}
