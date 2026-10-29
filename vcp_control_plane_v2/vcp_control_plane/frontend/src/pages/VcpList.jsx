import { useState, useEffect } from 'react'
import { useSearchParams, Link } from 'react-router-dom'
import { Plus } from 'lucide-react'
import { Header } from '../components/layout/Header.jsx'
import { VcpCard } from '../components/vcps/VcpCard.jsx'
import { Modal } from '../components/common/Modal.jsx'
import { vcpsApi } from '../api/vcps.js'
import { fleetsApi } from '../api/fleets.js'
import './VcpList.css'

export default function VcpList() {
  const [params, setParams] = useSearchParams()
  const [vcps, setVcps] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [search, setSearch] = useState('')
  const [groupFilter, setGroupFilter] = useState('')
  const [stateFilter, setStateFilter] = useState('')
  const [fleets, setFleets] = useState([])
  const [registering, setRegistering] = useState(false)
  const fleetFilter = params.get('fleetId') || ''

  const PAGE_SIZE = 48

  async function load(p = page) {
    setLoading(true)
    try {
      const data = await vcpsApi.list({
        page: p,
        pageSize: PAGE_SIZE,
        ...(groupFilter ? { groupId: groupFilter } : {}),
        ...(fleetFilter ? { fleetId: fleetFilter } : {}),
        ...(stateFilter ? { actualState: stateFilter } : {}),
      })
      setVcps(data.items)
      setTotal(data.total)
      setError(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fleetsApi.list().then((d) => setFleets(d.items)).catch(() => {}) }, [])
  useEffect(() => { setPage(1); load(1) }, [groupFilter, fleetFilter, stateFilter]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { load(page) }, [page]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const id = setInterval(() => load(page), 5000)
    return () => clearInterval(id)
  }, [page, groupFilter, fleetFilter, stateFilter]) // eslint-disable-line react-hooks/exhaustive-deps

  const filtered = search
    ? vcps.filter(
        (v) =>
          v.vcpId.includes(search) ||
          v.chargePointId?.includes(search) ||
          v.groupId?.includes(search),
      )
    : vcps

  const totalPages = Math.ceil(total / PAGE_SIZE)

  return (
    <div className="page">
      <Header title="VCP Instances" />
      <div className="vcp-list-toolbar">
        <input
          className="toolbar-search"
          placeholder="Filter by ID, CP ID, group…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          className="toolbar-search toolbar-select"
          value={fleetFilter}
          onChange={(e) => setParams(e.target.value ? { fleetId: e.target.value } : {})}
        >
          <option value="">All fleets</option>
          {fleets.map((f) => <option key={f.fleetId} value={f.fleetId}>{f.name}</option>)}
        </select>
        <select className="toolbar-search toolbar-select" value={stateFilter} onChange={(e) => setStateFilter(e.target.value)}>
          <option value="">All states</option>
          {['idle', 'charging', 'preparing', 'faulted', 'unavailable', 'offline', 'unknown'].map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
        <input
          className="toolbar-search"
          placeholder="Group ID filter"
          value={groupFilter}
          onChange={(e) => setGroupFilter(e.target.value)}
        />
        <span className="toolbar-count">{total} VCPs</span>
        <button className="btn-primary" onClick={() => setRegistering(true)}>
          <Plus size={15} /> Register VCP
        </button>
      </div>

      {error && <div className="page-error">{error}</div>}

      {loading && vcps.length === 0 ? (
        <div className="page-loading">Loading VCPs…</div>
      ) : (
        <div className="vcp-grid">
          {filtered.map((vcp) => (
            <VcpCard key={vcp.vcpId} vcp={vcp} />
          ))}
          {filtered.length === 0 && (
            <div className="vcp-empty">
              No VCPs found.{' '}
              {fleets.length === 0 && <>Create a fleet on the <Link to="/fleets">VCP Fleets</Link> page first.</>}
            </div>
          )}
        </div>
      )}

      {totalPages > 1 && (
        <div className="pagination">
          <button disabled={page === 1} onClick={() => setPage(page - 1)}>←</button>
          <span>Page {page} of {totalPages}</span>
          <button disabled={page === totalPages} onClick={() => setPage(page + 1)}>→</button>
        </div>
      )}

      {registering && (
        <RegisterVcp
          fleets={fleets}
          defaultFleet={fleetFilter}
          onClose={() => setRegistering(false)}
          onDone={() => { load(page); fleetsApi.list().then((d) => setFleets(d.items)).catch(() => {}) }}
        />
      )}
    </div>
  )
}

function RegisterVcp({ fleets, defaultFleet, onClose, onDone }) {
  const [form, setForm] = useState({
    fleetId: defaultFleet || fleets[0]?.fleetId || '',
    vcpId: '',
    chargePointId: '',
    profileId: '',
    groupId: '',
  })
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState(null)
  const [created, setCreated] = useState(null)
  const fleet = fleets.find((f) => f.fleetId === form.fleetId)
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })
  const running = fleet && ['running', 'deploying'].includes(fleet.status)

  async function submit() {
    setSaving(true)
    setErr(null)
    try {
      const vcp = await vcpsApi.create({
        fleetId: form.fleetId,
        chargePointId: form.chargePointId.trim(),
        ...(form.vcpId.trim() ? { vcpId: form.vcpId.trim() } : {}),
        ...(form.profileId.trim() ? { profileId: form.profileId.trim() } : {}),
        ...(form.groupId.trim() ? { groupId: form.groupId.trim() } : {}),
      })
      setCreated(vcp)
      onDone()
    } catch (e) {
      setErr(e.message)
    } finally {
      setSaving(false)
    }
  }

  async function redeploy() {
    setSaving(true)
    try {
      await fleetsApi.redeploy(form.fleetId)
      onClose()
    } catch (e) {
      setErr(e.message)
      setSaving(false)
    }
  }

  if (fleets.length === 0) {
    return (
      <Modal title="Register VCP" onClose={onClose}
        footer={<Link className="btn-primary" to="/fleets">Go to VCP Fleets</Link>}>
        <div className="form-info">Every VCP belongs to a fleet. Create a fleet first, then add VCPs to it.</div>
      </Modal>
    )
  }

  if (created) {
    return (
      <Modal
        title="VCP registered"
        onClose={onClose}
        footer={(
          <>
            <button className="btn-secondary" onClick={onClose}>Close</button>
            {running && (
              <button className="btn-primary" disabled={saving} onClick={redeploy}>
                {saving ? 'Redeploying…' : `Redeploy ${fleet.name} now`}
              </button>
            )}
          </>
        )}
      >
        <div className="form-success">
          {created.vcpId} ({created.chargePointId}) added to fleet {fleet?.name}.
        </div>
        <div className="form-info">
          {running
            ? 'The fleet worker is running with its previous configuration. Redeploy to bring this VCP online.'
            : 'It goes live the next time the fleet is deployed.'}
        </div>
        {err && <div className="form-error">{err}</div>}
      </Modal>
    )
  }

  return (
    <Modal
      title="Register VCP"
      onClose={onClose}
      footer={(
        <>
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={saving || !form.fleetId || !form.chargePointId.trim()} onClick={submit}>
            {saving ? 'Registering…' : 'Register'}
          </button>
        </>
      )}
    >
      <label className="form-label">Fleet <span className="form-hint">required — the VCP is added to this fleet's worker config</span>
        <select className="form-select" value={form.fleetId} onChange={set('fleetId')}>
          {fleets.map((f) => <option key={f.fleetId} value={f.fleetId}>{f.name} ({f.status})</option>)}
        </select>
      </label>
      <div className="form-row">
        <label className="form-label">Charge point ID <span className="form-hint">OCPP identity at the CSMS</span>
          <input className="form-input" value={form.chargePointId} onChange={set('chargePointId')}
            placeholder={fleet ? `${fleet.chargePointIdPrefix}…` : 'CP-…'} autoFocus />
        </label>
        <label className="form-label">VCP ID <span className="form-hint">blank = next {fleet?.idPrefix}NNNNN</span>
          <input className="form-input" value={form.vcpId} onChange={set('vcpId')} />
        </label>
      </div>
      <div className="form-row">
        <label className="form-label">Profile <span className="form-hint">blank = {fleet?.profileId}</span>
          <input className="form-input" value={form.profileId} onChange={set('profileId')} />
        </label>
        <label className="form-label">Group <span className="form-hint">blank = {fleet?.groupId || 'none'}</span>
          <input className="form-input" value={form.groupId} onChange={set('groupId')} />
        </label>
      </div>
      {running && <div className="form-info">This fleet is running; the new VCP comes online after a redeploy.</div>}
      {err && <div className="form-error">{err}</div>}
    </Modal>
  )
}
