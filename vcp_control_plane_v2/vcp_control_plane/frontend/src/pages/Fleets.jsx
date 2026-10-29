import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { formatDistanceToNow } from 'date-fns'
import { Plus, Rocket, Square, RotateCw, Trash2, Pencil, FileCode, ScrollText, Info } from 'lucide-react'
import { Header } from '../components/layout/Header.jsx'
import { Modal } from '../components/common/Modal.jsx'
import { FleetStatusBadge } from '../components/common/Badges.jsx'
import { fleetsApi } from '../api/fleets.js'
import './Fleets.css'

const EMPTY_FORM = {
  name: '',
  description: '',
  csmsUrl: 'ws://localhost:9000/ocpp',
  authType: 'none',
  username: '',
  password: '',
  profileId: '',
  ocppVersion: '',
  groupId: '',
  vcpCount: 10,
  idPrefix: 'VCP-',
  chargePointIdPrefix: 'CP-',
  idStart: 1,
  launcher: 'process',
  jitterMin: 0,
  jitterMax: 2000,
  forwardTraces: true,
  logLevel: 'INFO',
}

const ago = (ts) => (ts ? formatDistanceToNow(new Date(ts), { addSuffix: true }) : '–')

export default function Fleets() {
  const [fleets, setFleets] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [editing, setEditing] = useState(null) // null | 'new' | fleet
  const [busy, setBusy] = useState({})
  const [launchers, setLaunchers] = useState([])
  const [profiles, setProfiles] = useState([])

  const load = useCallback(async () => {
    try {
      const data = await fleetsApi.list()
      setFleets(data.items)
      setError(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, 4000)
    fleetsApi.launchers().then(setLaunchers).catch(() => {})
    fleetsApi.profiles().then(setProfiles).catch(() => {})
    return () => clearInterval(id)
  }, [load])

  async function act(fleet, action) {
    if (action === 'delete') {
      const force = ['running', 'deploying'].includes(fleet.status)
      if (!confirm(`Delete fleet "${fleet.name}" and its ${fleet.vcpCount} VCPs?${force ? '\nThe worker will be stopped first.' : ''}`)) return
    }
    if (action === 'stop' && !confirm(`Stop the worker for "${fleet.name}"? All its VCPs disconnect from the CSMS.`)) return
    setBusy((b) => ({ ...b, [fleet.fleetId]: action }))
    try {
      if (action === 'deploy') await fleetsApi.deploy(fleet.fleetId)
      if (action === 'stop') await fleetsApi.stop(fleet.fleetId)
      if (action === 'redeploy') await fleetsApi.redeploy(fleet.fleetId)
      if (action === 'delete') {
        await fleetsApi.remove(fleet.fleetId, ['running', 'deploying'].includes(fleet.status))
        if (selectedId === fleet.fleetId) setSelectedId(null)
      }
      await load()
    } catch (e) {
      alert(e.message)
    } finally {
      setBusy((b) => ({ ...b, [fleet.fleetId]: null }))
    }
  }

  const selected = fleets.find((f) => f.fleetId === selectedId)
  const totals = useMemo(() => ({
    fleets: fleets.length,
    running: fleets.filter((f) => f.status === 'running').length,
    vcps: fleets.reduce((n, f) => n + f.vcpCount, 0),
    connected: fleets.reduce((n, f) => n + f.connectedCount, 0),
  }), [fleets])

  return (
    <div className="page">
      <Header title="VCP Fleets" />
      <div className="fleets-toolbar">
        <div className="fleets-stats">
          <span><b>{totals.fleets}</b> fleets</span>
          <span><b>{totals.running}</b> running</span>
          <span><b>{totals.connected}</b> / {totals.vcps} VCPs connected</span>
        </div>
        <LauncherHint launchers={launchers} />
        <button className="btn-primary" onClick={() => setEditing('new')}>
          <Plus size={15} /> New Fleet
        </button>
      </div>

      {error && <div className="page-error">{error}</div>}

      {loading ? (
        <div className="page-loading">Loading fleets…</div>
      ) : (
        <div className="data-table-wrap">
          <table className="data-table fleets-table">
            <thead>
              <tr>
                <th>Fleet</th>
                <th>Status</th>
                <th>VCPs</th>
                <th>CSMS</th>
                <th>Profile / Group</th>
                <th>Worker</th>
                <th className="th-actions">Actions</th>
              </tr>
            </thead>
            <tbody>
              {fleets.length === 0 && (
                <tr>
                  <td colSpan={7} className="table-empty">
                    No fleets yet. Create one to generate a worker configuration, then deploy it.
                  </td>
                </tr>
              )}
              {fleets.map((f) => {
                const b = busy[f.fleetId]
                const active = ['running', 'deploying', 'stopping'].includes(f.status)
                return (
                  <tr
                    key={f.fleetId}
                    className={`fleet-row ${selectedId === f.fleetId ? 'fleet-row--selected' : ''}`}
                    onClick={() => setSelectedId(selectedId === f.fleetId ? null : f.fleetId)}
                  >
                    <td>
                      <span className="fleet-name">{f.name}</span>
                      <span className="td-sub td-mono">{f.fleetId}</span>
                    </td>
                    <td>
                      <FleetStatusBadge status={f.status} />
                      {f.redeployRequired && <span className="badge badge--amber fleet-pill">redeploy required</span>}
                      {f.status === 'failed' && f.lastError && (
                        <span className="td-sub fleet-err" title={f.lastError}>{f.lastError}</span>
                      )}
                    </td>
                    <td>
                      <span className="td-mono">{f.connectedCount}/{f.vcpCount}</span>
                      <span className="td-sub">connected</span>
                    </td>
                    <td className="td-mono td-ellipsis" title={f.csmsUrl}>{f.csmsUrl}</td>
                    <td>
                      <span className="td-mono">{f.profileId}</span>
                      <span className="td-sub">{f.groupId || 'no group'}</span>
                    </td>
                    <td>
                      <span className="td-mono">{f.workerId}</span>
                      <span className="td-sub">{f.launcher} · :{f.apiPort}</span>
                    </td>
                    <td className="td-actions" onClick={(e) => e.stopPropagation()}>
                      <div className="actions-wrap">
                      {!active && (
                        <button className="btn-sm btn-sm--go" disabled={!!b} onClick={() => act(f, 'deploy')}>
                          <Rocket size={13} /> {b === 'deploy' ? 'Deploying…' : 'Deploy'}
                        </button>
                      )}
                      {active && (
                        <button className={`btn-sm ${f.redeployRequired ? 'btn-sm--warn' : ''}`} disabled={!!b}
                          onClick={() => act(f, 'redeploy')}>
                          <RotateCw size={13} /> {b === 'redeploy' ? 'Redeploying…' : 'Redeploy'}
                        </button>
                      )}
                      {active && (
                        <button className="btn-sm" disabled={!!b} onClick={() => act(f, 'stop')}>
                          <Square size={12} /> {b === 'stop' ? 'Stopping…' : 'Stop'}
                        </button>
                      )}
                      <button className="btn-sm" disabled={!!b} onClick={() => setEditing(f)} title="Edit">
                        <Pencil size={13} />
                      </button>
                      <button className="btn-danger-sm" disabled={!!b} onClick={() => act(f, 'delete')} title="Delete">
                        <Trash2 size={13} />
                      </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {selected && <FleetDetail fleet={selected} />}

      {editing && (
        <FleetForm
          fleet={editing === 'new' ? null : editing}
          profiles={profiles}
          launchers={launchers}
          onClose={() => setEditing(null)}
          onSaved={(f) => { setEditing(null); setSelectedId(f.fleetId); load() }}
        />
      )}
    </div>
  )
}

function LauncherHint({ launchers }) {
  if (!launchers.length) return <span className="fleets-launchers" />
  return (
    <span className="fleets-launchers">
      {launchers.map((l) => (
        <span key={l.name} className={`launcher-chip ${l.available ? 'launcher-chip--ok' : ''}`} title={l.detail}>
          {l.name}: {l.available ? 'available' : 'unavailable'}
        </span>
      ))}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Detail panel: overview · generated worker.yaml · logs
// ---------------------------------------------------------------------------

function FleetDetail({ fleet }) {
  const [tab, setTab] = useState('overview')
  const [config, setConfig] = useState(null)
  const [logs, setLogs] = useState('')
  const [err, setErr] = useState(null)

  useEffect(() => { setTab('overview'); setConfig(null); setLogs(''); setErr(null) }, [fleet.fleetId])

  useEffect(() => {
    let cancelled = false
    async function fetchTab() {
      try {
        if (tab === 'config') {
          const c = await fleetsApi.configYaml(fleet.fleetId)
          if (!cancelled) setConfig(c)
        }
        if (tab === 'logs') {
          const t = await fleetsApi.logs(fleet.fleetId, 300)
          if (!cancelled) setLogs(t)
        }
        if (!cancelled) setErr(null)
      } catch (e) {
        if (!cancelled) setErr(e.message)
      }
    }
    fetchTab()
    const id = tab === 'logs' ? setInterval(fetchTab, 3000) : null
    return () => { cancelled = true; if (id) clearInterval(id) }
  }, [tab, fleet.fleetId, fleet.configVersion])

  const opts = fleet.options || {}
  return (
    <section className="fleet-detail">
      <div className="fleet-detail-head">
        <div>
          <h2 className="fleet-detail-title">{fleet.name}</h2>
          {fleet.description && <p className="fleet-detail-desc">{fleet.description}</p>}
        </div>
        <div className="fleet-tabs">
          {[['overview', Info, 'Overview'], ['config', FileCode, 'Worker config'], ['logs', ScrollText, 'Logs']].map(
            ([key, Icon, label]) => (
              <button key={key} className={`fleet-tab ${tab === key ? 'fleet-tab--active' : ''}`} onClick={() => setTab(key)}>
                <Icon size={14} /> {label}
              </button>
            ),
          )}
        </div>
      </div>

      {err && <div className="form-error">{err}</div>}

      {tab === 'overview' && (
        <div className="fleet-overview">
          <dl className="kv">
            <dt>Status</dt><dd><FleetStatusBadge status={fleet.status} /></dd>
            <dt>VCPs</dt>
            <dd>
              {fleet.vcpCount} ({fleet.idPrefix}{String(fleet.idStart).padStart(5, '0')}…) ·{' '}
              <Link to={`/vcps?fleetId=${fleet.fleetId}`}>open VCP list →</Link>
            </dd>
            <dt>Charge point IDs</dt><dd className="td-mono">{fleet.chargePointIdPrefix}{String(fleet.idStart).padStart(5, '0')}…</dd>
            <dt>CSMS</dt><dd className="td-mono">{fleet.csmsUrl}</dd>
            <dt>CSMS auth</dt><dd>{fleet.csmsAuthType}{fleet.csmsUsername ? ` (${fleet.csmsUsername})` : ''}</dd>
            <dt>Profile</dt><dd className="td-mono">{fleet.profileId}{fleet.ocppVersion ? ` · OCPP ${fleet.ocppVersion}` : ''}</dd>
            <dt>Group</dt><dd className="td-mono">{fleet.groupId || '–'}</dd>
          </dl>
          <dl className="kv">
            <dt>Worker ID</dt><dd className="td-mono">{fleet.workerId}</dd>
            <dt>Launcher</dt><dd>{fleet.launcher}</dd>
            <dt>Worker API</dt><dd className="td-mono">{fleet.workerUrl || `port ${fleet.apiPort}`}</dd>
            <dt>Metrics</dt><dd className="td-mono">:{fleet.metricsPort}/metrics</dd>
            <dt>Runtime</dt><dd className="td-mono">{Object.entries(fleet.runtime || {}).filter(([k]) => k !== 'log').map(([k, v]) => `${k}=${v}`).join(' ') || '–'}</dd>
            <dt>Config</dt><dd>v{fleet.configVersion}{fleet.deployedConfigVersion ? ` (deployed v${fleet.deployedConfigVersion})` : ' (never deployed)'}</dd>
            <dt>Deployed</dt><dd>{ago(fleet.deployedAt)}</dd>
            <dt>Last seen</dt><dd>{ago(fleet.lastSeen)}</dd>
            <dt>Options</dt>
            <dd>boot jitter {(opts.boot_jitter_ms || [0, 2000]).join('–')} ms · traces {opts.forward_traces_to_events === false ? 'off' : 'on'} · log {opts.log_level || 'INFO'}</dd>
          </dl>
          {fleet.lastError && <div className="form-error fleet-detail-error">{fleet.lastError}</div>}
        </div>
      )}

      {tab === 'config' && (
        <div>
          <p className="fleet-note">
            Generated <code>worker.yaml</code> for this fleet (secrets masked). Written to{' '}
            <code>{fleet.configPath || `<fleet_config_dir>/${fleet.fleetId}/worker.yaml`}</code> on deploy.
          </p>
          <pre className="code-pre">{config ?? 'Loading…'}</pre>
        </div>
      )}

      {tab === 'logs' && (
        <pre className="code-pre code-pre--logs">{logs || 'No worker output yet.'}</pre>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// Create / edit form
// ---------------------------------------------------------------------------

function FleetForm({ fleet, profiles, launchers, onClose, onSaved }) {
  const isNew = !fleet
  const [f, setF] = useState(() => (isNew ? { ...EMPTY_FORM, profileId: profiles[0]?.profileId || '' } : {
    ...EMPTY_FORM,
    name: fleet.name,
    description: fleet.description,
    csmsUrl: fleet.csmsUrl,
    authType: fleet.csmsAuthType || 'none',
    username: fleet.csmsUsername || '',
    password: '',
    profileId: fleet.profileId,
    ocppVersion: fleet.ocppVersion || '',
    groupId: fleet.groupId || '',
    vcpCount: fleet.vcpCount,
    idPrefix: fleet.idPrefix,
    chargePointIdPrefix: fleet.chargePointIdPrefix,
    idStart: fleet.idStart,
    launcher: fleet.launcher,
    jitterMin: (fleet.options?.boot_jitter_ms || [0, 2000])[0],
    jitterMax: (fleet.options?.boot_jitter_ms || [0, 2000])[1],
    forwardTraces: fleet.options?.forward_traces_to_events !== false,
    logLevel: fleet.options?.log_level || 'INFO',
  }))
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState(null)
  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value })

  async function save() {
    setSaving(true)
    setErr(null)
    const body = {
      name: f.name.trim(),
      description: f.description,
      csmsUrl: f.csmsUrl.trim(),
      csmsAuth: {
        type: f.authType,
        ...(f.authType === 'basic' ? { username: f.username, ...(f.password ? { password: f.password } : {}) } : {}),
      },
      profileId: f.profileId.trim(),
      ocppVersion: f.ocppVersion || null,
      groupId: f.groupId.trim() || null,
      vcpCount: Number(f.vcpCount),
      launcher: f.launcher,
      options: {
        bootJitterMs: [Number(f.jitterMin), Number(f.jitterMax)],
        forwardTracesToEvents: f.forwardTraces,
        logLevel: f.logLevel,
      },
    }
    if (isNew) {
      Object.assign(body, {
        idPrefix: f.idPrefix,
        chargePointIdPrefix: f.chargePointIdPrefix,
        idStart: Number(f.idStart),
      })
    }
    try {
      const saved = isNew ? await fleetsApi.create(body) : await fleetsApi.patch(fleet.fleetId, body)
      onSaved(saved)
    } catch (e) {
      setErr(e.message)
    } finally {
      setSaving(false)
    }
  }

  const running = fleet && ['running', 'deploying'].includes(fleet.status)
  const first = `${f.idPrefix}${String(f.idStart).padStart(5, '0')}`
  const last = `${f.idPrefix}${String(Number(f.idStart) + Math.max(1, Number(f.vcpCount)) - 1).padStart(5, '0')}`

  return (
    <Modal
      title={isNew ? 'New VCP Fleet' : `Edit ${fleet.name}`}
      onClose={onClose}
      width={680}
      footer={(
        <>
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={saving || !f.name || !f.csmsUrl || !f.profileId} onClick={save}>
            {saving ? 'Saving…' : isNew ? 'Create fleet' : 'Save changes'}
          </button>
        </>
      )}
    >
      {running && (
        <div className="form-info">This fleet is running. Changes are saved now and applied when you redeploy it.</div>
      )}
      <div className="fieldset-title">Fleet</div>
      <div className="form-row">
        <label className="form-label">Name
          <input className="form-input" value={f.name} onChange={set('name')} placeholder="Lab A — AC wallboxes" autoFocus />
        </label>
        <label className="form-label">Group ID <span className="form-hint">for group-scoped commands &amp; scenarios</span>
          <input className="form-input" value={f.groupId} onChange={set('groupId')} placeholder="group-a" />
        </label>
      </div>
      <label className="form-label">Description
        <input className="form-input" value={f.description} onChange={set('description')} />
      </label>

      <div className="fieldset-title">CSMS under test</div>
      <label className="form-label">CSMS WebSocket URL <span className="form-hint">the charge point ID is appended: …/ocpp/CP-00001</span>
        <input className="form-input td-mono" value={f.csmsUrl} onChange={set('csmsUrl')} placeholder="wss://csms.example.com/ocpp" />
      </label>
      <div className="form-row--3 form-row">
        <label className="form-label">Auth
          <select className="form-select" value={f.authType} onChange={set('authType')}>
            <option value="none">none</option>
            <option value="basic">HTTP Basic</option>
          </select>
        </label>
        {f.authType === 'basic' && (
          <>
            <label className="form-label">Username
              <input className="form-input" value={f.username} onChange={set('username')} />
            </label>
            <label className="form-label">Password {!isNew && <span className="form-hint">blank = unchanged</span>}
              <input className="form-input" type="password" value={f.password} onChange={set('password')} />
            </label>
          </>
        )}
      </div>

      <div className="fieldset-title">VCPs</div>
      <div className="form-row--3 form-row">
        <label className="form-label">Profile
          <input className="form-input td-mono" list="fleet-profiles" value={f.profileId} onChange={set('profileId')} />
          <datalist id="fleet-profiles">
            {profiles.map((p) => <option key={p.profileId} value={p.profileId}>{p.label}</option>)}
          </datalist>
        </label>
        <label className="form-label">OCPP version
          <select className="form-select" value={f.ocppVersion} onChange={set('ocppVersion')}>
            <option value="">from profile</option>
            <option value="1.6J">1.6J</option>
            <option value="2.0.1">2.0.1</option>
          </select>
        </label>
        <label className="form-label">Number of VCPs
          <input className="form-input" type="number" min={1} max={5000} value={f.vcpCount} onChange={set('vcpCount')} />
        </label>
      </div>
      <div className="form-row--3 form-row">
        <label className="form-label">VCP ID prefix
          <input className="form-input td-mono" value={f.idPrefix} onChange={set('idPrefix')} disabled={!isNew} />
        </label>
        <label className="form-label">Charge point ID prefix
          <input className="form-input td-mono" value={f.chargePointIdPrefix} onChange={set('chargePointIdPrefix')} disabled={!isNew} />
        </label>
        <label className="form-label">Start index
          <input className="form-input" type="number" min={0} value={f.idStart} onChange={set('idStart')} disabled={!isNew} />
        </label>
      </div>
      <div className="form-hint">VCP IDs {first} … {last}</div>

      <div className="fieldset-title">Worker</div>
      <div className="form-row--3 form-row">
        <label className="form-label">Launcher
          <select className="form-select" value={f.launcher} onChange={set('launcher')} disabled={running}>
            {(launchers.length ? launchers : [{ name: 'process' }, { name: 'docker' }]).map((l) => (
              <option key={l.name} value={l.name}>{l.name}{l.available === false ? ' (unavailable)' : ''}</option>
            ))}
          </select>
        </label>
        <label className="form-label">Boot jitter (ms)
          <span className="jitter-pair">
            <input className="form-input" type="number" min={0} value={f.jitterMin} onChange={set('jitterMin')} />
            <input className="form-input" type="number" min={0} value={f.jitterMax} onChange={set('jitterMax')} />
          </span>
        </label>
        <label className="form-label">Log level
          <select className="form-select" value={f.logLevel} onChange={set('logLevel')}>
            {['DEBUG', 'INFO', 'WARNING', 'ERROR'].map((l) => <option key={l}>{l}</option>)}
          </select>
        </label>
      </div>
      <label className="checkbox-row">
        <input type="checkbox" checked={f.forwardTraces} onChange={set('forwardTraces')} />
        Forward OCPP traces to Live Events <span className="form-hint">(turn off for very large fleets)</span>
      </label>

      {err && <div className="form-error">{err}</div>}
    </Modal>
  )
}
