import { useState, useEffect, useCallback } from 'react'
import { fleetApi } from '../api/fleet.js'

/**
 * useFleetSummary — polls /fleet/summary at the given interval.
 *
 * @param {number} intervalMs  Polling interval in ms (default 10 000)
 * @returns {{ summary, loading, error, refresh }}
 */
export function useFleetSummary(intervalMs = 10_000) {
  const [summary, setSummary] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const refresh = useCallback(async () => {
    try {
      const data = await fleetApi.summary()
      setSummary(data)
      setError(null)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, intervalMs)
    return () => clearInterval(id)
  }, [refresh, intervalMs])

  return { summary, loading, error, refresh }
}
