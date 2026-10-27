// Tests for the second button-driven-workshop batch: the Home server/fleet
// cards, the pasted-plan-id approval form, the run result panel's report
// detection, the spec / result / health / memory-export client methods.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { api, WorkflowRunResult } from '../api'
import { FleetStatusCard, HealthCard } from '../components/ServerStatusCards'
import { PlanApprovalForm } from '../components/workflow/PlanApprovalForm'
import { RunResultPanel, findReportPaths } from '../components/workflow/RunResultPanel'

function ok(data: unknown) {
  return { ok: true, status: 200, statusText: 'OK', json: () => Promise.resolve(data), text: () => Promise.resolve(JSON.stringify(data)) }
}
function fail(status: number, detail: unknown) {
  return { ok: false, status, statusText: 'Error', json: () => Promise.resolve({ detail }) }
}

describe('batch-1 client methods', () => {
  const mockFetch = vi.fn()
  beforeEach(() => vi.stubGlobal('fetch', mockFetch))
  afterEach(() => { vi.restoreAllMocks(); mockFetch.mockReset() })

  it('getHealth reads /health', async () => {
    mockFetch.mockResolvedValueOnce(ok({ status: 'ok', version: '2.5.4' }))
    expect((await api.getHealth()).version).toBe('2.5.4')
    expect(mockFetch.mock.calls[0][0]).toBe('/health')
  })

  it('getWorkflowRunResult and getWorkflowSpec encode their path segment', async () => {
    mockFetch.mockResolvedValueOnce(ok({ run_id: 'a b', state: 'completed', steps: [] }))
    await api.getWorkflowRunResult('a b')
    expect(mockFetch.mock.calls[0][0]).toBe('/workflows/runs/a%20b/result')
    mockFetch.mockResolvedValueOnce(ok({ name: 'wf', source: 'x' }))
    await api.getWorkflowSpec('go/no')
    expect(mockFetch.mock.calls[1][0]).toBe('/workflows/go%2Fno')
  })

  it('exportMemory sends scope, scope_id and redact and returns a blob', async () => {
    const blob = new Blob(['tar'])
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, blob: () => Promise.resolve(blob) })
    const r = await api.exportMemory({ scope: 'project', scopeId: 'p1', redact: true })
    expect(r.blob).toBe(blob)
    expect(r.filename).toBe('cao-memory-project.tar.gz')
    expect(mockFetch.mock.calls[0][0]).toBe('/memory/export?scope=project&scope_id=p1&redact=true')
  })

  it('exportMemory surfaces the server detail on a refusal', async () => {
    mockFetch.mockResolvedValueOnce(fail(400, "scope 'project' requires scope_id"))
    await expect(api.exportMemory({ scope: 'project' })).rejects.toMatchObject({ status: 400, detail: "scope 'project' requires scope_id" })
  })
})

describe('findReportPaths', () => {
  const base: WorkflowRunResult = { run_id: 'r', workflow_name: 'wf', state: 'completed', started_at: '', steps: [] }

  it('lifts report-like paths out of step outputs, once each', () => {
    const r = {
      ...base,
      steps: [
        { id: 'a', state: 'completed', attempts: 1, output: { findings: 'x', report_path: '/tmp/go.md' } },
        { id: 'b', state: 'completed', attempts: 1, output: { nested: { file: '/tmp/go.md' }, other: '/tmp/notes.md' } },
      ],
    }
    expect(findReportPaths(r)).toEqual(['/tmp/go.md', '/tmp/notes.md'])
  })

  it('ignores prose that merely mentions a report', () => {
    const r = { ...base, steps: [{ id: 'a', state: 'completed', attempts: 1, output: { report: 'all good' } }] }
    expect(findReportPaths(r)).toEqual([])
  })
})

describe('Home cards', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('FleetStatusCard shows the summary', async () => {
    vi.spyOn(api, 'getFleetStatus').mockResolvedValue({ broker: 'http://127.0.0.1:9890', live: 3, cleanup_pending: 1, states: { leased: 3 } })
    render(<FleetStatusCard />)
    expect(await screen.findByText('3')).toBeTruthy()
    expect(screen.getByText('leased: 3')).toBeTruthy()
  })

  it('FleetStatusCard says the fleet is not reachable on 503', async () => {
    vi.spyOn(api, 'getFleetStatus').mockRejectedValue(Object.assign(new Error('503'), { status: 503, detail: 'No fleet configured' }))
    render(<FleetStatusCard />)
    expect(await screen.findByText('Fleet not reachable')).toBeTruthy()
    expect(screen.getByText('No fleet configured')).toBeTruthy()
  })

  it('FleetStatusCard hides itself when the first read is a 404 (older server)', async () => {
    const spy = vi.spyOn(api, 'getFleetStatus').mockRejectedValue(Object.assign(new Error('404'), { status: 404 }))
    const { container } = render(<FleetStatusCard />)
    await waitFor(() => expect(spy).toHaveBeenCalled())
    await waitFor(() => expect(container.querySelector('[aria-label="Fleet status"]')).toBeNull())
  })

  it('HealthCard shows backend, components and version', async () => {
    vi.spyOn(api, 'getHealth').mockResolvedValue({ status: 'ok', terminal_backend: 'herdr', version: '2.5.4', components: { cao: 'ok', herdr: 'ok', claude: 'unavailable' } })
    render(<HealthCard />)
    expect(await screen.findByText('herdr')).toBeTruthy()
    expect(screen.getByText('v2.5.4')).toBeTruthy()
    expect(screen.getByText('claude: unavailable')).toBeTruthy()
  })
})

describe('PlanApprovalForm', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('approves the pasted id verbatim and reports the outcome', async () => {
    const spy = vi.spyOn(api, 'approveWorkflowPlan').mockResolvedValue({ plan_id: 'plan-v2:abc', approved: true, changed: true, approved_at: 't0' })
    render(<PlanApprovalForm />)
    fireEvent.click(screen.getByText('Approve a plan id'))
    fireEvent.change(screen.getByLabelText('Plan id'), { target: { value: ' plan-v2:abc ' } })
    fireEvent.click(screen.getByRole('button', { name: /Approve$/ }))
    expect(await screen.findByRole('status')).toBeTruthy()
    expect(spy).toHaveBeenCalledWith('plan-v2:abc')
    expect(screen.getByRole('status').textContent).toContain('Approved at t0')
  })

  it('says when the plan was already approved', async () => {
    vi.spyOn(api, 'approveWorkflowPlan').mockResolvedValue({ plan_id: 'p', approved: true, changed: false })
    render(<PlanApprovalForm />)
    fireEvent.click(screen.getByText('Approve a plan id'))
    fireEvent.change(screen.getByLabelText('Plan id'), { target: { value: 'p' } })
    fireEvent.click(screen.getByRole('button', { name: /Approve$/ }))
    expect((await screen.findByRole('status')).textContent).toContain('Already approved')
  })
})

describe('RunResultPanel', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('does not fetch while the run is still going', () => {
    const spy = vi.spyOn(api, 'getWorkflowRunResult')
    const { container } = render(<RunResultPanel runId="r" terminal={false} state="running" />)
    expect(spy).not.toHaveBeenCalled()
    expect(container.textContent).toBe('')
  })

  it('shows the final state and the report path once terminal', async () => {
    vi.spyOn(api, 'getWorkflowRunResult').mockResolvedValue({
      run_id: 'r', workflow_name: 'wf', state: 'completed', started_at: '', finished_at: 't1',
      steps: [{ id: 'write-report', state: 'completed', attempts: 1, output: { report: '/tmp/go.md' } }],
    })
    render(<RunResultPanel runId="r" terminal state="completed" />)
    expect(await screen.findByText('/tmp/go.md')).toBeTruthy()
    expect(screen.getByText('completed')).toBeTruthy()
  })
})
