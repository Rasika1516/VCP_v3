import { api } from './client.js'

export const vcpsApi = {
  list: (params) => api.get('/vcps', params),
  get: (vcpId) => api.get(`/vcps/${vcpId}`),
  create: (body) => api.post('/vcps', body),
  patch: (vcpId, body) => api.patch(`/vcps/${vcpId}`, body),
  delete: (vcpId) => api.delete(`/vcps/${vcpId}`),
  sendCommand: (vcpId, body) => api.post(`/vcps/${vcpId}/commands`, body),
  getCommands: (vcpId, limit) => api.get(`/vcps/${vcpId}/commands`, { limit }),
}
