import { NavLink } from 'react-router-dom'
import {
  LayoutDashboard,
  Zap,
  Server,
  BookOpen,
  FlaskConical,
  Radio,
  Boxes,
} from 'lucide-react'
import './Sidebar.css'

const NAV_ITEMS = [
  { to: '/', icon: LayoutDashboard, label: 'Dashboard', end: true },
  { to: '/fleets', icon: Boxes, label: 'VCP Fleets' },
  { to: '/fleet', icon: Server, label: 'Fleet Overview' },
  { to: '/vcps', icon: Zap, label: 'VCPs' },
  { to: '/profiles', icon: BookOpen, label: 'Profiles' },
  { to: '/scenarios', icon: FlaskConical, label: 'Scenarios' },
  { to: '/events', icon: Radio, label: 'Live Events' },
]

export function Sidebar() {
  return (
    <aside className="sidebar">
      <div className="sidebar-logo">
        <Zap size={22} />
        <span>VCP Control</span>
      </div>
      <nav className="sidebar-nav">
        {NAV_ITEMS.map(({ to, icon: Icon, label, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              `sidebar-link${isActive ? ' sidebar-link--active' : ''}`
            }
          >
            <Icon size={18} />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>
      <div className="sidebar-footer">
        <span>VCP Fleet Simulator</span>
      </div>
    </aside>
  )
}
