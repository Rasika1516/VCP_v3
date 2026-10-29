import { useState } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { ArrowLeft, RefreshCw } from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'
import { StateBadge, ConnBadge, CommandStatusBadge } from '../components/common/Badges.jsx'
import { Header } from '../components/layout/Header.jsx'
import { VcpControls } from '../components/vcps/VcpControls.jsx'
import { LiveTrace } from '../components/vcps/LiveTrace.jsx'
import { useVcp } from '../hooks/useVcp.js'
import { useWebSocket } from '../hooks/useWebSocket.js'
import './VcpDetail.css'

export default function VcpDetail() {
  const { vcpId } = useParams()
  const navigate = useNavigate()
  const { vcp, commands, loading, error, refresh } = useVcp(vcpId)
  const { frames, connected, clear } = useWebSocket({ vcpId })

  if (loading) return <div className="page"><div className="page-loading">Loading…</div></div>
  if (error) return <div className="page"><div className="page-error">{error}</div></div>
  if (!vcp) return <div className="page"><div className="page-loading">VCP not found.</div></div>

  return (
    <div className="page">
      <Header title={vcp.vcpId} connected={connected} />
      <div className="vcp-detail-topbar">
        <button className="back-btn" onClick={() => navigate('/vcps')}>
          <ArrowLeft size={16} /> All VCPs
        </button>
        <button className="icon-btn" onClick={refresh} title="Refresh">
          <RefreshCw size={15} />
        </button>
      </div>

      <div className="vcp-detail-grid">
        {/* Left column: meta + controls */}
        <div className="vcp-detail-left">
          <section className="detail-card">
            <h3 className="detail-card-title">Details</h3>
            <MetaRow label="Charge Point ID" value={vcp.chargePointId} />
            <MetaRow label="Profile" value={vcp.profileId} />
            <MetaRow label="OCPP Version" value={vcp.ocppVersion} />
            <MetaRow
              label="Fleet"
              value={vcp.fleetId ? <Link className="meta-link" to={`/vcps?fleetId=${vcp.fleetId}`}>{vcp.fleetId}</Link> : null}
            />
            <MetaRow label="Worker" value={vcp.workerId} />
            <MetaRow label="Group" value={vcp.groupId} />
            <MetaRow label="Desired State" value={vcp.desiredState} />
            <MetaRow
              label="Actual State"
              value={<StateBadge state={vcp.actualState} />}
            />
            <MetaRow
              label="Connection"
              value={<ConnBadge state={vcp.connectionState} />}
            />
            <MetaRow
              label="Last Seen"
              value={vcp.lastSeen
                ? formatDistanceToNow(new Date(vcp.lastSeen), { addSuffix: true })
                : '–'}
            />
            {vcp.tags?.length > 0 && (
              <MetaRow label="Tags" value={vcp.tags.join(', ')} />
            )}
          </section>

          {Object.keys(vcp.connectorState || {}).length > 0 && (
            <section className="detail-card">
              <h3 className="detail-card-title">Connector State</h3>
              <pre className="json-pre">{JSON.stringify(vcp.connectorState, null, 2)}</pre>
            </section>
          )}

          <VcpControls vcpId={vcpId} onCommandSent={refresh} />
        </div>

        {/* Right column: live trace + command audit */}
        <div className="vcp-detail-right">
          <LiveTrace frames={frames} connected={connected} onClear={clear} />

          <section className="detail-card">
            <h3 className="detail-card-title">Command Audit</h3>
            {commands.length === 0 && (
              <div className="audit-empty">No commands yet.</div>
            )}
            <div className="audit-list">
              {commands.map((c) => <AuditRow key={c.commandId} c={c} />)}
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}

function MetaRow({ label, value }) {
  return (
    <div className="meta-row">
      <span className="meta-label">{label}</span>
      <span className="meta-value">{value ?? '–'}</span>
    </div>
  )
}

function AuditRow({ c }) {
  const [open, setOpen] = useState(false)
  const res = c.result || {}
  const hasDetail = res.error || (res.result && Object.keys(res.result).length > 0)
  return (
    <>
      <div className={`audit-row ${hasDetail ? 'audit-row--clickable' : ''}`} onClick={() => hasDetail && setOpen(!open)}>
        <span className="audit-type">{c.commandType}</span>
        <CommandStatusBadge status={c.status} />
        <span className="audit-ts">
          {c.issuedAt ? formatDistanceToNow(new Date(c.issuedAt), { addSuffix: true }) : ''}
        </span>
      </div>
      {res.error && <div className="audit-error">{res.error}</div>}
      {open && res.result && <pre className="json-pre audit-detail">{JSON.stringify(res.result, null, 2)}</pre>}
    </>
  )
}
