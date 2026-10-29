/**
 * Base API client — wraps fetch with auth headers and base URL.
 */

const BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api/v1'
const TOKEN = import.meta.env.VITE_API_TOKEN || ''

export class ApiError extends Error {
  constructor(status, message, body) {
    super(message)
    this.status = status
    this.body = body
  }
}

async function request(method, path, body = undefined, params = undefined, asText = false) {
  const url = new URL(BASE_URL + path, window.location.origin)
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null) url.searchParams.set(k, v)
    })
  }

  const headers = {
    'Content-Type': 'application/json',
    ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
  }

  const res = await fetch(url.toString(), {
    method,
    headers,
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  })

  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const json = await res.json()
      detail = json.detail || JSON.stringify(json)
    } catch (_) {}
    throw new ApiError(res.status, detail, null)
  }

  if (res.status === 204) return null
  return asText ? res.text() : res.json()
}

export const api = {
  get: (path, params) => request('GET', path, undefined, params),
  getText: (path, params) => request('GET', path, undefined, params, true),
  post: (path, body) => request('POST', path, body),
  patch: (path, body) => request('PATCH', path, body),
  delete: (path) => request('DELETE', path),
}

/**
 * Build a WebSocket URL for the events endpoint.
 * Passes the API token as a query param (WS clients can't set headers).
 */
export function buildWsUrl(params = {}) {
  const wsBase = import.meta.env.VITE_WS_BASE_URL || BASE_URL.replace(/^http/, 'ws')
  const url = new URL(wsBase + '/events/ws', window.location.origin.replace(/^http/, 'ws'))
  if (TOKEN) url.searchParams.set('token', TOKEN)
  Object.entries(params).forEach(([k, v]) => {
    if (v) url.searchParams.set(k, v)
  })
  return url.toString()
}
