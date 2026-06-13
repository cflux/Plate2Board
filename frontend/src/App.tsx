import { useEffect, useRef, useState } from 'react'
import { UploadStep } from './components/UploadStep'
import { SvgPreview } from './components/SvgPreview'
import { MatrixGrid } from './components/MatrixGrid'
import { NumberInput } from './components/NumberInput'
import { OrientationHelp } from './components/OrientationHelp'
import {
  downloadRoutedProjectResult,
  generateNetlist,
  generatePcb,
  generatePlateSvg,
  generateProjectZip,
  generateSchematic,
  getBackendVersion,
  getRouteJob,
  parseSvg,
  startRoutedProject,
  type BackendVersion,
  type RouteJobStatus,
} from './api/client'
import type {
  DiodeType,
  MatrixStrategy,
  ParseResult,
  StabilizerDef,
  StabilizerType,
  SwitchDef,
  SwitchType,
} from './types'

// Maps the live route-job status into a short label for the routed-zip
// button. Shows whichever signal best conveys progress: stats when present
// ("Routing… 12/50"), else percent ("Routing… 47%"), else just the phase.
function routedButtonLabel(status: RouteJobStatus | null): string {
  if (!status) return 'Routing…'
  if (status.state === 'done') return 'Downloading…'
  const phase = status.phase.replace(/-/g, ' ')
  const elapsed = status.elapsed_s != null ? ` (${Math.round(status.elapsed_s)}s)` : ''
  if (status.stats && status.stats.total > 0) {
    const { routed, total } = status.stats
    return `${capitalize(phase)}… ${routed}/${total}${elapsed}`
  }
  return `${capitalize(phase)}… ${Math.round(status.percent)}%${elapsed}`
}

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1)
}

// Banner that surfaces the three routing outcomes (in-progress / partial /
// hard failure). Always dismissible. The button state separately disables
// new export actions while a route is in flight.
function RouteProgressBanner({
  status,
  onDismiss,
}: {
  status: RouteJobStatus
  onDismiss: () => void
}) {
  // Connections the user must finish by hand: nets the router gave up on
  // plus pads whose copper never actually attached after the splice.
  const unfinished =
    status.state === 'done' && status.stats
      ? status.stats.unrouted + (status.stats.unattached ?? 0)
      : 0
  const partial = unfinished > 0
  const failed = status.state === 'failed'
  const variant = failed ? 'error' : partial ? 'warning' : 'info'
  let body: string
  const elapsed = status.elapsed_s != null ? ` · elapsed ${Math.round(status.elapsed_s)}s` : ''
  if (failed) {
    body = `Auto-routing failed: ${status.error ?? 'unknown error'}`
  } else if (partial && status.stats) {
    const { routed, total } = status.stats
    body =
      `Routed ${routed} of ${total} connections, but ${unfinished} ` +
      `connection${unfinished === 1 ? '' : 's'} need${unfinished === 1 ? 's' : ''} finishing ` +
      `by hand — open the project in KiCad and route the remaining ratsnest lines.`
  } else if (status.state === 'done') {
    body = `Routed ${status.stats?.routed ?? '?'} connections — download starting.`
  } else if (status.stats && (status.stats.total > 0 || status.stats.pass)) {
    const passInfo = status.stats.pass ? `pass ${status.stats.pass} · ` : ''
    const counts = status.stats.total > 0
      ? `${status.stats.routed} of ${status.stats.total} connections`
      : `${status.stats.routed} connections so far`
    body =
      `${capitalize(status.phase.replace(/-/g, ' '))} — ${passInfo}${counts}` +
      (status.stats.vias ? ` (${status.stats.vias} vias)` : '') +
      elapsed
  } else {
    body =
      `${capitalize(status.phase.replace(/-/g, ' '))} — ${Math.round(status.percent)}%` +
      elapsed
  }
  return (
    <div className={`route-banner route-banner-${variant}`}>
      <div className="route-banner-body">{body}</div>
      {(status.state === 'done' || status.state === 'failed') && (
        <button
          className="route-banner-dismiss"
          onClick={onDismiss}
          aria-label="Dismiss"
          title="Dismiss"
        >
          ×
        </button>
      )}
      {status.state !== 'done' && status.state !== 'failed' && (
        <div className="route-banner-bar">
          <div
            className="route-banner-bar-fill"
            style={{ width: `${Math.max(2, Math.min(100, status.percent))}%` }}
          />
        </div>
      )}
    </div>
  )
}

export function App() {
  const [file, setFile] = useState<File | null>(null)
  const [result, setResult] = useState<ParseResult | null>(null)
  const [originalSwitches, setOriginalSwitches] = useState<SwitchDef[]>([])
  const [originalStabs, setOriginalStabs] = useState<StabilizerDef[]>([])
  const [strategy, setStrategy] = useState<MatrixStrategy>('auto')
  const [switchType, setSwitchType] = useState<SwitchType>('soldered')
  const [diodeType, setDiodeType] = useState<DiodeType>('tht')
  const [stabilizerType, setStabilizerType] = useState<StabilizerType>('pcb_mount')
  const [groundPour, setGroundPour] = useState(true)
  const [rgb, setRgb] = useState(false)
  const [inspectMode, setInspectMode] = useState<boolean>(false)
  const [editPlateMode, setEditPlateMode] = useState<boolean>(false)
  const [selectedOutlineNodeIdx, setSelectedOutlineNodeIdx] = useState<number | null>(null)
  const [selectedHoleId, setSelectedHoleId] = useState<number | null>(null)
  const [addingHole, setAddingHole] = useState<boolean>(false)
  const [unitOverride, setUnitOverride] = useState<string>('auto')
  const [redetectError, setRedetectError] = useState<string | null>(null)
  const [selectedSwitchIds, setSelectedSwitchIds] = useState<number[]>([])
  const [moveError, setMoveError] = useState<string | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)
  const [backendVersion, setBackendVersion] = useState<BackendVersion | null>(null)
  const [backendError, setBackendError] = useState<string | null>(null)

  useEffect(() => {
    getBackendVersion()
      .then(setBackendVersion)
      .catch((err) => setBackendError(err instanceof Error ? err.message : String(err)))
  }, [])

  function handleParsed(f: File, r: ParseResult) {
    setFile(f)
    setResult(r)
    setOriginalSwitches(r.switches.map((s) => ({ ...s })))
    setOriginalStabs(r.stabilizers.map((s) => ({ ...s })))
    setRedetectError(null)
    setSelectedSwitchIds([])
    setSelectedOutlineNodeIdx(null)
    setSelectedHoleId(null)
    setEditPlateMode(false)
    setAddingHole(false)
  }

  async function redetect(newStrategy: MatrixStrategy) {
    if (!file || newStrategy === strategy) {
      setStrategy(newStrategy)
      return
    }
    setRedetectError(null)
    try {
      const r = await parseSvg(file, newStrategy, unitOverride)
      setResult(r)
      setOriginalSwitches(r.switches.map((s) => ({ ...s })))
      setOriginalStabs(r.stabilizers.map((s) => ({ ...s })))
      setStrategy(newStrategy)
      setSelectedSwitchIds([])
    } catch (err) {
      setRedetectError(err instanceof Error ? err.message : String(err))
    }
  }

  function moveSwitches(draggedId: number, newRow: number, newCol: number) {
    if (!result) return
    setMoveError(null)
    const moving = result.switches.find((s) => s.id === draggedId)
    if (!moving) return
    if (moving.row === newRow && moving.col === newCol) return

    // Single-key path: dragged switch isn't part of the active selection.
    // If the target is occupied by another switch, INSERT — shift everyone
    // in the target row at col >= newCol one column right (growing the
    // matrix if necessary), then place the dragged switch at (newRow, newCol).
    // If the target is empty, just place.
    if (!selectedSwitchIds.includes(draggedId)) {
      const occupant = result.switches.find(
        (s) => s.row === newRow && s.col === newCol && s.id !== draggedId,
      )
      let updated: typeof result.switches
      if (occupant) {
        updated = result.switches.map((s) => {
          if (s.id === draggedId) return { ...s, row: newRow, col: newCol }
          if (s.row === newRow && s.col >= newCol && s.id !== draggedId) {
            return { ...s, col: s.col + 1 }
          }
          return s
        })
      } else {
        updated = result.switches.map((s) =>
          s.id === draggedId ? { ...s, row: newRow, col: newCol } : s,
        )
      }
      setResult({ ...result, switches: renormalizeRowCol(updated) })
      setSelectedSwitchIds([draggedId])
      return
    }

    // Group-shift path: every selected switch moves by the same (Δrow, Δcol).
    // Padding cells (negative row/col) are allowed — renormalize back to
    // non-negative indices after the move.
    const dRow = newRow - moving.row
    const dCol = newCol - moving.col
    if (dRow === 0 && dCol === 0) return

    const selectedSet = new Set(selectedSwitchIds)
    const targetCells = new Map<string, number>() // "r,c" -> switch id
    for (const sw of result.switches) {
      if (!selectedSet.has(sw.id)) continue
      const r = sw.row + dRow
      const c = sw.col + dCol
      const key = `${r},${c}`
      if (targetCells.has(key)) {
        setMoveError(`move blocked: two switches collide at (${r}, ${c}).`)
        return
      }
      targetCells.set(key, sw.id)
    }
    // Collision check vs. non-selected switches.
    for (const sw of result.switches) {
      if (selectedSet.has(sw.id)) continue
      if (targetCells.has(`${sw.row},${sw.col}`)) {
        setMoveError(
          `move blocked: cell (${sw.row}, ${sw.col}) already occupied by SW${sw.id}.`,
        )
        return
      }
    }
    const updated = result.switches.map((s) =>
      selectedSet.has(s.id) ? { ...s, row: s.row + dRow, col: s.col + dCol } : s,
    )
    setResult({ ...result, switches: renormalizeRowCol(updated) })
  }

  // Shift row/col so the minimum is back to 0 if any went negative after a
  // move into the padding region. Keeps internal indices well-defined and
  // matches the always-padded grid the matrix UI renders.
  function renormalizeRowCol(switches: SwitchDef[]): SwitchDef[] {
    if (switches.length === 0) return switches
    const minRow = Math.min(...switches.map((s) => s.row))
    const minCol = Math.min(...switches.map((s) => s.col))
    if (minRow >= 0 && minCol >= 0) return switches
    const dRow = minRow < 0 ? -minRow : 0
    const dCol = minCol < 0 ? -minCol : 0
    return switches.map((s) => ({ ...s, row: s.row + dRow, col: s.col + dCol }))
  }

  // Selection helpers — anchor = last id in the array. Empty array = nothing
  // selected (today's `null`).
  function selectSingle(id: number) {
    setMoveError(null)
    setSelectedSwitchIds([id])
  }
  function toggleInSelection(id: number) {
    setMoveError(null)
    setSelectedSwitchIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    )
  }
  function extendSelection(targetId: number) {
    setMoveError(null)
    if (!result) return
    const anchorId = selectedSwitchIds[selectedSwitchIds.length - 1]
    if (anchorId === undefined) {
      setSelectedSwitchIds([targetId])
      return
    }
    const anchor = result.switches.find((s) => s.id === anchorId)
    const target = result.switches.find((s) => s.id === targetId)
    if (!anchor || !target) return
    // Bounding-box select: anchor and target are opposite corners. Every
    // switch whose (row, col) lies inside the rect joins the selection.
    const r0 = Math.min(anchor.row, target.row)
    const r1 = Math.max(anchor.row, target.row)
    const c0 = Math.min(anchor.col, target.col)
    const c1 = Math.max(anchor.col, target.col)
    const captured = result.switches.filter(
      (s) => s.row >= r0 && s.row <= r1 && s.col >= c0 && s.col <= c1,
    )
    // Anchor stays first so subsequent shift+click pivots from it.
    const ids = captured.map((s) => s.id).filter((id) => id !== anchorId)
    setSelectedSwitchIds([anchorId, ...ids])
  }
  function clearSelection() {
    setMoveError(null)
    setSelectedSwitchIds([])
  }

  function rotateSwitch(id: number, delta: number) {
    if (!result) return
    setResult({
      ...result,
      switches: result.switches.map((s) =>
        s.id === id
          ? { ...s, rotation_deg: ((s.rotation_deg + delta) % 360 + 360) % 360 }
          : s,
      ),
    })
  }

  function flipStab(id: number) {
    if (!result) return
    setResult({
      ...result,
      stabilizers: result.stabilizers.map((s) =>
        s.id === id
          ? { ...s, rotation_deg: ((s.rotation_deg + 180) % 360 + 360) % 360 }
          : s,
      ),
    })
  }

  function updateMcu(updates: Partial<{ cx_mm: number; cy_mm: number; rotation_deg: number }>) {
    if (!result || !result.mcu_placement) return
    setResult({
      ...result,
      mcu_placement: { ...result.mcu_placement, ...updates },
    })
  }

  function setOutlineShrink(mm: number) {
    if (!result) return
    setResult({ ...result, outline_shrink_mm: Math.max(0, mm) })
  }

  // Pro Micro body dims + offsets (mm) — must match SvgPreview.tsx. The
  // body is 18 × 33 mm with pin 1 inset (-0.11, -1.5) from the top-left
  // corner. USB connector center sits at the body's width midpoint on
  // the USB-end edge — i.e. local (BODY_X + W/2, BODY_Y).
  const MCU_BODY_W_MM = 18.0
  const MCU_BODY_X_OFFSET = -0.11
  const MCU_BODY_Y_OFFSET = -1.5

  function rotateLocal(lx: number, ly: number, rotDeg: number): { x: number; y: number } {
    const r = (rotDeg * Math.PI) / 180
    const cos = Math.cos(r)
    const sin = Math.sin(r)
    return { x: lx * cos - ly * sin, y: lx * sin + ly * cos }
  }

  // USB jack reference: body's width midpoint sitting on the USB-end edge
  // — i.e. local (BODY_X + W/2, BODY_Y). Rotated by the MCU's rotation
  // and translated by its pin-1 anchor for the world coord.
  function getUsbJackWorld(): { x: number; y: number } | null {
    if (!result?.mcu_placement) return null
    const m = result.mcu_placement
    const off = rotateLocal(
      MCU_BODY_X_OFFSET + MCU_BODY_W_MM / 2,
      MCU_BODY_Y_OFFSET,
      m.rotation_deg,
    )
    return { x: m.cx_mm + off.x, y: m.cy_mm + off.y }
  }

  function setUsbJackWorld(newX: number, newY: number) {
    if (!result?.mcu_placement) return
    const m = result.mcu_placement
    const off = rotateLocal(
      MCU_BODY_X_OFFSET + MCU_BODY_W_MM / 2,
      MCU_BODY_Y_OFFSET,
      m.rotation_deg,
    )
    // anchor + off = usb → anchor = usb - off
    updateMcu({ cx_mm: newX - off.x, cy_mm: newY - off.y })
  }

  // ============================================================
  // Edit-plate mode: outline node editing + mounting hole CRUD
  // ============================================================

  function parseOutlineVerts(pathD: string): Array<{ x: number; y: number }> {
    // Tokenize an M/L/Z polygon path (the only shape our backend emits) into
    // a deduplicated vertex list. Mirrors `parsePathVertices` in SvgPreview.
    const tokens = pathD.match(/[MLHVZmlhvz]|-?\d+(?:\.\d+)?/g) ?? []
    const out: Array<{ x: number; y: number }> = []
    const seen = new Set<string>()
    let cmd = ''
    let i = 0
    while (i < tokens.length) {
      const t = tokens[i]
      if (/^[MLHVZmlhvz]$/.test(t)) {
        cmd = t.toUpperCase()
        i++
        continue
      }
      if (cmd === 'M' || cmd === 'L') {
        const x = parseFloat(tokens[i++])
        const y = parseFloat(tokens[i++])
        if (Number.isFinite(x) && Number.isFinite(y)) {
          const key = `${x.toFixed(4)},${y.toFixed(4)}`
          if (!seen.has(key)) {
            seen.add(key)
            out.push({ x, y })
          }
        }
      } else {
        i++
      }
    }
    return out
  }

  function vertsToPathD(verts: Array<{ x: number; y: number }>): string {
    if (verts.length === 0) return ''
    const [v0, ...rest] = verts
    return (
      `M ${v0.x.toFixed(4)} ${v0.y.toFixed(4)} ` +
      rest.map((v) => `L ${v.x.toFixed(4)} ${v.y.toFixed(4)}`).join(' ') +
      ' Z'
    )
  }

  function enterEditPlateMode() {
    if (!result) return
    setEditPlateMode(true)
    if (result.edited_outline_path_d) return // already initialized
    // Seed the editable polygon from the parsed outline only. `outline_shrink_mm`
    // is an independent dilation applied on top of the base outline by both
    // the backend and the SvgPreview overlay, so seeding the editable polygon
    // with the pre-grown shape would cause grow to be applied twice.
    const baseVerts = parseOutlineVerts(result.pcb_outline.path_d)
    setResult({
      ...result,
      edited_outline_path_d: vertsToPathD(baseVerts),
    })
  }

  function exitEditPlateMode() {
    setEditPlateMode(false)
    setAddingHole(false)
    setSelectedOutlineNodeIdx(null)
    setSelectedHoleId(null)
  }

  function resetEditedOutline() {
    if (!result) return
    setSelectedOutlineNodeIdx(null)
    // Re-seed the editable polygon from the parsed outline so node handles
    // stay on screen for further editing. `outline_shrink_mm` is layered on top
    // by the renderer / backend, so we deliberately don't bake it in here.
    const baseVerts = parseOutlineVerts(result.pcb_outline.path_d)
    setResult({ ...result, edited_outline_path_d: vertsToPathD(baseVerts) })
  }

  function setOutlineVerts(verts: Array<{ x: number; y: number }>) {
    if (!result) return
    setResult({ ...result, edited_outline_path_d: vertsToPathD(verts) })
  }

  function moveOutlineNode(idx: number, x: number, y: number) {
    if (!result?.edited_outline_path_d) return
    const verts = parseOutlineVerts(result.edited_outline_path_d)
    if (idx < 0 || idx >= verts.length) return
    verts[idx] = { x, y }
    setOutlineVerts(verts)
  }

  function insertOutlineNode(edgeIdx: number, x: number, y: number) {
    if (!result?.edited_outline_path_d) return
    const verts = parseOutlineVerts(result.edited_outline_path_d)
    // Insert AFTER index `edgeIdx`, so the new node becomes idx (edgeIdx + 1).
    verts.splice(edgeIdx + 1, 0, { x, y })
    setOutlineVerts(verts)
    setSelectedOutlineNodeIdx(edgeIdx + 1)
  }

  function deleteOutlineNode(idx: number) {
    if (!result?.edited_outline_path_d) return
    const verts = parseOutlineVerts(result.edited_outline_path_d)
    if (verts.length <= 3) return // keep a valid polygon
    verts.splice(idx, 1)
    setOutlineVerts(verts)
    setSelectedOutlineNodeIdx(null)
  }

  function addMountingHole(cx: number, cy: number, diameter = 3.5) {
    if (!result) return
    const nextId =
      result.mounting_holes.reduce((m, h) => Math.max(m, h.id), 0) + 1
    const hole = { id: nextId, cx_mm: cx, cy_mm: cy, diameter_mm: diameter }
    setResult({ ...result, mounting_holes: [...result.mounting_holes, hole] })
    setSelectedHoleId(nextId)
    setAddingHole(false)
  }

  function moveMountingHole(id: number, cx: number, cy: number) {
    if (!result) return
    setResult({
      ...result,
      mounting_holes: result.mounting_holes.map((h) =>
        h.id === id ? { ...h, cx_mm: cx, cy_mm: cy } : h,
      ),
    })
  }

  function setMountingHoleDiameter(id: number, diameter: number) {
    if (!result) return
    setResult({
      ...result,
      mounting_holes: result.mounting_holes.map((h) =>
        h.id === id ? { ...h, diameter_mm: Math.max(0.5, diameter) } : h,
      ),
    })
  }

  function deleteMountingHole(id: number) {
    if (!result) return
    setResult({
      ...result,
      mounting_holes: result.mounting_holes.filter((h) => h.id !== id),
    })
    if (selectedHoleId === id) setSelectedHoleId(null)
  }

  // Delete key removes the currently-selected outline node or hole while in
  // edit-plate mode.
  useEffect(() => {
    if (!editPlateMode) return
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Delete' && e.key !== 'Backspace') return
      // Don't fire when the user is typing in an input.
      const tag = (e.target as HTMLElement | null)?.tagName?.toLowerCase()
      if (tag === 'input' || tag === 'textarea') return
      if (selectedHoleId !== null) {
        e.preventDefault()
        deleteMountingHole(selectedHoleId)
      } else if (selectedOutlineNodeIdx !== null) {
        e.preventDefault()
        deleteOutlineNode(selectedOutlineNodeIdx)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [editPlateMode, selectedHoleId, selectedOutlineNodeIdx, result])

  async function setUnitOverrideAndReparse(newUnit: string) {
    setUnitOverride(newUnit)
    if (!file) return
    setRedetectError(null)
    try {
      const r = await parseSvg(file, strategy, newUnit)
      setResult(r)
      setOriginalSwitches(r.switches.map((s) => ({ ...s })))
      setOriginalStabs(r.stabilizers.map((s) => ({ ...s })))
    } catch (err) {
      setRedetectError(err instanceof Error ? err.message : String(err))
    }
  }

  function resetRotations() {
    if (!result) return
    setResult({
      ...result,
      switches: originalSwitches.map((s) => ({ ...s })),
      stabilizers: originalStabs.map((s) => ({ ...s })),
    })
  }

  const [busyExport, setBusyExport] = useState<
    null | 'net' | 'sch' | 'pcb' | 'zip' | 'plate' | 'routed-zip'
  >(null)
  // Live progress + stats for the auto-route flow. `null` while idle, an
  // object with phase/percent/stats while a route job is in flight. Cleared
  // only when the user dismisses it or starts a new route.
  const [routeProgress, setRouteProgress] = useState<RouteJobStatus | null>(null)
  const routePollRef = useRef<{ cancelled: boolean } | null>(null)

  async function downloadFile(
    kind: 'net' | 'sch' | 'pcb' | 'plate',
    fetcher: () => Promise<string>,
    extension: string,
  ): Promise<void> {
    if (!result) return
    setExportError(null)
    setBusyExport(kind)
    try {
      const text = await fetcher()
      const blob = new Blob([text], { type: 'text/plain' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      const baseName = file?.name.replace(/\.svg$/i, '') ?? 'keyboard'
      a.download = `${baseName}.${extension}`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (err) {
      setExportError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusyExport(null)
    }
  }

  function downloadNetlist() {
    return downloadFile('net', () => generateNetlist(result!.switches, rgb), 'net')
  }

  function downloadSchematic() {
    return downloadFile(
      'sch',
      () => generateSchematic(result!.switches, switchType, diodeType, groundPour, rgb),
      'kicad_sch',
    )
  }

  function downloadPcb() {
    return downloadFile(
      'pcb',
      () => generatePcb(result!, switchType, diodeType, stabilizerType, groundPour, rgb),
      'kicad_pcb',
    )
  }

  function downloadPlateSvg() {
    return downloadFile(
      'plate',
      () => generatePlateSvg(result!),
      'plate.svg',
    )
  }

  async function downloadProjectZip() {
    if (!result || !file) return
    setExportError(null)
    setBusyExport('zip')
    try {
      const baseName = file.name.replace(/\.svg$/i, '') || 'keyboard'
      const blob = await generateProjectZip(
        result,
        baseName,
        switchType,
        diodeType,
        stabilizerType,
        groundPour,
        rgb,
      )
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${baseName}-project.zip`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (err) {
      setExportError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusyExport(null)
    }
  }

  // Auto-routed project: kick off a backend job, poll until done, then
  // trigger a blob download. The existing unrouted button stays unchanged
  // so the user keeps a fast path that doesn't depend on the freerouting
  // sidecar.
  async function downloadRoutedProjectZip() {
    if (!result || !file) return
    setExportError(null)
    setBusyExport('routed-zip')
    setRouteProgress({
      job_id: '',
      state: 'pending',
      phase: 'starting',
      percent: 0,
    })
    // Cancellation token — if the user navigates away or starts a new
    // route, we set cancelled=true so the in-flight poll loop bails
    // without touching state.
    const token = { cancelled: false }
    routePollRef.current = token
    const baseName = file.name.replace(/\.svg$/i, '') || 'keyboard'
    try {
      const job = await startRoutedProject(
        result, baseName, switchType, diodeType, stabilizerType, groundPour, rgb,
      )
      while (!token.cancelled) {
        await new Promise((r) => setTimeout(r, 500))
        if (token.cancelled) return
        const status = await getRouteJob(job.job_id)
        if (token.cancelled) return
        setRouteProgress(status)
        if (status.state === 'done') break
        if (status.state === 'failed') {
          throw new Error(status.error || 'auto-routing failed')
        }
      }
      if (token.cancelled) return
      const blob = await downloadRoutedProjectResult(job.job_id)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${baseName}-routed-project.zip`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (err) {
      setExportError(err instanceof Error ? err.message : String(err))
    } finally {
      if (!token.cancelled) setBusyExport(null)
      if (routePollRef.current === token) routePollRef.current = null
    }
  }

  // Cancel any in-flight polling loop on unmount.
  useEffect(() => {
    return () => {
      if (routePollRef.current) routePollRef.current.cancelled = true
    }
  }, [])

  function rotateAllSwitches(delta: number) {
    if (!result) return
    setResult({
      ...result,
      switches: result.switches.map((s) => ({
        ...s,
        rotation_deg: ((s.rotation_deg + delta) % 360 + 360) % 360,
      })),
    })
  }

  return (
    <main className="app">
      <header>
        <h1>Keyboard KiCad AutoDesigner</h1>
        <p className="tag">
          Upload a plate SVG. Click any switch dot to rotate it (shift = −90°).
          Click a stabilizer to flip its head. Drag matrix cells to fix
          row/col assignments.
        </p>
      </header>

      <UploadStep onParsed={handleParsed} strategy={strategy} />

      {file && result && (
        <>
          <OrientationHelp />
          <div className="toolbar toolbar-strategy">
            <span className="toolbar-label">Matrix:</span>
            <button
              className={strategy === 'auto' ? 'active' : ''}
              onClick={() => redetect('auto')}
              title="Try all three strategies on the plate and pick the one whose rows × cols is closest to square."
            >
              Detect: auto
            </button>
            <button
              className={strategy === 'row_first' ? 'active' : ''}
              onClick={() => redetect('row_first')}
              title="Group by Y first — best for axis-aligned layouts (kbplate)."
            >
              Detect: row-first
            </button>
            <button
              className={strategy === 'column_first' ? 'active' : ''}
              onClick={() => redetect('column_first')}
              title="Group by X first — splits split keyboards into hand-clusters."
            >
              Detect: column-first
            </button>
            <button
              className={strategy === 'stagger_aware' ? 'active' : ''}
              onClick={() => redetect('stagger_aware')}
              title="Chain switches column-by-column using each switch's own rotation — best for Dactyl-style finger-column staggers."
            >
              Detect: stagger-aware
            </button>
            {strategy === 'auto' && result?.matrix_strategy && (
              <span className="hint">→ {result.matrix_strategy.replace('_', '-')}</span>
            )}
            {redetectError && <span className="err">{redetectError}</span>}
          </div>
          <div className="toolbar">
            <button onClick={() => rotateAllSwitches(90)}>Rotate switches +90°</button>
            <button onClick={() => rotateAllSwitches(-90)}>Rotate switches −90°</button>
            <button onClick={resetRotations}>Reset to detected</button>
            <span className="hint">
              Click a switch: +90° (shift = −90°). Click a stab: flip 180°.
            </span>
          </div>
          <div className="toolbar toolbar-strategy">
            <span className="toolbar-label">Switches:</span>
            <button
              className={switchType === 'soldered' ? 'active' : ''}
              onClick={() => setSwitchType('soldered')}
              title="Cherry MX PCB-mount switches soldered directly to the board (2× thru-hole signal pads)."
            >
              Soldered MX
            </button>
            <button
              className={switchType === 'hotswap' ? 'active' : ''}
              onClick={() => setSwitchType('hotswap')}
              title="Kailh CPG151101S11 hotswap socket on B.Cu. Switches drop in and out without soldering. Pads sized to community-standard MX_Alps_Hybrid geometry."
            >
              Kailh hotswap
            </button>
          </div>
          <div className="toolbar toolbar-strategy">
            <span className="toolbar-label">Diodes:</span>
            <button
              className={diodeType === 'tht' ? 'active' : ''}
              onClick={() => setDiodeType('tht')}
              title="1N4148 through-hole diode (DO-35) placed adjacent to each switch on F.Cu."
            >
              THT (DO-35)
            </button>
            <button
              className={diodeType === 'smd' ? 'active' : ''}
              onClick={() => setDiodeType('smd')}
              title="1N4148 SMD diode (SOD-123) placed at switch center on B.Cu — tucks under the switch, no offset needed."
            >
              SMD (SOD-123)
            </button>
          </div>
          <div className="toolbar toolbar-strategy">
            <span className="toolbar-label">Stabilizers:</span>
            <button
              className={stabilizerType === 'pcb_mount' ? 'active' : ''}
              onClick={() => setStabilizerType('pcb_mount')}
              title="Cherry MX PCB-mount stabilizer: 4 NPTH holes per stab (wire + housing on each side) at the canonical Cherry offsets, anchored on the switch stem. Snap-in and screw-in stabs share the same PCB hole pattern."
            >
              PCB-mount
            </button>
            <button
              className={stabilizerType === 'plate_mount' ? 'active' : ''}
              onClick={() => setStabilizerType('plate_mount')}
              title="Plate-mount stabilizer clips into the plate only. PCB gets an F.Cu footprint-keepout zone under the stab — no drills; tracks and vias still allowed underneath."
            >
              Plate-mount
            </button>
          </div>
          <div className="toolbar toolbar-strategy">
            <span className="toolbar-label">Ground pour:</span>
            <button
              className={groundPour ? 'active' : ''}
              onClick={() => setGroundPour(true)}
              title="GND copper pours on both layers, stitched together with vias on a 15 mm grid and tied to the Pro Micro's ground pins. Zones ship unfilled — press B in KiCad to fill them."
            >
              On
            </button>
            <button
              className={!groundPour ? 'active' : ''}
              onClick={() => setGroundPour(false)}
              title="No ground plane — bare board with matrix traces only, MCU ground pins left unconnected."
            >
              Off
            </button>
          </div>
          <div className="toolbar toolbar-strategy">
            <span className="toolbar-label">RGB LEDs:</span>
            <button
              className={rgb ? 'active' : ''}
              onClick={() => setRgb(true)}
              title="Per-key SK6812 MINI-E reverse-mount RGB on the board's back, shining through a milled cutout under each switch (south-facing). DIN→DOUT daisy-chain from a free Pro Micro GPIO, 100nF cap per LED, power from RAW (USB 5V)."
            >
              On
            </button>
            <button
              className={!rgb ? 'active' : ''}
              onClick={() => setRgb(false)}
              title="No per-key RGB."
            >
              Off
            </button>
          </div>
          {result.mcu_placement && (
            <div className="toolbar toolbar-strategy">
              <span className="toolbar-label">MCU (Pro Micro):</span>
              <label className="toolbar-input">
                X
                <NumberInput
                  step={0.1}
                  value={result.mcu_placement.cx_mm}
                  onChange={(n) => updateMcu({ cx_mm: n })}
                  title="X position (mm) of Pro Micro pin 1 (USB end). Drag the marker on the preview for coarse placement, fine-tune here."
                />
              </label>
              <label className="toolbar-input">
                Y
                <NumberInput
                  step={0.1}
                  value={result.mcu_placement.cy_mm}
                  onChange={(n) => updateMcu({ cy_mm: n })}
                  title="Y position (mm) of Pro Micro pin 1 (USB end)."
                />
              </label>
              <label className="toolbar-input">
                Rotation
                <NumberInput
                  step={1}
                  decimals={1}
                  value={result.mcu_placement.rotation_deg}
                  onChange={(n) => updateMcu({ rotation_deg: n })}
                  title="Rotation in degrees, SVG convention (clockwise positive). Pin 1 / USB end of the marker faces the direction USB will exit."
                />
              </label>
              {(() => {
                const usb = getUsbJackWorld()
                if (!usb) return null
                return (
                  <>
                    <label className="toolbar-input">
                      USB X
                      <NumberInput
                        step={0.1}
                        value={usb.x}
                        onChange={(n) => setUsbJackWorld(n, usb.y)}
                        title="X coordinate (mm) of the USB-jack center. Computed from the pin-1 anchor + the module body width (17.78 mm) under the current rotation. Editing this slides the entire MCU so the USB lands at the entered X."
                      />
                    </label>
                    <label className="toolbar-input">
                      USB Y
                      <NumberInput
                        step={0.1}
                        value={usb.y}
                        onChange={(n) => setUsbJackWorld(usb.x, n)}
                        title="Y coordinate (mm) of the USB-jack center — sits on the pin-1 edge of the module body. Editing this slides the entire MCU so the USB lands at the entered Y."
                      />
                    </label>
                  </>
                )
              })()}
            </div>
          )}
          <div className="toolbar toolbar-strategy">
            <span className="toolbar-label">Tools:</span>
            <button
              className={inspectMode ? 'active' : ''}
              onClick={() => setInspectMode(!inspectMode)}
              title="Inspect mode: hover anywhere on the preview to read X/Y coordinates. Snaps to rectangular-feature corners (plate / switch / stab / MCU) and to mounting-hole centers (with diameter). Hold Alt for a temporary peek without toggling."
            >
              Inspect {inspectMode ? '(on)' : ''}
            </button>
          </div>
          <div className="toolbar toolbar-strategy">
            <span className="toolbar-label">PCB inset:</span>
            <label className="toolbar-input">
              <NumberInput
                step={0.5}
                min={0}
                max={10}
                decimals={1}
                value={result.outline_shrink_mm}
                onChange={(n) => setOutlineShrink(n)}
                title="Shrink the PCB outline N mm inside the plate outline on all sides — most assemblies need the PCB the same size or smaller than the plate. The plate SVG export keeps the original outline. Generation fails if any pad ends up within 0.5 mm of the new PCB edge."
              />
              <span className="toolbar-unit">mm</span>
            </label>
            <button
              onClick={downloadPlateSvg}
              disabled={busyExport !== null}
              title="Export a clean plate SVG with the plate outline (unaffected by PCB inset) and all switch / stab / mounting-hole cutouts. Hairline-stroked, fill-less — ready for laser cutting."
            >
              {busyExport === 'plate' ? 'Generating…' : 'Download plate SVG'}
            </button>
          </div>
          <div className="toolbar toolbar-strategy">
            <span className="toolbar-label">Plate edit:</span>
            <button
              className={editPlateMode ? 'active' : ''}
              onClick={() =>
                editPlateMode ? exitEditPlateMode() : enterEditPlateMode()
              }
              title="Edit the plate outline (drag, add, or delete vertices) and mounting holes. The PCB inset is applied on top of whatever you draw here."
            >
              {editPlateMode ? 'Editing (exit)' : 'Edit plate'}
            </button>
            {editPlateMode && (
              <>
                <button
                  className={addingHole ? 'active' : ''}
                  onClick={() => setAddingHole(!addingHole)}
                  title="Arm hole-placement: the next click on the preview adds a mounting hole at that point."
                >
                  + Hole
                </button>
                {result.edited_outline_path_d && (
                  <button
                    onClick={resetEditedOutline}
                    title="Discard outline edits and revert to the parsed outline (the PCB inset still applies on top)."
                  >
                    Reset outline
                  </button>
                )}
                {selectedOutlineNodeIdx !== null && (() => {
                  const verts = result.edited_outline_path_d
                    ? parseOutlineVerts(result.edited_outline_path_d)
                    : []
                  const v = verts[selectedOutlineNodeIdx]
                  if (!v) return null
                  return (
                    <>
                      <label className="toolbar-input">
                        Node X
                        <NumberInput
                          step={0.1}
                          value={v.x}
                          onChange={(n) =>
                            moveOutlineNode(selectedOutlineNodeIdx, n, v.y)
                          }
                        />
                      </label>
                      <label className="toolbar-input">
                        Y
                        <NumberInput
                          step={0.1}
                          value={v.y}
                          onChange={(n) =>
                            moveOutlineNode(selectedOutlineNodeIdx, v.x, n)
                          }
                        />
                      </label>
                    </>
                  )
                })()}
                {selectedHoleId !== null && (() => {
                  const h = result.mounting_holes.find(
                    (m) => m.id === selectedHoleId,
                  )
                  if (!h) return null
                  return (
                    <>
                      <label className="toolbar-input">
                        Hole ⌀
                        <NumberInput
                          step={0.1}
                          min={0.5}
                          value={h.diameter_mm}
                          onChange={(n) => setMountingHoleDiameter(h.id, n)}
                        />
                        <span className="toolbar-unit">mm</span>
                      </label>
                      <button
                        onClick={() => deleteMountingHole(h.id)}
                        title="Delete this mounting hole (Delete / Backspace also works)."
                      >
                        Delete hole
                      </button>
                    </>
                  )
                })()}
              </>
            )}
            <span className="toolbar-label" style={{ marginLeft: 18 }}>
              Unit:
            </span>
            <select
              value={unitOverride}
              onChange={(e) => setUnitOverrideAndReparse(e.target.value)}
              title={`Detected SVG unit: ${result.detected_svg_unit} (×${result.mm_per_unit} mm/unit). Override here if the heuristic picked wrong — re-parses the file under the new unit.`}
            >
              <option value="auto">Auto ({result.detected_svg_unit})</option>
              <option value="mm">mm</option>
              <option value="cm">cm</option>
              <option value="in">in</option>
              <option value="pt">pt</option>
              <option value="pc">pc</option>
            </select>
          </div>
          <div className="toolbar toolbar-export">
            <span className="toolbar-label">Export:</span>
            <button
              className="primary"
              onClick={downloadProjectZip}
              disabled={busyExport !== null}
              title="Download a complete KiCad project (.zip) — .kicad_pro + .kicad_sch + .kicad_pcb. Extract and double-click the .kicad_pro to open everything in KiCad."
            >
              {busyExport === 'zip'
                ? 'Generating…'
                : 'Download KiCad project (.zip)'}
            </button>
            <button
              className="primary"
              onClick={downloadRoutedProjectZip}
              disabled={busyExport !== null}
              title="Auto-route the board with Freerouting before zipping. Takes 10–60s depending on board complexity. The plain Download button next to this stays available as a fast unrouted fallback."
            >
              {busyExport === 'routed-zip'
                ? routedButtonLabel(routeProgress)
                : 'Download routed project (.zip)'}
            </button>
            <button
              onClick={downloadSchematic}
              disabled={busyExport !== null}
              title="Just the schematic file (.kicad_sch) generated via SKiDL."
            >
              {busyExport === 'sch' ? 'Generating…' : 'Schematic (.kicad_sch)'}
            </button>
            <button
              onClick={downloadPcb}
              disabled={busyExport !== null}
              title="Just the PCB layout (.kicad_pcb) — switches placed at their detected coords with diodes, mounting holes, edge cuts, and matrix routing."
            >
              {busyExport === 'pcb' ? 'Generating…' : 'PCB (.kicad_pcb)'}
            </button>
            <button
              onClick={downloadNetlist}
              disabled={busyExport !== null}
              title="Just the legacy netlist (.net) — switches + diodes + header. Import into KiCad PCB via Tools → Update PCB from Netlist."
            >
              {busyExport === 'net' ? 'Generating…' : 'Netlist (.net)'}
            </button>
            {exportError && <span className="err">{exportError}</span>}
          </div>
          {routeProgress && (
            <RouteProgressBanner
              status={routeProgress}
              onDismiss={() => setRouteProgress(null)}
            />
          )}
          <SvgPreview
            file={file}
            result={result}
            selectedSwitchIds={selectedSwitchIds}
            onRotateSwitch={rotateSwitch}
            onSelectSwitch={(id) => (id === null ? clearSelection() : selectSingle(id))}
            onFlipStab={flipStab}
            onMcuMove={(cx, cy) =>
              updateMcu({ cx_mm: Math.round(cx * 1000) / 1000, cy_mm: Math.round(cy * 1000) / 1000 })
            }
            inspectMode={inspectMode}
            editPlateMode={editPlateMode}
            addingHole={addingHole}
            selectedOutlineNodeIdx={selectedOutlineNodeIdx}
            selectedHoleId={selectedHoleId}
            onSelectOutlineNode={setSelectedOutlineNodeIdx}
            onSelectHole={setSelectedHoleId}
            onMoveOutlineNode={(idx, x, y) =>
              moveOutlineNode(idx, Math.round(x * 1000) / 1000, Math.round(y * 1000) / 1000)
            }
            onInsertOutlineNode={(edgeIdx, x, y) =>
              insertOutlineNode(edgeIdx, Math.round(x * 1000) / 1000, Math.round(y * 1000) / 1000)
            }
            onAddHole={(x, y) =>
              addMountingHole(Math.round(x * 1000) / 1000, Math.round(y * 1000) / 1000)
            }
            onMoveHole={(id, x, y) =>
              moveMountingHole(id, Math.round(x * 1000) / 1000, Math.round(y * 1000) / 1000)
            }
          />
          <MatrixGrid
            result={result}
            selectedSwitchIds={selectedSwitchIds}
            onSelectClick={(id, mode) => {
              if (mode === 'extend') extendSelection(id)
              else if (mode === 'toggle') toggleInSelection(id)
              else if (
                selectedSwitchIds.length === 1 &&
                selectedSwitchIds[0] === id
              ) {
                // Plain click on the lone selected cell → deselect.
                clearSelection()
              } else {
                selectSingle(id)
              }
            }}
            onClearSelection={clearSelection}
            onMoveSwitch={moveSwitches}
            moveError={moveError}
          />
        </>
      )}

      <footer className="version-footer">
        <span>
          frontend v{__APP_VERSION__} · built {__BUILD_TIME__}
        </span>
        <span>
          {backendVersion
            ? `backend v${backendVersion.version} · built ${backendVersion.built_at}`
            : backendError
              ? `backend: ${backendError}`
              : 'backend: …'}
        </span>
      </footer>
    </main>
  )
}
