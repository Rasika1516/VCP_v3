import { api } from './client.js'

/** VCP Fleets — deployable data-plane workers (POST /fleets, /deploy, /stop …). */
export const fleetsApi = {
  list: () => api.get('/fleets'),
  get: (id) => api.get(`/fleets/${id}`),
  create: (body) => api.post('/fleets', body),
  patch: (id, body) => api.patch(`/fleets/${id}`, body),
  remove: (id, force = false) => api.delete(`/fleets/${id}${force ? '?force=true' : ''}`),
  config: (id) => api.get(`/fleets/${id}/config`),
  configYaml: (id) => api.getText(`/fleets/${id}/config`, { format: 'yaml' }),
  vcps: (id, params) => api.get(`/fleets/${id}/vcps`, params),
  deploy: (id) => api.post(`/fleets/${id}/deploy`),
  stop: (id) => api.post(`/fleets/${id}/stop`),
  redeploy: (id) => api.post(`/fleets/${id}/redeploy`),
  logs: (id, tail = 200) => api.getText(`/fleets/${id}/logs`, { tail }),
  launchers: () => api.get('/fleets/launchers'),
  profiles: () => api.get('/fleets/profiles'),
}
