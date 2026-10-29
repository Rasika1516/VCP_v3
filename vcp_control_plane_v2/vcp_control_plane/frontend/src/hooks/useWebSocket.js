import { useEffect, useRef, useState, useCallback } from 'react'
import { buildWsUrl } from '../api/client.js'

/**
 * useWebSocket — connects to the control plane event stream.
 *
 * @param {Object} filters  Optional: { vcpId, groupId, events }
 * @param {number} maxFrames  Max frames kept in the buffer (default 200)
 * @returns {{ frames, connected, clear }}
 */
export function useWebSocket(filters = {}, maxFrames = 200) {
  const [frames, setFrames] = useState([])
  const [connected, setConnected] = useState(false)
  const wsRef = useRef(null)
  const reconnectTimer = useRef(null)

  const connect = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState < 2) return // already open/connecting

    const url = buildWsUrl(filters)
    const ws = new WebSocket(url)
    wsRef.current = ws

    ws.onopen = () => setConnected(true)
    ws.onclose = () => {
      setConnected(false)
      // Reconnect after 3 s
      reconnectTimer.current = setTimeout(connect, 3000)
    }
    ws.onerror = () => ws.close()
    ws.onmessage = (evt) => {
      try {
        const frame = JSON.parse(evt.data)
        if (frame.event === 'heartbeat') return
        setFrames((prev) => {
          const next = [...prev, { ...frame, _id: Date.now() + Math.random() }]
          return next.length > maxFrames ? next.slice(next.length - maxFrames) : next
        })
      } catch (_) {}
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    connect()
    return () => {
      clearTimeout(reconnectTimer.current)
      wsRef.current?.close()
    }
  }, [connect])

  const clear = useCallback(() => setFrames([]), [])

  return { frames, connected, clear }
}
