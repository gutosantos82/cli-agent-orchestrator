// Tests for the button-driven-workshop additions: the workflow run-control API
// methods (launcher + cancel/resume), the one-click install method, and the
// generic prompt-preset persistence helpers.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { api } from '../api'
import { useStore } from '../store'
import { loadPromptPresets, savePromptPresets, PROMPT_PRESETS_KEY } from '../components/PromptPresets'

describe('Workshop run-control + install API methods', () => {
  const mockFetch = vi.fn()
  beforeEach(() => vi.stubGlobal('fetch', mockFetch))
  afterEach(() => vi.restoreAllMocks())

  function mockResponse(data: unknown, status = 200) {
    mockFetch.mockResolvedValueOnce({
      ok: status >= 200 && status < 300,
      status,
      statusText: status === 200 ? 'OK' : 'Error',
      json: () => Promise.resolve(data),
      text: () => Promise.resolve(JSON.stringify(data)),
    })
  }

  it('listWorkflows fetches /workflows (definitions, not runs)', async () => {
    const rows = [{ name: 'go_no_go_review', source_path: '/p.py', mode: 'script', step_count: null, description: 'd', indexed_at: '' }]
    mockResponse(rows)
    const result = await api.listWorkflows()
    expect(result).toEqual(rows)
    expect(mockFetch).toHaveBeenCalledWith('/workflows', expect.any(Object))
  })

  it('submitWorkflowRun POSTs the async :submit twin with a JSON body', async () => {
    mockResponse({ run_id: 'gng-1', state: 'running' })
    const ack = await api.submitWorkflowRun({ name_or_path: 'go_no_go_review', inputs: { topic: 't', report: '/tmp/x.md' }, run_id: 'gng-1' })
    expect(ack.run_id).toBe('gng-1')
    const [url, opts] = mockFetch.mock.calls[0]
    expect(url).toBe('/workflows/runs:submit')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ name_or_path: 'go_no_go_review', inputs: { topic: 't', report: '/tmp/x.md' }, run_id: 'gng-1' })
    expect(opts.headers['Content-Type']).toBe('application/json')
  })

  it('cancelWorkflowRun POSTs the cancel route with no body', async () => {
    mockResponse({ success: true, run_id: 'gng-1' })
    await api.cancelWorkflowRun('gng-1')
    const [url, opts] = mockFetch.mock.calls[0]
    expect(url).toBe('/workflows/runs/gng-1/cancel')
    expect(opts.method).toBe('POST')
    expect(opts.body).toBeUndefined()
  })

  it('resumeWorkflowRun POSTs the resume route', async () => {
    mockResponse({ run_id: 'gng-1', state: 'running' })
    await api.resumeWorkflowRun('gng-1')
    expect(mockFetch).toHaveBeenCalledWith('/workflows/runs/gng-1/resume', expect.objectContaining({ method: 'POST' }))
  })

  it('installProfile sends {source} alone, and {source,provider} when given', async () => {
    mockResponse({ success: true, message: 'ok' })
    await api.installProfile('reviewer')
    expect(JSON.parse(mockFetch.mock.calls[0][1].body)).toEqual({ source: 'reviewer' })

    mockResponse({ success: true, message: 'ok' })
    await api.installProfile('reviewer', 'claude_code')
    expect(JSON.parse(mockFetch.mock.calls[1][1].body)).toEqual({ source: 'reviewer', provider: 'claude_code' })
    expect(mockFetch.mock.calls[1][0]).toBe('/agents/profiles/install')
  })

  it('getFleetStatus reads /fleet/status', async () => {
    mockResponse({ broker: 'http://127.0.0.1:9890', live: 2, cleanup_pending: 0, states: { leased: 2 } })
    const s = await api.getFleetStatus()
    expect(s.live).toBe(2)
    expect(mockFetch).toHaveBeenCalledWith('/fleet/status', expect.any(Object))
  })

  it('surfaces 503 when the fleet broker is not reachable', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false, status: 503, statusText: 'Service Unavailable',
      json: () => Promise.resolve({ detail: 'No fleet configured. Set CAO_ELASTIC_BROKER_URL ...' }),
    })
    await expect(api.getFleetStatus()).rejects.toMatchObject({ status: 503 })
  })

  it('approveWorkflowPlan POSTs the plan id verbatim in the body', async () => {
    const planId = 'plan-v1:75048c5db632'
    mockResponse({ plan_id: planId, approved: true, changed: true })
    const r = await api.approveWorkflowPlan(planId)
    expect(r.approved).toBe(true)
    const [url, opts] = mockFetch.mock.calls[0]
    expect(url).toBe('/workflows/plans/approve')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ plan_id: planId })
  })

  it('startWorkflowRun hands back the plan id on an approval_required 403', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false, status: 403, statusText: 'Forbidden',
      json: () => Promise.resolve({ detail: { kind: 'approval_required', plan_id: 'plan-v1:abc', message: 'needs approval' } }),
    })
    const outcome = await useStore.getState().startWorkflowRun('wf', { topic: 't' }, 'gng-1')
    expect(outcome).toEqual({ approvalPlanId: 'plan-v1:abc', message: 'needs approval' })
  })

  it('startWorkflowRun returns null (not a plan) on other refusals', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false, status: 503, statusText: 'Service Unavailable',
      json: () => Promise.resolve({ detail: { kind: 'plan_identity_unavailable', plan_id: null, message: 'freeze failed' } }),
    })
    expect(await useStore.getState().startWorkflowRun('wf', {})).toBeNull()
  })

  it('surfaces ApiError detail on a bad-inputs 400 from submit', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false, status: 400, statusText: 'Bad Request',
      json: () => Promise.resolve({ detail: "undeclared input 'topc'" }),
    })
    await expect(api.submitWorkflowRun({ name_or_path: 'wf', inputs: { topc: 'x' } }))
      .rejects.toMatchObject({ status: 400, detail: "undeclared input 'topc'" })
  })
})

describe('Prompt-preset persistence helpers', () => {
  beforeEach(() => localStorage.clear())

  it('round-trips presets through localStorage', () => {
    savePromptPresets([{ label: 'Ch3 author', text: 'Use the cao-workflow skill…' }])
    expect(loadPromptPresets()).toEqual([{ label: 'Ch3 author', text: 'Use the cao-workflow skill…' }])
    expect(localStorage.getItem(PROMPT_PRESETS_KEY)).toContain('Ch3 author')
  })

  it('returns [] when nothing is stored', () => {
    expect(loadPromptPresets()).toEqual([])
  })

  it('ignores malformed JSON and non-preset entries', () => {
    localStorage.setItem(PROMPT_PRESETS_KEY, 'not json')
    expect(loadPromptPresets()).toEqual([])
    localStorage.setItem(PROMPT_PRESETS_KEY, JSON.stringify([{ label: 'ok', text: 't' }, { label: 42 }, { text: 'no label' }, null]))
    expect(loadPromptPresets()).toEqual([{ label: 'ok', text: 't' }])
  })
})
