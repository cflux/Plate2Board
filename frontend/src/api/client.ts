import type {
  DiodeType,
  MatrixStrategy,
  ParseResult,
  StabilizerType,
  SwitchDef,
  SwitchType,
} from '../types'

export interface BackendVersion {
  version: string
  built_at: string
}

export async function getBackendVersion(): Promise<BackendVersion> {
  const res = await fetch('/api/version')
  if (!res.ok) throw new Error(`version fetch failed (${res.status})`)
  return (await res.json()) as BackendVersion
}

async function postBody(path: string, body: unknown): Promise<string> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const data = (await res.json()) as { detail?: string }
      if (data.detail) detail = data.detail
    } catch {
      // ignore
    }
    throw new Error(`Generation failed (${res.status}): ${detail}`)
  }
  return res.text()
}

export function generateNetlist(switches: SwitchDef[]): Promise<string> {
  return postBody('/api/generate-netlist', { switches })
}

export function generateSchematic(switches: SwitchDef[]): Promise<string> {
  return postBody('/api/generate-schematic', { switches })
}

export function generatePcb(
  result: ParseResult,
  switchType: SwitchType = 'soldered',
  diodeType: DiodeType = 'tht',
  stabilizerType: StabilizerType = 'pcb_mount',
): Promise<string> {
  const params = new URLSearchParams({
    switch_type: switchType,
    diode_type: diodeType,
    stabilizer_type: stabilizerType,
  })
  return postBody(`/api/generate-pcb?${params}`, result)
}

export async function generateProjectZip(
  result: ParseResult,
  projectName: string,
  switchType: SwitchType,
  diodeType: DiodeType,
  stabilizerType: StabilizerType = 'pcb_mount',
): Promise<Blob> {
  const params = new URLSearchParams({
    project_name: projectName,
    switch_type: switchType,
    diode_type: diodeType,
    stabilizer_type: stabilizerType,
  })
  const res = await fetch(`/api/generate-project?${params}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(result),
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const data = (await res.json()) as { detail?: string }
      if (data.detail) detail = data.detail
    } catch {
      // ignore
    }
    throw new Error(`Project generation failed (${res.status}): ${detail}`)
  }
  return res.blob()
}

export function generatePlateSvg(result: ParseResult): Promise<string> {
  return postBody('/api/generate-plate-svg', result)
}

// ---- routed-project lifecycle ---------------------------------------------
// Backend kicks off an async routing job and returns immediately with a
// job_id. The frontend polls /api/route-jobs/{id} for progress and fetches
// /api/route-jobs/{id}/result when state === 'done'.

export interface RouteJobStart {
  job_id: string
  status_url: string
  result_url: string
}

export type RouteJobState = 'pending' | 'running' | 'done' | 'failed'

export interface RouteJobStatus {
  job_id: string
  state: RouteJobState
  phase: string
  percent: number
  elapsed_s?: number
  error?: string
  stats?: {
    routed: number
    unrouted: number
    total: number
    vias: number
    pass?: number
    log?: string
  }
}

async function jsonOrThrow<T>(res: Response, action: string): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText
    try {
      const data = (await res.json()) as { detail?: string }
      if (data.detail) detail = data.detail
    } catch {
      // ignore
    }
    throw new Error(`${action} failed (${res.status}): ${detail}`)
  }
  return (await res.json()) as T
}

export async function startRoutedProject(
  result: ParseResult,
  projectName: string,
  switchType: SwitchType,
  diodeType: DiodeType,
  stabilizerType: StabilizerType = 'pcb_mount',
): Promise<RouteJobStart> {
  const params = new URLSearchParams({
    project_name: projectName,
    switch_type: switchType,
    diode_type: diodeType,
    stabilizer_type: stabilizerType,
  })
  const res = await fetch(`/api/generate-routed-project?${params}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(result),
  })
  return jsonOrThrow<RouteJobStart>(res, 'Routed project start')
}

export async function getRouteJob(jobId: string): Promise<RouteJobStatus> {
  const res = await fetch(`/api/route-jobs/${encodeURIComponent(jobId)}`)
  return jsonOrThrow<RouteJobStatus>(res, 'Route job poll')
}

export async function downloadRoutedProjectResult(
  jobId: string,
): Promise<Blob> {
  const res = await fetch(
    `/api/route-jobs/${encodeURIComponent(jobId)}/result`,
  )
  if (!res.ok) {
    let detail = res.statusText
    try {
      const data = (await res.json()) as { detail?: string }
      if (data.detail) detail = data.detail
    } catch {
      // ignore
    }
    throw new Error(`Routed project download failed (${res.status}): ${detail}`)
  }
  return res.blob()
}

export async function parseSvg(
  file: File,
  strategy: MatrixStrategy = 'row_first',
  unitOverride: string = 'auto',
): Promise<ParseResult> {
  const body = new FormData()
  body.append('file', file)
  body.append('matrix_strategy', strategy)
  body.append('svg_unit_override', unitOverride)
  const res = await fetch('/api/parse', { method: 'POST', body })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const data = (await res.json()) as { detail?: string }
      if (data.detail) detail = data.detail
    } catch {
      // ignore
    }
    throw new Error(`Parse failed (${res.status}): ${detail}`)
  }
  return (await res.json()) as ParseResult
}
