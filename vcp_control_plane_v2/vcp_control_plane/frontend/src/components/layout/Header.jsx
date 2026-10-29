import { Wifi, WifiOff } from 'lucide-react'
import './Header.css'

export function Header({ title, connected }) {
  return (
    <header className="page-header">
      <h1 className="page-title">{title}</h1>
      <div className="header-right">
        {connected !== undefined && (
          <span className={`ws-badge ${connected ? 'ws-badge--on' : 'ws-badge--off'}`}>
            {connected ? <Wifi size={14} /> : <WifiOff size={14} />}
            {connected ? 'Live' : 'Disconnected'}
          </span>
        )}
      </div>
    </header>
  )
}
