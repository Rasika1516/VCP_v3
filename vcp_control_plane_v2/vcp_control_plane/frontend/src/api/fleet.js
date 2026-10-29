import { api } from './client.js'

export const fleetApi = {
  summary: () => api.get('/fleet/summary'),
  workers: () => api.get('/fleet/workers'),
  bulkCommand: (body) => api.post('/fleet/commands', body),
}
