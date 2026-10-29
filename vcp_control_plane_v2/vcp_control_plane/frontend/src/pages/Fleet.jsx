import { useEffect, useState } from 'react'
import { Header } from '../components/layout/Header.jsx'
import { fleetApi } from '../api/fleet.js'
import { commandsApi, CATEGORY_LABELS } from '../api/commands.js'
import './Fleet.css'

export default function Fleet() {
  const [workers, setWorkers] = useState([])
  const [loading, setLoading] = useState(true)
  const [catalogue, setCatalogue] = useState([])
  const [cmd, setCmd] = useState({ commandType: 'get_state', payload: '{}', scope: 'fleet', target: '' })
  const [sending, setSending] = useState(false)
  const [result, setResult] = useState(null)
  const [err, setErr] = useState(null)

  async function loadWorkers() {
    try {
      setWorkers(await fleetApi.workers())
    } catch (_) {
      // keep last list
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadWorkers()
    const id = setInterval(loadWorkers, 10_000)
    commandsApi.catalogue().then(setCatalogue).catch(() => {})
    return () => clearInterval(id)
  }, [])

  function chooseCommand(type) {
    const def = catalogue.find((c) => c.commandType === type)
    setCmd({ ...cmd, commandType: type, payload: JSON.stringify(def?.payload || {}, null, 1) })
  }

  async function sendBulk() {
    setSending(true)
    setErr(null)
    setResult(null)
    try {
      const payload = JSON.parse(cmd.payload || '{}')
      const body = { commandType: cmd.commandType, payload, issuedBy: 'ui' }
      if (cmd.scope === 'group') body.groupId = cmd.target.trim()
      if (cmd.scope === 'list') body.vcpIds = cmd.target.split(/[\s,]+/).filter(Boolean)
      const res = await fleetApi.bulkCommand(body)
      setResult(res)
    } catch (e) {
      setErr(e.message)
    } finally {
      setSending(false)
    }
  }

  const byCategory = catalogue.reduce((acc, c) => {
    (acc[c.category] = acc[c.category] || []).push(c)
    return acc
  }, {})

  return (
    <div className="page">
      <Header title="Fleet Overview" />
      <div className="fleet-page-grid">
        <section className="fleet-section">
          <h2 className="section-title">Data-Plane Workers</h2>
          {loading ? (
            <p className="page-loading">Loading…</p>
          ) : (
            <div className="workers-table">
              <div className="workers-head workers-head--v2">
                <span>Worker</span>
                <span>Source</span>
                <span>Status</span>
                <span>VCPs</span>
                <span>Connector states</span>
              </div>
              {workers.length === 0 && (
                <div className="workers-empty">
                  No workers yet — deploy a fleet on the VCP Fleets page, or configure VCP_CP_WORKERS.
                </div>
              )}
              {workers.map((w) => (
                <div key={w.workerId} className="workers-row workers-row--v2">
                  <span>
                    <span className="worker-id">{w.workerId}</span>
                    <span className="td-sub td-mono">{w.url || 'no URL'}</span>
                  </span>
                  <span className="td-sub">{w.source}{w.fleetId ? ` · ${w.fleetId}` : ''}</span>
                  <span className={`worker-status ${w.reachable ? 'worker-status--up' : 'worker-status--down'}`}>
                    {w.reachable ? 'Reachable' : w.status === 'online' ? 'Online (bus)' : 'Unreachable'}
                  </span>
                  <span>{w.vcpCount}</span>
                  <span className="worker-states">
                    {Object.entries(w.connectorStateCounts || {})
                      .filter(([, n]) => n > 0)
                      .map(([s, n]) => <span key={s} className="state-chip">{s}: {n}</span>)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="fleet-section">
          <h2 className="section-title">Bulk Command</h2>
          <div className="bulk-form">
            <label className="form-label">
              Command
              <select className="form-select" value={cmd.commandType} onChange={(e) => chooseCommand(e.target.value)}>
                {Object.entries(byCategory).map(([cat, cmds]) => (
                  <optgroup key={cat} label={CATEGORY_LABELS[cat] || cat}>
                    {cmds.map((c) => <option key={c.commandType} value={c.commandType}>{c.label}</option>)}
                  </optgroup>
                ))}
              </select>
            </label>
            <label className="form-label">
              Target
              <span className="scope-tabs">
                {[['fleet', 'Whole fleet'], ['group', 'Group'], ['list', 'VCP list']].map(([k, l]) => (
                  <button key={k} type="button" className={`scope-tab ${cmd.scope === k ? 'scope-tab--active' : ''}`}
                    onClick={() => setCmd({ ...cmd, scope: k })}>{l}</button>
                ))}
              </span>
            </label>
            {cmd.scope !== 'fleet' && (
              <label className="form-label">
                {cmd.scope === 'group' ? 'Group ID' : 'VCP IDs (comma or space separated)'}
                <input className="form-input" value={cmd.target}
                  onChange={(e) => setCmd({ ...cmd, target: e.target.value })} />
              </label>
            )}
            <label className="form-label">
              Payload (JSON)
              <textarea className="form-textarea" rows={4} value={cmd.payload}
                onChange={(e) => setCmd({ ...cmd, payload: e.target.value })} />
            </label>
            <button className="btn-primary" disabled={sending || (cmd.scope !== 'fleet' && !cmd.target.trim())} onClick={sendBulk}>
              {sending ? 'Sending…' : 'Dispatch'}
            </button>
            {err && <div className="form-error">{err}</div>}
            {result && (
              <div className="form-success">
                Dispatched {result.dispatched} command(s) — worker results are recorded in the command audit.
              </div>
            )}
          </div>
        </section>
      </div>
    </div>
  )
}
