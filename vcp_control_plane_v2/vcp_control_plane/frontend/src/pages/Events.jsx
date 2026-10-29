import { useState } from 'react'
import { Header } from '../components/layout/Header.jsx'
import { LiveTrace } from '../components/vcps/LiveTrace.jsx'
import { useWebSocket } from '../hooks/useWebSocket.js'
import './Events.css'

export default function Events() {
  const [vcpFilter, setVcpFilter] = useState('')
  const [groupFilter, setGroupFilter] = useState('')
  const [eventFilter, setEventFilter] = useState('')

  const filters = {
    ...(vcpFilter ? { vcpId: vcpFilter } : {}),
    ...(groupFilter ? { groupId: groupFilter } : {}),
    ...(eventFilter ? { events: eventFilter } : {}),
  }

  const { frames, connected, clear } = useWebSocket(filters, 500)

  const eventTypes = [...new Set(frames.map((f) => f.event))].sort()

  return (
    <div className="page">
      <Header title="Live Event Stream" connected={connected} />
      <div className="events-toolbar">
        <input
          className="toolbar-search"
          placeholder="Filter by VCP ID"
          value={vcpFilter}
          onChange={(e) => setVcpFilter(e.target.value)}
        />
        <input
          className="toolbar-search"
          placeholder="Filter by Group ID"
          value={groupFilter}
          onChange={(e) => setGroupFilter(e.target.value)}
        />
        <input
          className="toolbar-search"
          placeholder="Event types (comma-sep)"
          value={eventFilter}
          onChange={(e) => setEventFilter(e.target.value)}
        />
        <span className="toolbar-count">{frames.length} frames</span>
        {eventTypes.length > 0 && (
          <div className="event-type-pills">
            {eventTypes.map((t) => (
              <span key={t} className="event-pill">{t}</span>
            ))}
          </div>
        )}
      </div>

      <div className="events-trace-wrap">
        <LiveTrace frames={frames} connected={connected} onClear={clear} />
      </div>
    </div>
  )
}
