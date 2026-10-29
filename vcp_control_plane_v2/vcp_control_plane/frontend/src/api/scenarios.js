import { api } from './client.js'

export const scenariosApi = {
  list: () => api.get('/scenarios'),
  run: (body) => api.post('/scenarios/run', body),
  listExecutions: () => api.get('/scenarios/executions'),
  getExecution: (id) => api.get(`/scenarios/executions/${id}`),
  cancelExecution: (id) => api.post(`/scenarios/executions/${id}/cancel`),
}
