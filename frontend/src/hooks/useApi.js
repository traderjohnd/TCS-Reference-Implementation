import { useState, useEffect, useCallback } from 'react';

const API_BASE = '/v2';

function getHeaders() {
  const token = localStorage.getItem('tcs_token');
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  return headers;
}

// Structured API error. `detail` carries the backend's error payload
// verbatim (object for structured errors like protected_metadata_keys,
// array for ordinary FastAPI validation 422s) so callers can
// discriminate instead of string-matching a flattened message.
export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

function summarizeDetail(detail, fallback) {
  if (typeof detail === 'string' && detail) return detail;
  if (detail && !Array.isArray(detail) && typeof detail === 'object') {
    if (typeof detail.message === 'string' && detail.message) {
      return detail.message;
    }
    if (typeof detail.error === 'string' && detail.error) return detail.error;
  }
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0];
    const loc = Array.isArray(first?.loc) ? first.loc.join('.') : '';
    if (first?.msg) return loc ? `${loc}: ${first.msg}` : String(first.msg);
  }
  return fallback;
}

export async function apiFetch(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: getHeaders(),
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(
      summarizeDetail(err?.detail, res.statusText),
      res.status,
      err?.detail,
    );
  }
  return res.json();
}

export async function apiPost(path, body) {
  return apiFetch(path, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export function useApi(path, deps = []) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const refetch = useCallback(() => {
    setLoading(true);
    setError(null);
    apiFetch(path)
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [path]);

  useEffect(() => {
    refetch();
  }, [refetch, ...deps]);

  return { data, loading, error, refetch };
}

export function usePolling(path, intervalMs = 5000) {
  const { data, loading, error, refetch } = useApi(path);

  useEffect(() => {
    const id = setInterval(refetch, intervalMs);
    return () => clearInterval(id);
  }, [refetch, intervalMs]);

  return { data, loading, error, refetch };
}
