// Fixes from the live e2e run: a pending plan approval survives a reload, the
// fleet worker's last output is shown without terminal escapes, and the confirm
// modal is announced as a dialog.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { WorkflowLauncher, LAUNCHER_DRAFT_KEY, loadLauncherDraft } from '../components/workflow/WorkflowLauncher'
import { stripAnsi } from '../components/FleetWorkers'
import { ConfirmModal } from '../components/ConfirmModal'

function ok(data: unknown) {
  return { ok: true, status: 200, statusText: 'OK', json: () => Promise.resolve(data), text: () => Promise.resolve(JSON.stringify(data)) }
}

describe('launcher draft persistence', () => {
  const mockFetch = vi.fn()
  beforeEach(() => { localStorage.clear(); vi.stubGlobal('fetch', mockFetch) })
  afterEach(() => { cleanup(); vi.restoreAllMocks(); mockFetch.mockReset() })

  const inputs = [{ key: 'topic', value: 't' }, { key: 'report', value: '/tmp/x.md' }]
  const forDraft = JSON.stringify(['go_no_go_review', inputs, 'gng-1'])

  it('restores a pending approval after a reload and opens the launcher', async () => {
    localStorage.setItem(LAUNCHER_DRAFT_KEY, JSON.stringify({
      selected: 'go_no_go_review', runId: 'gng-1', inputs,
      pendingApproval: { planId: 'plan-v2:abc', forDraft },
    }))
    mockFetch.mockResolvedValue(ok([{ name: 'go_no_go_review', source_path: '/p.py', mode: 'script', step_count: null, description: '', indexed_at: '' }]))
    render(<WorkflowLauncher />)
    expect(await screen.findByText('plan-v2:abc')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Approve & run/ })).toBeTruthy()
  })

  it('drops a stored approval that no longer matches the draft', () => {
    localStorage.setItem(LAUNCHER_DRAFT_KEY, JSON.stringify({
      selected: 'go_no_go_review', runId: 'other', inputs,
      pendingApproval: { planId: 'plan-v2:abc', forDraft },
    }))
    mockFetch.mockResolvedValue(ok([]))
    render(<WorkflowLauncher />)
    expect(screen.queryByText('plan-v2:abc')).toBeNull()
  })

  it('ignores malformed drafts', () => {
    localStorage.setItem(LAUNCHER_DRAFT_KEY, 'not json')
    expect(loadLauncherDraft()).toBeNull()
    localStorage.setItem(LAUNCHER_DRAFT_KEY, JSON.stringify({ selected: 1 }))
    expect(loadLauncherDraft()).toBeNull()
    localStorage.setItem(LAUNCHER_DRAFT_KEY, JSON.stringify({ selected: 'a', runId: '', inputs: [{ key: 'k', value: 'v' }, 7], pendingApproval: { planId: 3 } }))
    expect(loadLauncherDraft()).toEqual({ selected: 'a', runId: '', inputs: [{ key: 'k', value: 'v' }], pendingApproval: null })
  })
})

describe('stripAnsi', () => {
  it('removes colour, cursor and title sequences', () => {
    const raw = '\x1b]0;cao@worker\x07\x1b[1m\x1b[32mcao@w\x1b[0m:\x1b[34m~/ws\x1b[0m$ \x1b[?2004hdone'
    expect(stripAnsi(raw)).toBe('cao@w:~/ws$ done')
  })
  it('leaves plain text alone', () => {
    expect(stripAnsi('PASS 10/10\n')).toBe('PASS 10/10\n')
  })
})

describe('ConfirmModal accessibility', () => {
  afterEach(() => cleanup())
  it('is an alertdialog labelled by its title and message', () => {
    render(<ConfirmModal open title="Shut down all sessions" message="This stops 5 sessions." onConfirm={() => {}} onCancel={() => {}} />)
    const dlg = screen.getByRole('alertdialog', { name: 'Shut down all sessions' })
    expect(dlg.getAttribute('aria-modal')).toBe('true')
    expect(dlg.getAttribute('aria-describedby')).toBe('confirm-modal-message')
  })
})
