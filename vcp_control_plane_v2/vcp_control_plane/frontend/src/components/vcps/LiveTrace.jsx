import { useEffect, useRef } from 'react'
import { Trash2 } from 'lucide-react'
import './LiveTrace.css'

export function LiveTrace({ frames, connected, onClear }) {
  const bottomRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [frames.length])

  return (
    <div className="live-trace">
      <div className="live-trace-header">
        <span className="live-trace-title">
          Live Event Stream
          <span className={`trace-dot ${connected ? 'trace-dot--live' : ''}`} />
        </span>
        <button className="trace-clear" onClick={onClear} title="Clear">
          <Trash2 size={14} />
        </button>
      </div>
      <div className="trace-body">
        {frames.length === 0 && (
          <div className="trace-empty">No events yet. Waiting for live data…</div>
        )}
        {frames.map((f) => (
          <div key={f._id} className={`trace-frame trace-frame--${f.event}`}>
            <span className="trace-ts">
              {f.ts ? new Date(f.ts).toLocaleTimeString() : ''}
            </span>
            <span className="trace-event">{f.event}</span>
            <span className="trace-vcp">{f.vcpId}</span>
            <span className="trace-payload">
              {JSON.stringify(f.payload)}
            </span>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
