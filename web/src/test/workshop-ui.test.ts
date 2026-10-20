// Tests for the button-driven-workshop additions: the workflow run-control API
// methods (launcher + cancel/resume), the one-click install method, and the
// generic prompt-preset persistence helpers.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { api } from '../api'
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
