import { useEffect, useMemo, useState } from 'react'
import { vcpsApi } from '../../api/vcps.js'
import { commandsApi, CATEGORY_LABELS } from '../../api/commands.js'
import { scenariosApi } from '../../api/scenarios.js'
import './VcpControls.css'

/**
 * Per-VCP command panel built from the worker command catalogue (GET /commands).
 * Commands with options (reset type, availability, fault type) or a scenario
 * get an inline selector; everything else is a one-click button.
 */
export function VcpControls({ vcpId, onCommandSent }) {
  const [catalogue, setCatalogue] = useState([])
  const [scenarios, setScenarios] = useState([])
  const [choice, setChoice] = useState({})          // commandType → {field: value}
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [sent, setSent] = useState(null)

  useEffect(() => {
    commandsApi.catalogue().then(setCatalogue).catch((e) => setError(e.message))
    scenariosApi.list().then(setScenarios).catch(() => {})
  }, [])

  const groups = useMemo(() => {
    const g = {}
    catalogue
      .filter((c) => c.commandType !== 'get_state')
      .forEach((c) => { (g[c.category] = g[c.category] || []).push(c) })
    return Object.entries(g)
  }, [catalogue])

  async function send(cmd) {
    setBusy(cmd.commandType)
    setError(null)
    setSent(null)
    const payload = { ...cmd.payload, ...(choice[cmd.commandType] || {}) }
    if (cmd.commandType === 'run_scenario' && !choice.run_scenario?.scenario_id && scenarios[0]) {
      payload.scenario_id = scenarios[0].scenarioId
    }
    try {
      await vcpsApi.sendCommand(vcpId, { commandType: cmd.commandType, payload, issuedBy: 'ui' })
      setSent(cmd.label)
      onCommandSent?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(null)
    }
  }

  const pick = (ct, field) => (e) =>
    setChoice({ ...choice, [ct]: { ...(choice[ct] || {}), [field]: e.target.value } })

  return (
    <div className="vcp-controls">
      <h3 className="vcp-controls-heading">Commands</h3>
      {error && <div className="vcp-controls-error">{error}</div>}
      {sent && <div className="vcp-controls-ok">“{sent}” dispatched — see Command Audit for the worker's result.</div>}
      {groups.map(([category, cmds]) => (
        <div key={category} className="ctrl-group">
          <div className="ctrl-group-title">{CATEGORY_LABELS[category] || category}</div>
          <div className="vcp-controls-grid">
            {cmds.map((cmd) => {
              const optField = cmd.options ? Object.keys(cmd.options)[0] : null
              const isScenario = cmd.commandType === 'run_scenario'
              return (
                <div key={cmd.commandType} className={`ctrl-cell ${optField || isScenario ? 'ctrl-cell--wide' : ''}`}>
                  {optField && (
                    <select
                      className="ctrl-select"
                      value={choice[cmd.commandType]?.[optField] ?? cmd.payload[optField]}
                      onChange={pick(cmd.commandType, optField)}
                    >
                      {cmd.options[optField].map((o) => <option key={o} value={o}>{o}</option>)}
                    </select>
                  )}
                  {isScenario && (
                    <select
                      className="ctrl-select"
                      value={choice.run_scenario?.scenario_id ?? scenarios[0]?.scenarioId ?? ''}
                      onChange={pick('run_scenario', 'scenario_id')}
                    >
                      {scenarios.map((s) => <option key={s.scenarioId} value={s.scenarioId}>{s.name}</option>)}
                    </select>
                  )}
                  <button
                    className="ctrl-btn"
                    disabled={busy !== null}
                    onClick={() => send(cmd)}
                    title={JSON.stringify({ ...cmd.payload, ...(choice[cmd.commandType] || {}) })}
                  >
                    {busy === cmd.commandType ? 'Sending…' : cmd.label}
                  </button>
                </div>
              )
            })}
          </div>
        </div>
      ))}
    </div>
  )
}
