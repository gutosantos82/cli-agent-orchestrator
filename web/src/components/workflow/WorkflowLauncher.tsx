// WorkflowLauncher — start a stored workflow from the UI (button-driven
// workshop). Replaces the `cao workflow run` CLI and the WORKFLOW_NAME /
// REPORT_PATH shell-variable dance that gated the Ch3 workflow lab (the measured
// drop-off cliff). Picks an indexed workflow (GET /workflows), collects its run
// inputs as editable key/value rows (prefilled topic/report for the lab), and
// submits async via the store's startWorkflowRun (POST /workflows/runs:submit),
// which refreshes the run list and selects the new run so the detail pane follows
// it live. Purely additive over existing endpoints.

import { useEffect, useState } from 'react'
import { Play, Plus, X, Loader2, ChevronDown, ChevronRight } from 'lucide-react'
import { api, WorkflowDefRow } from '../../api'
import { useStore } from '../../store'

interface InputRow {
  key: string
  value: string
}

// The Ch3 lab's inputs. Prefilled so a participant can run immediately, but
// editable/removable so the launcher works for any workflow's declared inputs.
const DEFAULT_INPUTS: InputRow[] = [
  { key: 'topic', value: '' },
  { key: 'report', value: '/tmp/go-no-go.md' },
]

export function WorkflowLauncher() {
  const startWorkflowRun = useStore(s => s.startWorkflowRun)

  const [open, setOpen] = useState(false)
  const [workflows, setWorkflows] = useState<WorkflowDefRow[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [selected, setSelected] = useState('')
  const [runId, setRunId] = useState('')
  const [inputs, setInputs] = useState<InputRow[]>(DEFAULT_INPUTS)
  const [submitting, setSubmitting] = useState(false)

  // Load the stored workflow definitions when the launcher is first opened.
  useEffect(() => {
    if (!open) return
    let cancelled = false
    api.listWorkflows()
      .then(rows => {
        if (cancelled) return
        setWorkflows(rows)
        setLoadError(null)
        // Default the picker to the only / first workflow for a one-click run.
        if (rows.length && !selected) setSelected(rows[0].name)
      })
      .catch((e: any) => {
        if (!cancelled) setLoadError(e?.detail || e?.message || 'Failed to load workflows')
      })
    return () => { cancelled = true }
  }, [open, selected])

  const setRow = (i: number, patch: Partial<InputRow>) =>
    setInputs(rows => rows.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  const addRow = () => setInputs(rows => [...rows, { key: '', value: '' }])
  const removeRow = (i: number) => setInputs(rows => rows.filter((_, j) => j !== i))

  const submit = async () => {
    if (!selected || submitting) return
    // Only send rows with a non-empty key; the server validates against the
    // workflow's declared inputs and returns a 400 the store surfaces as a snackbar.
    const payload: Record<string, unknown> = {}
    for (const { key, value } of inputs) {
      const k = key.trim()
      if (k) payload[k] = value
    }
    setSubmitting(true)
    try {
      await startWorkflowRun(selected, payload, runId.trim() || undefined)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900/40 p-3 mb-4">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 text-sm font-semibold text-gray-300"
      >
        {open ? <ChevronDown size={16} aria-hidden="true" /> : <ChevronRight size={16} aria-hidden="true" />}
        Run a workflow
      </button>

      {open && (
        <div className="mt-3 space-y-3">
          {loadError ? (
            <p className="text-sm text-red-400" role="alert">{loadError}</p>
          ) : workflows.length === 0 ? (
            <p className="text-sm text-gray-500">
              No stored workflows yet. Author one with the workflow agent first, then it appears here.
            </p>
          ) : (
            <>
              <label className="block">
                <span className="text-xs text-gray-400">Workflow</span>
                <select
                  value={selected}
                  onChange={e => setSelected(e.target.value)}
                  className="mt-1 w-full rounded-lg border border-gray-700 bg-gray-900 px-2 py-1.5 text-sm text-gray-200"
                >
                  {workflows.map(w => (
                    <option key={w.name} value={w.name}>
                      {w.name}{w.description ? ` — ${w.description}` : ''}
                    </option>
                  ))}
                </select>
              </label>

              <div>
                <span className="text-xs text-gray-400">Inputs</span>
                <div className="mt-1 space-y-1.5">
                  {inputs.map((row, i) => (
                    <div key={i} className="flex items-center gap-1.5">
                      <input
                        aria-label={`Input ${i + 1} name`}
                        value={row.key}
                        onChange={e => setRow(i, { key: e.target.value })}
                        placeholder="name"
                        className="w-28 rounded-lg border border-gray-700 bg-gray-900 px-2 py-1 text-sm text-gray-200 font-mono"
                      />
                      <input
                        aria-label={`Input ${i + 1} value`}
                        value={row.value}
                        onChange={e => setRow(i, { value: e.target.value })}
                        placeholder="value"
                        className="flex-1 rounded-lg border border-gray-700 bg-gray-900 px-2 py-1 text-sm text-gray-200"
                      />
                      <button
                        type="button"
                        onClick={() => removeRow(i)}
                        aria-label={`Remove input ${i + 1}`}
                        className="p-1 text-gray-500 hover:text-red-400"
                      >
                        <X size={14} aria-hidden="true" />
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    onClick={addRow}
                    className="inline-flex items-center gap-1 text-xs text-gray-400 hover:text-gray-200"
                  >
                    <Plus size={13} aria-hidden="true" /> Add input
                  </button>
                </div>
              </div>

              <label className="block">
                <span className="text-xs text-gray-400">Run id (optional)</span>
                <input
                  value={runId}
                  onChange={e => setRunId(e.target.value)}
                  placeholder="auto-generated"
                  className="mt-1 w-full rounded-lg border border-gray-700 bg-gray-900 px-2 py-1 text-sm text-gray-200 font-mono"
                />
              </label>

              <button
                type="button"
                onClick={submit}
                disabled={!selected || submitting}
                className="inline-flex items-center gap-2 rounded-lg bg-emerald-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-600 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {submitting ? <Loader2 size={14} className="animate-spin" aria-hidden="true" /> : <Play size={14} aria-hidden="true" />}
                Run workflow
              </button>
            </>
          )}
        </div>
      )}
    </div>
  )
}
