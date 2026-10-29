/** Status badges shared across pages (contract v2 state vocabulary). */

const ACTUAL = {
  charging: 'badge--blue',
  idle: 'badge--purple',
  preparing: 'badge--amber',
  authorized: 'badge--amber',
  suspended: 'badge--amber',
  finishing: 'badge--amber',
  faulted: 'badge--red',
  unavailable: 'badge--red',
  offline: 'badge--gray',
  unknown: 'badge--gray',
}

const CONNECTION = {
  connected: 'badge--green',
  connecting: 'badge--amber',
  disconnected: 'badge--gray',
}

const FLEET = {
  draft: 'badge--gray',
  deploying: 'badge--amber',
  running: 'badge--green',
  stopping: 'badge--amber',
  stopped: 'badge--gray',
  failed: 'badge--red',
}

const EXEC = {
  pending: 'badge--gray',
  running: 'badge--amber',
  completed: 'badge--green',
  failed: 'badge--red',
  cancelled: 'badge--gray',
}

const COMMAND = {
  dispatched: 'badge--amber',
  acknowledged: 'badge--green',
  failed: 'badge--red',
}

function Badge({ map, value }) {
  return <span className={`badge ${map[value] || 'badge--gray'}`}>{value || '–'}</span>
}

export const StateBadge = ({ state }) => <Badge map={ACTUAL} value={state} />
export const ConnBadge = ({ state }) => <Badge map={CONNECTION} value={state} />
export const FleetStatusBadge = ({ status }) => <Badge map={FLEET} value={status} />
export const ExecStatusBadge = ({ status }) => <Badge map={EXEC} value={status} />
export const CommandStatusBadge = ({ status }) => <Badge map={COMMAND} value={status} />
