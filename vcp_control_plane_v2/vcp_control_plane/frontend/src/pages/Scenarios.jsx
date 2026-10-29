import { useState, useEffect } from 'react'
import { Header } from '../components/layout/Header.jsx'
import { scenariosApi } from '../api/scenarios.js'
import { formatDistanceToNow } from 'date-fns'
import { ExecStatusBadge } from '../components/common/Badges.jsx'
import './Scenarios.css'

export default function Scenarios() {
  const [catalogue, setCatalogue] = useState([])
  const [executions, setExecutions] = useState([])
  const [selected, setSelected] = useState(null)
  const [params, setParams] = useState({})
  const [scope, setScope] = useState('fleet')
  const [scopeTarget, setScopeTarget] = useState('')
  const [running, setRunning] = useState(false)
  const [runMsg, setRunMsg] = useState(null)
  const [loadingExec, setLoadingExec] = useState(true)

  async function loadCatalogue() {
    const data = await scenariosApi.list()
    setCatalogue(data)
    if (data.length > 0) {
      setSelected(data[0])
      initParams(data[0])
    }
  }

  async function loadExecutions() {
    setLoadingExec(true)
    try {
      const data = await scenariosApi.listExecutions()
      setExecutions(data.items)
    } catch (_) {}
    finally { setLoadingExec(false) }
  }

  function initParams(scenario) {
    const defaults = {}
    scenario.parameters?.forEach((p) => { defaults[p.name] = p.default ?? '' })
    setParams(defaults)
  }

  function selectScenario(s) {
    setSelected(s)
    initParams(s)
    setRunMsg(null)
  }

  async function runScenario() {
    if (!selected) return
    setRunning(true)
    setRunMsg(null)
    try {
      const body = {
        scenarioId: selected.scenarioId,
        scenarioName: selected.name,
        targetScope: scope,
        parameters: coerceParams(selected, params),
        ...(scope === 'single' ? { targetVcpId: scopeTarget } : {}),
        ...(scope === 'group' ? { targetGroupId: scopeTarget } : {}),
        initiatedBy: 'ui',
      }
      const exec = await scenariosApi.run(body)
      setRunMsg({ ok: true, text: `Started execution ${exec.executionId}` })
      loadExecutions()
    } catch (err) {
      setRunMsg({ ok: false, text: err.message })
    } finally {
      setRunning(false)
    }
  }

  async function cancelExecution(id) {
    try {
      await scenariosApi.cancelExecution(id)
      loadExecutions()
    } catch (err) {
      alert(err.message)
    }
  }

  useEffect(() => {
    loadCatalogue()
    loadExecutions()
    const id = setInterval(loadExecutions, 3_000)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="page">
      <Header title="Scenarios" />
      <div className="scenarios-grid">
        {/* Catalogue */}
        <aside className="scenarios-catalogue">
          <h3 className="cat-heading">Built-in Scenarios</h3>
          {catalogue.map((s) => (
            <button
              key={s.scenarioId}
              className={`cat-item ${selected?.scenarioId === s.scenarioId ? 'cat-item--active' : ''}`}
              onClick={() => selectScenario(s)}
            >
              <span className="cat-name">{s.name}</span>
              <span className="cat-id">{s.scenarioId}</span>
            </button>
          ))}
        </aside>

        {/* Run panel */}
        <main className="scenario-run-panel">
          {selected && (
            <>
              <div className="run-header">
                <h2 className="run-title">{selected.name}</h2>
                <p className="run-desc">{selected.description}</p>
                {selected.aliases?.length > 0 && (
                  <p className="run-alias">Also accepted as: {selected.aliases.join(', ')}</p>
                )}
              </div>

              <div className="run-form">
                <div className="run-scope">
                  <label className="form-label">Target Scope</label>
                  <div className="scope-tabs">
                    {['fleet', 'group', 'single'].map((s) => (
                      <button
                        key={s}
                        className={`scope-tab ${scope === s ? 'scope-tab--active' : ''}`}
                        onClick={() => setScope(s)}
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                </div>

                {(scope === 'single' || scope === 'group') && (
                  <label className="form-label">
                    {scope === 'single' ? 'VCP ID' : 'Group ID'}
                    <input
                      className="form-input"
                      value={scopeTarget}
                      onChange={(e) => setScopeTarget(e.target.value)}
                      placeholder={scope === 'single' ? 'vcp-...' : 'group-...'}
                    />
                  </label>
                )}

                {selected.parameters?.map((p) => (
                  <label key={p.name} className="form-label">
                    {p.name}
                    {p.description && <span className="param-desc">{p.description}</span>}
                    <input
                      className="form-input"
                      value={params[p.name] ?? ''}
                      onChange={(e) =>
                        setParams({ ...params, [p.name]: e.target.value })
                      }
                    />
                  </label>
                ))}

                <button
                  className="btn-primary run-btn"
                  disabled={running}
                  onClick={runScenario}
                >
                  {running ? 'Starting…' : `Run ${selected.name}`}
                </button>

                {runMsg && (
                  <div className={runMsg.ok ? 'form-success' : 'form-error'}>
                    {runMsg.text}
                  </div>
                )}
              </div>
            </>
          )}
        </main>
      </div>

      {/* Executions list */}
      <div className="executions-section">
        <h3 className="section-title" style={{ padding: '0 24px', marginBottom: 12 }}>
          Recent Executions
        </h3>
        {loadingExec ? (
          <div className="page-loading">Loading…</div>
        ) : (
          <div className="exec-table-wrap">
            <table className="profiles-table">
              <thead>
                <tr>
                  <th>Scenario</th>
                  <th>Scope</th>
                  <th>Targets</th>
                  <th>Progress</th>
                  <th>Status</th>
                  <th>Result</th>
                  <th>Started</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {executions.length === 0 && (
                  <tr><td colSpan={8} className="table-empty">No executions yet.</td></tr>
                )}
                {executions.map((e) => (
                  <tr key={e.executionId}>
                    <td className="td-mono">{e.scenarioId}</td>
                    <td>{e.targetScope}</td>
                    <td>{e.targetCount}</td>
                    <td><Progress e={e} /></td>
                    <td><ExecStatusBadge status={e.status} /></td>
                    <td>{e.result ? <span className={`badge ${e.result === 'passed' ? 'badge--green' : e.result === 'partial' ? 'badge--amber' : 'badge--red'}`}>{e.result}</span> : '–'}</td>
                    <td className="audit-ts">
                      {e.startTime
                        ? formatDistanceToNow(new Date(e.startTime), { addSuffix: true })
                        : '–'}
                    </td>
                    <td>
                      {e.status === 'running' && (
                        <button className="btn-danger-sm" onClick={() => cancelExecution(e.executionId)}>
                          Cancel
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

function Progress({ e }) {
  const p = e.progress || {}
  const done = (p.completed || 0) + (p.failed || 0) + (p.cancelled || 0)
  const pct = e.targetCount ? Math.min(100, Math.round((done / e.targetCount) * 100)) : 0
  return (
    <span className="exec-progress" title={JSON.stringify(p)}>
      <span className="exec-progress-bar"><span style={{ width: `${pct}%` }} /></span>
      <span className="exec-progress-txt">
        {done}/{e.targetCount}{p.failed ? ` · ${p.failed} failed` : ''}
      </span>
    </span>
  )
}

function coerceParams(scenario, params) {
  const out = {}
  for (const p of scenario.parameters || []) {
    const v = params[p.name]
    if (v === '' || v === undefined) continue
    out[p.name] = p.type === 'int' ? parseInt(v, 10) : p.type === 'float' ? parseFloat(v) : v
  }
  return out
}
