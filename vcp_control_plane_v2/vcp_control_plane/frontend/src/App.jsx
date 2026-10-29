import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { Sidebar } from './components/layout/Sidebar.jsx'

// Lazy-load pages for code splitting
const Dashboard = lazy(() => import('./pages/Dashboard.jsx'))
const Fleet = lazy(() => import('./pages/Fleet.jsx'))
const Fleets = lazy(() => import('./pages/Fleets.jsx'))
const VcpList = lazy(() => import('./pages/VcpList.jsx'))
const VcpDetail = lazy(() => import('./pages/VcpDetail.jsx'))
const Profiles = lazy(() => import('./pages/Profiles.jsx'))
const Scenarios = lazy(() => import('./pages/Scenarios.jsx'))
const Events = lazy(() => import('./pages/Events.jsx'))

function PageFallback() {
  return (
    <div style={{ padding: 40, color: 'var(--text-secondary)' }}>Loading…</div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <div className="app-shell">
        <Sidebar />
        <main className="main-content">
          <Suspense fallback={<PageFallback />}>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/fleets" element={<Fleets />} />
              <Route path="/fleet" element={<Fleet />} />
              <Route path="/vcps" element={<VcpList />} />
              <Route path="/vcps/:vcpId" element={<VcpDetail />} />
              <Route path="/profiles" element={<Profiles />} />
              <Route path="/scenarios" element={<Scenarios />} />
              <Route path="/events" element={<Events />} />
              <Route path="*" element={<div style={{ padding: 40 }}>Page not found.</div>} />
            </Routes>
          </Suspense>
        </main>
      </div>
    </BrowserRouter>
  )
}
