import { api } from './client.js'

let cache = null

/** Worker command catalogue (bus contract v2) — cached for the session. */
export const commandsApi = {
  catalogue: async () => {
    if (!cache) cache = api.get('/commands').then((r) => r.items).catch((e) => { cache = null; throw e })
    return cache
  },
}

export const CATEGORY_LABELS = {
  connection: 'Connection',
  session: 'Charging Session',
  operator: 'Operator Actions',
  fault: 'Fault Injection',
  meter: 'Metering',
  scenario: 'Scenario',
  introspection: 'Introspection',
}
