import { Header } from '../components/layout/Header.jsx'
import { FleetSummary } from '../components/fleet/FleetSummary.jsx'
import { LiveTrace } from '../components/vcps/LiveTrace.jsx'
import { useFleetSummary } from '../hooks/useFleetSummary.js'
import { useWebSocket } from '../hooks/useWebSocket.js'
import './Dashboard.css'

export default function Dashboard() {
  const { summary, loading } = useFleetSummary(10_000)
  const { frames, connected, clear } = useWebSocket({}, 100)

  return (
    <div className="page">
      <Header title="Dashboard" connected={connected} />
      <div className="dashboard-grid">
        <div className="dashboard-left">
          {loading ? (
            <div className="page-loading">Loading…</div>
          ) : (
            <FleetSummary summary={summary} />
          )}
        </div>
        <div className="dashboard-right">
          <LiveTrace frames={frames} connected={connected} onClear={clear} />
        </div>
      </div>
    </div>
  )
}
