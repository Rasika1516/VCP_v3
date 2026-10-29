import { useState, useEffect, useRef } from 'react'
import { Header } from '../components/layout/Header.jsx'
import { profilesApi } from '../api/profiles.js'
import './Profiles.css'

export default function Profiles() {
  const [profiles, setProfiles] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [uploadMsg, setUploadMsg] = useState(null)
  const fileRef = useRef(null)

  async function load() {
    setLoading(true)
    try {
      const data = await profilesApi.list()
      setProfiles(data.items)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  async function handleUpload(e) {
    const file = e.target.files[0]
    if (!file) return
    setUploading(true)
    setUploadMsg(null)
    try {
      const profile = await profilesApi.importYaml(file)
      setUploadMsg(`Imported: ${profile.profileId}`)
      load()
    } catch (err) {
      setUploadMsg(`Error: ${err.message}`)
    } finally {
      setUploading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  async function deprecate(profileId) {
    if (!confirm(`Deprecate profile "${profileId}"?`)) return
    try {
      await profilesApi.deprecate(profileId)
      load()
    } catch (err) {
      alert(err.message)
    }
  }

  return (
    <div className="page">
      <Header title="Charger Profiles" />
      <div className="profiles-toolbar">
        <label className="btn-secondary upload-label">
          {uploading ? 'Importing…' : 'Import YAML'}
          <input
            ref={fileRef}
            type="file"
            accept=".yaml,.yml"
            style={{ display: 'none' }}
            onChange={handleUpload}
            disabled={uploading}
          />
        </label>
        {uploadMsg && (
          <span className={uploadMsg.startsWith('Error') ? 'msg-error' : 'msg-success'}>
            {uploadMsg}
          </span>
        )}
      </div>

      {error && <div className="page-error">{error}</div>}

      {loading ? (
        <div className="page-loading">Loading profiles…</div>
      ) : (
        <div className="profiles-table-wrap">
          <table className="profiles-table">
            <thead>
              <tr>
                <th>Profile ID</th>
                <th>Vendor</th>
                <th>Model</th>
                <th>Version</th>
                <th>OCPP</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {profiles.length === 0 && (
                <tr><td colSpan={7} className="table-empty">No profiles.</td></tr>
              )}
              {profiles.map((p) => (
                <tr key={p.profileId} className={p.status === 'deprecated' ? 'row-deprecated' : ''}>
                  <td className="td-mono">{p.profileId}</td>
                  <td>{p.vendor}</td>
                  <td>{p.model}</td>
                  <td>{p.version}</td>
                  <td>{p.ocppVersion}</td>
                  <td>
                    <span className={`profile-status profile-status--${p.status}`}>{p.status}</span>
                  </td>
                  <td>
                    {p.status !== 'deprecated' && (
                      <button className="btn-danger-sm" onClick={() => deprecate(p.profileId)}>
                        Deprecate
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
  )
}
