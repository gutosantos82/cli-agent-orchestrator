// ServerStatusCards — two small read-only cards for the Home tab.
//
// HealthCard: GET /health — terminal backend, per-component probes and the
// server version (the `cao --version` / `curl /health` checks, without a shell).
//
// FleetStatusCard: GET /fleet/status — the `cao fleet status` summary (broker,
// live workers, cleanup pending, lease-state histogram), polled every 10s. A 503
// means no broker is configured or it is unreachable and is shown with the
// server's own message. A 404 on the FIRST read means the server predates the
// route, so the card hides itself rather than showing a permanent error.

import { useEffect, useState } from 'react'
import { Activity, Server } from 'lucide-react'
import { api, FleetStatus, HealthInfo } from '../api'

const FLEET_POLL_MS = 10000

export function HealthCard() {
  const [health, setHealth] = useState<HealthInfo | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    api.getHealth()
      .then(h => { if (!cancelled && h && typeof h === 'object') { setHealth(h); setError(null) } })
      .catch((e: any) => { if (!cancelled) setError(e?.detail || e?.message || 'Health check failed') })
    return () => { cancelled = true }
  }, [])

  return (
    <div className="rounded-xl border border-gray-700/50 bg-gray-900/40 p-4" aria-label="Server health">
      <div className="flex items-center gap-2 text-sm font-semibold text-gray-300">
        <Server size={15} aria-hidden="true" /> Server
        {health?.version && <span className="ml-auto font-mono text-xs text-gray-500">v{health.version}</span>}
      </div>
      {error ? (
        <p className="mt-2 text-xs text-red-400" role="alert">{error}</p>
      ) : !health ? (
        <p className="mt-2 text-xs text-gray-500">Checking…</p>
      ) : (
        <div className="mt-2 space-y-1 text-xs">
          {health.terminal_backend && (
            <p className="text-gray-400">
              Terminal backend: <span className="font-mono text-gray-200">{health.terminal_backend}</span>
            </p>
          )}
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(health.components && typeof health.components === 'object' ? health.components : {}).map(([name, state]) => (
              <span
                key={name}
                className={`rounded px-1.5 py-0.5 font-mono ${state === 'ok' ? 'bg-emerald-900/40 text-emerald-300' : 'bg-red-900/40 text-red-300'}`}
              >
                {name}: {state}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

export function FleetStatusCard() {
  const [fleet, setFleet] = useState<FleetStatus | null>(null)
  const [unreachable, setUnreachable] = useState<string | null>(null)
  const [hidden, setHidden] = useState(false)

  useEffect(() => {
    let cancelled = false
    let first = true
    const read = () => {
      api.getFleetStatus()
        .then(f => {
          if (cancelled) return
          // Anything that is not the summary shape (an older or proxied server) is ignored.
          if (!f || typeof f !== 'object' || typeof (f as FleetStatus).live !== 'number') return
          setFleet(f); setUnreachable(null)
        })
        .catch((e: any) => {
          if (cancelled) return
          if (first && e?.status === 404) { setHidden(true); return }
          setUnreachable(e?.detail || e?.message || 'Fleet not reachable')
        })
        .finally(() => { first = false })
    }
    read()
    const interval = setInterval(() => { if (!cancelled) read() }, FLEET_POLL_MS)
    return () => { cancelled = true; clearInterval(interval) }
  }, [])

  if (hidden) return null

  return (
    <div className="rounded-xl border border-gray-700/50 bg-gray-900/40 p-4" aria-label="Fleet status">
      <div className="flex items-center gap-2 text-sm font-semibold text-gray-300">
        <Activity size={15} aria-hidden="true" /> Fleet
        {fleet && <span className="ml-auto truncate font-mono text-xs text-gray-500" title={fleet.broker}>{fleet.broker}</span>}
      </div>
      {unreachable ? (
        <div className="mt-2 text-xs" role="status">
          <p className="text-amber-300">Fleet not reachable</p>
          <p className="mt-0.5 text-gray-500">{unreachable}</p>
        </div>
      ) : !fleet ? (
        <p className="mt-2 text-xs text-gray-500">Reading fleet…</p>
      ) : (
        <div className="mt-2 space-y-1.5 text-xs">
          <p className="text-gray-300">
            <span className="text-lg font-bold text-white">{fleet.live}</span> live
            <span className="mx-2 text-gray-600">·</span>
            {fleet.cleanup_pending} cleanup pending
          </p>
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(fleet.states ?? {}).map(([state, n]) => (
              <span key={state} className="rounded bg-gray-800 px-1.5 py-0.5 font-mono text-gray-300">
                {state}: {n}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
