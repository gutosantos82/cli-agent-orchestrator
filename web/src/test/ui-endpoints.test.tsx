// Run-artifact ("View report") and fleet-worker views: the API client shapes and
// the two components that consume them.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { api } from '../api'
import { RunArtifacts } from '../components/workflow/RunArtifacts'
import { FleetWorkers } from '../components/FleetWorkers'

describe('run artifact + fleet worker API methods', () => {
  const mockFetch = vi.fn()
  beforeEach(() => vi.stubGlobal('fetch', mockFetch))
  afterEach(() => { vi.unstubAllGlobals(); mockFetch.mockReset() })

  const ok = (data: unknown) => mockFetch.mockResolvedValueOnce({
    ok: true, status: 200, statusText: 'OK',
    json: () => Promise.resolve(data), text: () => Promise.resolve(JSON.stringify(data)),
  })

  it('listWorkflowArtifacts / getWorkflowArtifact encode the run id and input', async () => {
    ok({ run_id: 'r 1', artifacts: [] })
    await api.listWorkflowArtifacts('r 1')
    expect(mockFetch.mock.calls[0][0]).toBe('/workflows/runs/r%201/artifacts')

    ok({ run_id: 'r1', input: 'report', path: '/tmp/x.md', size: 1, content: 'x' })
    await api.getWorkflowArtifact('r1', 'report')
    expect(mockFetch.mock.calls[1][0]).toBe('/workflows/runs/r1/artifacts/report')
  })

  it('listFleetWorkers adds ?all=true only when asked; getFleetWorker encodes the id', async () => {
    ok({ broker: 'b', workers: [] })
    await api.listFleetWorkers()
    expect(mockFetch.mock.calls[0][0]).toBe('/fleet/workers')
    ok({ broker: 'b', workers: [] })
    await api.listFleetWorkers(true)
    expect(mockFetch.mock.calls[1][0]).toBe('/fleet/workers?all=true')
    ok({ worker_id: 'a/b', lease: null, terminal: null, terminal_error: null, last_output: null })
    await api.getFleetWorker('a/b')
    expect(mockFetch.mock.calls[2][0]).toBe('/fleet/workers/a%2Fb')
  })
})

describe('RunArtifacts', () => {
  afterEach(() => vi.restoreAllMocks())

  it('does not ask while the run is still going', () => {
    const list = vi.spyOn(api, 'listWorkflowArtifacts')
    const { container } = render(<RunArtifacts runId="r1" terminal={false} />)
    expect(list).not.toHaveBeenCalled()
    expect(container.textContent).toBe('')
  })

  it('shows a View button per artifact and renders the report as text, not HTML', async () => {
    vi.spyOn(api, 'listWorkflowArtifacts').mockResolvedValue({
      run_id: 'r1',
      artifacts: [
        { input: 'report', path: '/tmp/go.md', available: true, size: 30 },
        { input: 'draft', path: '/tmp/draft.md', available: false, size: null },
      ],
    })
    const get = vi.spyOn(api, 'getWorkflowArtifact').mockResolvedValue({
      run_id: 'r1', input: 'report', path: '/tmp/go.md', size: 30,
      content: '# Verdict\n<img src=x onerror=alert(1)>',
    })
    const { container } = render(<RunArtifacts runId="r1" terminal={true} />)

    const view = await screen.findByRole('button', { name: /View report/ })
    expect(screen.getByRole('button', { name: /View draft/ })).toBeDisabled()
    fireEvent.click(view)

    await waitFor(() => expect(get).toHaveBeenCalledWith('r1', 'report'))
    const pre = await waitFor(() => {
      const el = container.querySelector('pre')
      expect(el).not.toBeNull()
      return el!
    })
    expect(pre.textContent).toContain('<img src=x onerror=alert(1)>')
    expect(container.querySelector('img')).toBeNull()
  })

  it('renders nothing when the run declared no report', async () => {
    vi.spyOn(api, 'listWorkflowArtifacts').mockResolvedValue({ run_id: 'r1', artifacts: [] })
    const { container } = render(<RunArtifacts runId="r1" terminal={true} />)
    await waitFor(() => expect(api.listWorkflowArtifacts).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })
})

describe('FleetWorkers', () => {
  afterEach(() => vi.restoreAllMocks())

  it('fetches nothing until opened', () => {
    const list = vi.spyOn(api, 'listFleetWorkers')
    render(<FleetWorkers />)
    expect(list).not.toHaveBeenCalled()
  })

  it('lists workers and shows one worker on Details', async () => {
    vi.spyOn(api, 'listFleetWorkers').mockResolvedValue({
      broker: 'http://127.0.0.1:9890',
      workers: [{
        worker_id: 'a1b2c3d4', state: 'leased', reason: null, agent_profile: 'developer',
        provider: 'claude_code', age_seconds: 42, workload_present: true,
        cleanup_pending: false, lease_tracked: true,
      }],
    })
    const detail = vi.spyOn(api, 'getFleetWorker').mockResolvedValue({
      worker_id: 'a1b2c3d4',
      lease: null,
      terminal: { id: 't1', name: 'dev', session_name: 's', agent_profile: 'developer', provider: 'claude_code', status: 'completed' },
      terminal_error: null,
      last_output: 'all tests pass',
    })
    render(<FleetWorkers />)
    fireEvent.click(screen.getByRole('button', { name: /Fleet workers/ }))

    expect(await screen.findByText('a1b2c3d4')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Details' }))
    await waitFor(() => expect(detail).toHaveBeenCalledWith('a1b2c3d4'))
    expect(await screen.findByText('all tests pass')).toBeInTheDocument()
    expect(screen.getByText(/not in the broker's ledger/)).toBeInTheDocument()
  })

  it('reads a 503 as "no fleet reachable" and re-lists with settled leases on toggle', async () => {
    const list = vi.spyOn(api, 'listFleetWorkers')
      .mockRejectedValueOnce(Object.assign(new Error('503'), { status: 503, detail: 'No fleet configured.' }))
      .mockResolvedValue({ broker: 'b', workers: [] })
    render(<FleetWorkers />)
    fireEvent.click(screen.getByRole('button', { name: /Fleet workers/ }))

    expect(await screen.findByText(/No fleet reachable: No fleet configured\./)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox'))
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(true))
    expect(await screen.findByText('No workers.')).toBeInTheDocument()
  })
})
