import './FleetSummary.css'

function StatCard({ label, value, accent }) {
  return (
    <div className={`stat-card${accent ? ' stat-card--accent' : ''}`}>
      <span className="stat-value">{value ?? '–'}</span>
      <span className="stat-label">{label}</span>
    </div>
  )
}

export function FleetSummary({ summary }) {
  if (!summary) return <div className="fleet-loading">Loading fleet summary…</div>

  const conn = summary.connectionStateCounts || {}
  const actual = summary.actualStateCounts || {}

  return (
    <div className="fleet-summary">
      <div className="stat-grid">
        <StatCard label="Total VCPs" value={summary.vcpCount} accent />
        <StatCard label="Workers" value={summary.workerCount} />
        <StatCard label="Connected" value={conn.connected ?? 0} />
        <StatCard label="Disconnected" value={conn.disconnected ?? 0} />
      </div>

      <div className="state-section">
        <h3 className="state-heading">Connection States</h3>
        <div className="state-bars">
          {Object.entries(conn).map(([state, count]) => (
            <StateBar key={state} state={state} count={count} total={summary.vcpCount} />
          ))}
        </div>
      </div>

      <div className="state-section">
        <h3 className="state-heading">Operational States</h3>
        <div className="state-bars">
          {Object.entries(actual).map(([state, count]) => (
            <StateBar key={state} state={state} count={count} total={summary.vcpCount} />
          ))}
        </div>
      </div>
    </div>
  )
}

function StateBar({ state, count, total }) {
  const pct = total > 0 ? Math.round((count / total) * 100) : 0
  return (
    <div className="state-bar-row">
      <span className="state-bar-label">{state}</span>
      <div className="state-bar-track">
        <div
          className={`state-bar-fill state-bar-fill--${state}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="state-bar-count">{count}</span>
    </div>
  )
}
