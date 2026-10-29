import { api } from './client.js'

const BASE = import.meta.env.VITE_API_BASE_URL || '/api/v1'
const TOKEN = import.meta.env.VITE_API_TOKEN || ''

export const profilesApi = {
  list: () => api.get('/profiles'),
  get: (profileId) => api.get(`/profiles/${profileId}`),
  create: (body) => api.post('/profiles', body),
  deprecate: (profileId) => api.delete(`/profiles/${profileId}`),

  /** Upload a YAML file and import it as a profile. */
  importYaml: async (file) => {
    const formData = new FormData()
    formData.append('file', file)
    const res = await fetch(`${BASE}/profiles/import-yaml`, {
      method: 'POST',
      headers: TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {},
      body: formData,
    })
    if (!res.ok) {
      const json = await res.json().catch(() => ({}))
      throw new Error(json.detail || `HTTP ${res.status}`)
    }
    return res.json()
  },

  importDir: () => api.post('/profiles/import-dir'),
}
