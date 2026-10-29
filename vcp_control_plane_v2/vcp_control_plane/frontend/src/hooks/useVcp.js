import { useState, useEffect, useCallback } from 'react'
import { vcpsApi } from '../api/vcps.js'

/**
 * useVcp — fetches a single VCP and optionally its command history.
 */
export function useVcp(vcpId) {
  const [vcp, setVcp] = useState(null)
  const [commands, setCommands] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const refresh = useCallback(async () => {
    if (!vcpId) return
    try {
      const [vcpData, cmds] = await Promise.all([
        vcpsApi.get(vcpId),
        vcpsApi.getCommands(vcpId, 50),
      ])
      setVcp(vcpData)
      setCommands(cmds)
      setError(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [vcpId])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 5_000)
    return () => clearInterval(id)
  }, [refresh])

  return { vcp, commands, loading, error, refresh }
}
