import { useEffect, useMemo, useRef, useState } from 'react'
import type { MouseEvent, PointerEvent as ReactPointerEvent } from 'react'
import type { ParseResult, SwitchDef } from '../types'

interface Props {
  file: File
  result: ParseResult
  selectedSwitchIds: number[]
  onRotateSwitch: (id: number, deltaDeg: number) => void
  onSelectSwitch: (id: number | null) => void
  onFlipStab: (id: number) => void
  onMcuMove?: (cx: number, cy: number) => void
  inspectMode?: boolean
}

// Pro Micro module body dimensions in mm, matching the backend footprint.
// Pin 1 is the anchor (top-left in local frame); the USB connector sits at
// local Y = 0 — the pin-1 short edge.
const MCU_BODY_W_MM = 17.78
const MCU_BODY_H_MM = 27.94
const MCU_USB_NOTCH_MM = 4.0

// Inspect mode: how close (in world mm) the cursor must be to a feature
// before the readout snaps to it.
const SNAP_RADIUS_MM = 3.0
// Cherry MX body is 14×14 mm; matches the backend's _fab_switch_body outline.
const SWITCH_BODY_MM = 14.0

type SnapTarget =
  | { kind: 'corner'; x: number; y: number; label: string }
  | { kind: 'center'; x: number; y: number; label: string; diameter: number }

function rotateAroundOrigin(x: number, y: number, rotDeg: number): { x: number; y: number } {
  const r = (rotDeg * Math.PI) / 180
  const cos = Math.cos(r)
  const sin = Math.sin(r)
  return { x: x * cos - y * sin, y: x * sin + y * cos }
}

function centerRectCorners(
  cx: number, cy: number, w: number, h: number, rotDeg: number,
): Array<{ x: number; y: number }> {
  const hw = w / 2
  const hh = h / 2
  return [
    { lx: -hw, ly: -hh },
    { lx:  hw, ly: -hh },
    { lx:  hw, ly:  hh },
    { lx: -hw, ly:  hh },
  ].map(({ lx, ly }) => {
    const r = rotateAroundOrigin(lx, ly, rotDeg)
    return { x: cx + r.x, y: cy + r.y }
  })
}

function anchoredRectCorners(
  ax: number, ay: number, w: number, h: number, rotDeg: number,
): Array<{ x: number; y: number }> {
  // Anchor at the top-left of the local rect (matches the MCU footprint
  // convention: pin 1 at local (0, 0), body extends to (+W, +H)).
  return [
    { lx: 0, ly: 0 },
    { lx: w, ly: 0 },
    { lx: w, ly: h },
    { lx: 0, ly: h },
  ].map(({ lx, ly }) => {
    const r = rotateAroundOrigin(lx, ly, rotDeg)
    return { x: ax + r.x, y: ay + r.y }
  })
}

function parsePathVertices(pathD: string): Array<{ x: number; y: number }> {
  // Tokenize SVG path commands + numbers. The backend currently emits only
  // M / L / Z (see `_rect_path`); we still tolerate H/V/h/v/m/l skipped pairs
  // so a future polygonal outline doesn't silently lose vertices.
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
      // Skip a value for unsupported commands (H/V) so we don't desync.
      i++
    }
  }
  return out
}

// Warm hues for row groupings, cool hues for columns. Cycled by index;
// adjacent rows/columns get visibly distinct colors.
const ROW_COLORS = [
  '#e07050', '#d68030', '#c4992a', '#a8a830',
  '#d04070', '#b85050', '#cc6020', '#9c7020',
]
const COL_COLORS = [
  '#3088c0', '#4070b0', '#5060c8', '#3098a8',
  '#5078d0', '#3878a0', '#4060c0', '#2898b8',
]

function groupBy<T, K extends string | number>(
  items: T[],
  key: (item: T) => K,
): Map<K, T[]> {
  const out = new Map<K, T[]>()
  for (const item of items) {
    const k = key(item)
    const bucket = out.get(k)
    if (bucket) bucket.push(item)
    else out.set(k, [item])
  }
  return out
}

function pointsAttr(switches: SwitchDef[]): string {
  return switches.map((s) => `${s.cx_mm},${s.cy_mm}`).join(' ')
}

export function SvgPreview({
  file,
  result,
  selectedSwitchIds,
  onRotateSwitch,
  onSelectSwitch,
  onFlipStab,
  onMcuMove,
  inspectMode = false,
}: Props) {
  const [svgUrl, setSvgUrl] = useState<string | null>(null)
  const overlayRef = useRef<SVGSVGElement | null>(null)
  const mcuDragRef = useRef<{ pointerId: number; offsetX: number; offsetY: number } | null>(null)
  const [altHeld, setAltHeld] = useState(false)
  const [inspectInfo, setInspectInfo] = useState<{
    cursor: { x: number; y: number }
    target: SnapTarget | null
  } | null>(null)

  const effectiveInspect = inspectMode || altHeld

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Alt') setAltHeld(true)
    }
    function onKeyUp(e: KeyboardEvent) {
      if (e.key === 'Alt') setAltHeld(false)
    }
    function onBlur() {
      setAltHeld(false)
    }
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('keyup', onKeyUp)
    window.addEventListener('blur', onBlur)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('keyup', onKeyUp)
      window.removeEventListener('blur', onBlur)
    }
  }, [])

  useEffect(() => {
    if (!effectiveInspect) setInspectInfo(null)
  }, [effectiveInspect])

  useEffect(() => {
    const url = URL.createObjectURL(file)
    setSvgUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [file])

  const { svg_width_mm: w, svg_height_mm: h } = result
  // Expand viewBox so the MCU marker and grown outline render even when they
  // sit outside the plate's natural bounds.
  const padded = useMemo(() => {
    let xmin = 0
    let ymin = 0
    let xmax = w
    let ymax = h
    if (result.outline_grow_mm > 0) {
      xmin = Math.min(xmin, -result.outline_grow_mm)
      ymin = Math.min(ymin, -result.outline_grow_mm)
      xmax = Math.max(xmax, w + result.outline_grow_mm)
      ymax = Math.max(ymax, h + result.outline_grow_mm)
    }
    if (result.mcu_placement) {
      // Conservative: max possible extent of the body around the anchor under
      // any rotation = diagonal of the bounding rect.
      const r = Math.hypot(MCU_BODY_W_MM, MCU_BODY_H_MM + MCU_USB_NOTCH_MM)
      xmin = Math.min(xmin, result.mcu_placement.cx_mm - r)
      ymin = Math.min(ymin, result.mcu_placement.cy_mm - r)
      xmax = Math.max(xmax, result.mcu_placement.cx_mm + r)
      ymax = Math.max(ymax, result.mcu_placement.cy_mm + r)
    }
    return { xmin, ymin, w: xmax - xmin, h: ymax - ymin }
  }, [w, h, result.outline_grow_mm, result.mcu_placement])
  const viewBox = `${padded.xmin} ${padded.ymin} ${padded.w} ${padded.h}`
  const dotR = Math.min(w, h) * 0.012
  const tickLen = Math.min(w, h) * 0.045
  const stroke = Math.min(w, h) * 0.004
  const matrixStroke = Math.min(w, h) * 0.006

  const rowLines = useMemo(() => {
    const groups = groupBy(result.switches, (s) => s.row)
    return [...groups.entries()]
      .sort(([a], [b]) => a - b)
      .map(([row, members]) => ({
        row,
        switches: [...members].sort((a, b) => a.col - b.col),
      }))
  }, [result.switches])

  const colLines = useMemo(() => {
    const groups = groupBy(result.switches, (s) => s.col)
    return [...groups.entries()]
      .sort(([a], [b]) => a - b)
      .map(([col, members]) => ({
        col,
        switches: [...members].sort((a, b) => a.row - b.row),
      }))
  }, [result.switches])

  function handleSwitchClick(e: MouseEvent<SVGElement>, id: number) {
    if (effectiveInspect) return
    e.preventDefault()
    e.stopPropagation()
    const delta = e.shiftKey ? -90 : 90
    onRotateSwitch(id, delta)
    onSelectSwitch(id)
  }

  function handleStabClick(e: MouseEvent<SVGElement>, id: number) {
    if (effectiveInspect) return
    e.preventDefault()
    e.stopPropagation()
    onFlipStab(id)
  }

  function clientToMm(e: ReactPointerEvent<SVGElement> | PointerEvent): { x: number; y: number } | null {
    const svg = overlayRef.current
    if (!svg) return null
    const rect = svg.getBoundingClientRect()
    // The viewBox is "{xmin} {ymin} {w} {h}" with preserveAspectRatio="xMidYMid meet".
    const scale = Math.min(rect.width / padded.w, rect.height / padded.h)
    const drawnW = padded.w * scale
    const drawnH = padded.h * scale
    const padX = (rect.width - drawnW) / 2
    const padY = (rect.height - drawnH) / 2
    const localX = padded.xmin + (e.clientX - rect.left - padX) / scale
    const localY = padded.ymin + (e.clientY - rect.top - padY) / scale
    return { x: localX, y: localY }
  }

  function handleMcuPointerDown(e: ReactPointerEvent<SVGElement>) {
    if (effectiveInspect) return
    if (!onMcuMove || !result.mcu_placement) return
    const pos = clientToMm(e)
    if (!pos) return
    e.stopPropagation()
    e.preventDefault()
    mcuDragRef.current = {
      pointerId: e.pointerId,
      offsetX: pos.x - result.mcu_placement.cx_mm,
      offsetY: pos.y - result.mcu_placement.cy_mm,
    }
    e.currentTarget.setPointerCapture(e.pointerId)
  }

  function handleMcuPointerMove(e: ReactPointerEvent<SVGElement>) {
    const drag = mcuDragRef.current
    if (!drag || drag.pointerId !== e.pointerId) return
    if (!onMcuMove) return
    const pos = clientToMm(e)
    if (!pos) return
    onMcuMove(pos.x - drag.offsetX, pos.y - drag.offsetY)
  }

  function handleMcuPointerUp(e: ReactPointerEvent<SVGElement>) {
    if (mcuDragRef.current?.pointerId === e.pointerId) {
      mcuDragRef.current = null
      try {
        e.currentTarget.releasePointerCapture(e.pointerId)
      } catch {
        // ignore
      }
    }
  }

  // Inspect-mode snap targets: corners of every rectangular feature + centers
  // of every circular feature. Built once per result; the hover handler scans
  // it for the nearest target within SNAP_RADIUS_MM.
  const snapTargets = useMemo<SnapTarget[]>(() => {
    const out: SnapTarget[] = []
    // Plate outline: every vertex on the path. The four extreme bbox corners
    // get the "Plate corner" label; any intermediate vertices (for non-rect
    // outlines) are "Plate node N" so the user can still snap to them.
    const verts = parsePathVertices(result.pcb_outline.path_d)
    if (verts.length > 0) {
      const xs = verts.map(v => v.x)
      const ys = verts.map(v => v.y)
      const xmin = Math.min(...xs)
      const xmax = Math.max(...xs)
      const ymin = Math.min(...ys)
      const ymax = Math.max(...ys)
      verts.forEach((v, idx) => {
        const isCorner =
          (v.x === xmin || v.x === xmax) && (v.y === ymin || v.y === ymax)
        out.push({
          kind: 'corner',
          x: v.x,
          y: v.y,
          label: isCorner ? 'Plate corner' : `Plate node ${idx + 1}`,
        })
      })
      // Grown outline (only when growth > 0). Today the backend always emits
      // a rectangular path so a bbox dilation is exact; for future non-rect
      // outlines this falls back to the dilated bbox of the same vertices.
      if (result.outline_grow_mm > 0) {
        const g = result.outline_grow_mm
        for (const [x, y] of [
          [xmin - g, ymin - g],
          [xmax + g, ymin - g],
          [xmax + g, ymax + g],
          [xmin - g, ymax + g],
        ]) {
          out.push({ kind: 'corner', x, y, label: 'Grown outline corner' })
        }
      }
    }
    // Switch fab corners (14×14 mm centered on switch, rotated).
    for (const s of result.switches) {
      for (const c of centerRectCorners(s.cx_mm, s.cy_mm, SWITCH_BODY_MM, SWITCH_BODY_MM, s.rotation_deg)) {
        out.push({ kind: 'corner', x: c.x, y: c.y, label: `SW${s.id} corner` })
      }
    }
    // Stabilizer cutout corners.
    for (const s of result.stabilizers) {
      for (const c of centerRectCorners(s.cx_mm, s.cy_mm, s.width_mm, s.height_mm, s.rotation_deg)) {
        out.push({ kind: 'corner', x: c.x, y: c.y, label: `Stab${s.id} corner` })
      }
    }
    // MCU body corners — anchored at pin 1 (top-left of local frame), so
    // corners walk (0,0)→(W,0)→(W,H)→(0,H) before rotation.
    if (result.mcu_placement) {
      const m = result.mcu_placement
      for (const c of anchoredRectCorners(m.cx_mm, m.cy_mm, MCU_BODY_W_MM, MCU_BODY_H_MM, m.rotation_deg)) {
        out.push({ kind: 'corner', x: c.x, y: c.y, label: 'MCU corner' })
      }
      // Pin 1 / USB anchor itself — also useful to snap to.
      out.push({ kind: 'corner', x: m.cx_mm, y: m.cy_mm, label: 'MCU pin 1 (USB)' })
    }
    // Mounting hole centers.
    for (const h of result.mounting_holes) {
      out.push({
        kind: 'center', x: h.cx_mm, y: h.cy_mm,
        label: `MH${h.id}`, diameter: h.diameter_mm,
      })
    }
    return out
  }, [result])

  function findNearestSnap(x: number, y: number): SnapTarget | null {
    let best: SnapTarget | null = null
    let bestD2 = SNAP_RADIUS_MM * SNAP_RADIUS_MM
    for (const t of snapTargets) {
      const d2 = (t.x - x) ** 2 + (t.y - y) ** 2
      if (d2 < bestD2) {
        bestD2 = d2
        best = t
      }
    }
    return best
  }

  function handleOverlayPointerMove(e: ReactPointerEvent<SVGSVGElement>) {
    if (!effectiveInspect) return
    const pos = clientToMm(e)
    if (!pos) return
    setInspectInfo({ cursor: pos, target: findNearestSnap(pos.x, pos.y) })
  }

  function handleOverlayPointerLeave() {
    setInspectInfo(null)
  }

  // Convert a world-mm point back to a pixel position inside .preview-stage,
  // matching the SVG's preserveAspectRatio="xMidYMid meet" layout. Used to
  // position the HTML tooltip and corner-coord box on top of the SVG overlay.
  function mmToStagePixel(x: number, y: number): { left: number; top: number } | null {
    const svg = overlayRef.current
    if (!svg) return null
    const rect = svg.getBoundingClientRect()
    const scale = Math.min(rect.width / padded.w, rect.height / padded.h)
    const drawnW = padded.w * scale
    const drawnH = padded.h * scale
    const padX = (rect.width - drawnW) / 2
    const padY = (rect.height - drawnH) / 2
    return {
      left: padX + (x - padded.xmin) * scale,
      top: padY + (y - padded.ymin) * scale,
    }
  }

  // Grown outline: parse the original rect path for its bounds, then expand
  // by outline_grow_mm on all four sides. Only renders when growth is > 0.
  const grownOutline = useMemo(() => {
    const grow = result.outline_grow_mm
    if (!grow || grow <= 0) return null
    const nums = result.pcb_outline.path_d.match(/-?\d+(?:\.\d+)?/g)?.map(Number) ?? []
    if (nums.length < 8) return null
    const xs = [nums[0], nums[2], nums[4], nums[6]]
    const ys = [nums[1], nums[3], nums[5], nums[7]]
    return {
      x0: Math.min(...xs) - grow,
      y0: Math.min(...ys) - grow,
      x1: Math.max(...xs) + grow,
      y1: Math.max(...ys) + grow,
    }
  }, [result.outline_grow_mm, result.pcb_outline.path_d])

  return (
    <div className="preview">
      <div className="preview-stage" style={{ aspectRatio: `${padded.w} / ${padded.h}` }}>
        {svgUrl && (
          <img
            className="preview-img"
            src={svgUrl}
            alt="Plate preview"
            draggable={false}
            style={{
              // Position the plate image within the (possibly padded) stage
              // so MCU markers / grown outlines outside the plate render in
              // the surrounding padded region.
              left: `${((-padded.xmin) / padded.w) * 100}%`,
              top: `${((-padded.ymin) / padded.h) * 100}%`,
              width: `${(w / padded.w) * 100}%`,
              height: `${(h / padded.h) * 100}%`,
              inset: 'auto',
            }}
          />
        )}
        <svg
          ref={overlayRef}
          className="preview-overlay"
          viewBox={viewBox}
          preserveAspectRatio="xMidYMid meet"
          xmlns="http://www.w3.org/2000/svg"
          style={{ cursor: effectiveInspect ? 'crosshair' : undefined }}
          onPointerMove={handleOverlayPointerMove}
          onPointerLeave={handleOverlayPointerLeave}
        >
          {grownOutline && (
            <rect
              x={grownOutline.x0}
              y={grownOutline.y0}
              width={grownOutline.x1 - grownOutline.x0}
              height={grownOutline.y1 - grownOutline.y0}
              fill="rgba(220, 50, 50, 0.04)"
              stroke="rgba(220, 50, 50, 0.55)"
              strokeWidth={stroke * 0.8}
              strokeDasharray={`${stroke * 4} ${stroke * 3}`}
            />
          )}
          <path
            d={result.pcb_outline.path_d}
            fill="rgba(220, 50, 50, 0.10)"
            stroke="rgba(220, 50, 50, 0.85)"
            strokeWidth={stroke}
          />

          {rowLines.map(({ row, switches }) =>
            switches.length >= 2 ? (
              <polyline
                key={`row-${row}`}
                points={pointsAttr(switches)}
                fill="none"
                stroke={ROW_COLORS[row % ROW_COLORS.length]}
                strokeOpacity={0.55}
                strokeWidth={matrixStroke}
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            ) : null,
          )}

          {colLines.map(({ col, switches }) =>
            switches.length >= 2 ? (
              <polyline
                key={`col-${col}`}
                points={pointsAttr(switches)}
                fill="none"
                stroke={COL_COLORS[col % COL_COLORS.length]}
                strokeOpacity={0.55}
                strokeWidth={matrixStroke}
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeDasharray={`${matrixStroke * 3} ${matrixStroke * 2}`}
              />
            ) : null,
          )}

          {result.switches.map((s) => (
            <g
              key={`sw-${s.id}`}
              transform={`rotate(${s.rotation_deg} ${s.cx_mm} ${s.cy_mm})`}
              className="switch-group"
              onClick={(e) => handleSwitchClick(e, s.id)}
            >
              {selectedSwitchIds.includes(s.id) && (
                <circle
                  cx={s.cx_mm}
                  cy={s.cy_mm}
                  r={dotR * 2.4}
                  fill="none"
                  stroke="rgba(255, 220, 80, 0.95)"
                  strokeWidth={stroke * 1.6}
                />
              )}
              <line
                x1={s.cx_mm}
                y1={s.cy_mm}
                x2={s.cx_mm}
                y2={s.cy_mm + tickLen}
                stroke="rgba(20, 120, 50, 0.95)"
                strokeWidth={stroke * 1.4}
                strokeLinecap="round"
              />
              <circle
                cx={s.cx_mm}
                cy={s.cy_mm}
                r={dotR}
                fill="rgba(40, 180, 70, 0.85)"
                stroke="white"
                strokeWidth={dotR * 0.15}
              >
                <title>
                  switch #{s.id} — row {s.row}, col {s.col}
                  {'\n'}rotation {s.rotation_deg.toFixed(1)}°
                  {'\n'}click: +90°, shift-click: −90°
                </title>
              </circle>
            </g>
          ))}

          {result.stabilizers.map((s) => {
            const headOffset = s.width_mm / 2 - s.height_mm * 0.35
            return (
              <g
                key={`stab-${s.id}`}
                className="stab-group"
                onClick={(e) => handleStabClick(e, s.id)}
              >
                <g transform={`rotate(${s.rotation_deg} ${s.cx_mm} ${s.cy_mm})`}>
                  <rect
                    x={s.cx_mm - s.width_mm / 2}
                    y={s.cy_mm - s.height_mm / 2}
                    width={s.width_mm}
                    height={s.height_mm}
                    fill="rgba(50, 110, 220, 0.18)"
                    stroke="rgba(50, 110, 220, 0.85)"
                    strokeWidth={stroke * 0.8}
                  />
                  <polygon
                    points={[
                      `${s.cx_mm + headOffset},${s.cy_mm - s.height_mm * 0.28}`,
                      `${s.cx_mm + headOffset},${s.cy_mm + s.height_mm * 0.28}`,
                      `${s.cx_mm + s.width_mm / 2 - s.height_mm * 0.05},${s.cy_mm}`,
                    ].join(' ')}
                    fill="rgba(30, 80, 200, 0.95)"
                    stroke="white"
                    strokeWidth={stroke * 0.4}
                  />
                </g>
                <circle
                  cx={s.cx_mm}
                  cy={s.cy_mm}
                  r={dotR * 0.8}
                  fill="rgba(50, 110, 220, 0.9)"
                  stroke="white"
                  strokeWidth={dotR * 0.12}
                >
                  <title>
                    stab #{s.id} — {s.width_mm.toFixed(1)}×{s.height_mm.toFixed(1)} mm
                    {'\n'}rotation {s.rotation_deg.toFixed(1)}° — click to flip 180°
                  </title>
                </circle>
              </g>
            )
          })}

          {result.mounting_holes.map((h) => (
            <circle
              key={`mh-${h.id}`}
              cx={h.cx_mm}
              cy={h.cy_mm}
              r={h.diameter_mm / 2}
              fill="rgba(180, 180, 190, 0.55)"
              stroke="rgba(80, 90, 110, 0.95)"
              strokeWidth={stroke * 0.7}
            >
              <title>
                mounting hole #{h.id} — ⌀{h.diameter_mm.toFixed(2)} mm
              </title>
            </circle>
          ))}

          {result.unclassified.map((u) => (
            <circle
              key={`un-${u.id}`}
              cx={u.cx_mm}
              cy={u.cy_mm}
              r={dotR}
              fill="rgba(240, 180, 30, 0.9)"
              stroke="white"
              strokeWidth={dotR * 0.15}
            >
              <title>
                unclassified #{u.id} — {u.width_mm.toFixed(1)}×{u.height_mm.toFixed(1)} mm
              </title>
            </circle>
          ))}

          {result.mcu_placement && (
            <g
              transform={`rotate(${result.mcu_placement.rotation_deg} ${result.mcu_placement.cx_mm} ${result.mcu_placement.cy_mm})`}
              className="mcu-group"
              style={{ cursor: onMcuMove ? 'grab' : 'default' }}
              onPointerDown={handleMcuPointerDown}
              onPointerMove={handleMcuPointerMove}
              onPointerUp={handleMcuPointerUp}
              onPointerCancel={handleMcuPointerUp}
            >
              {/* USB notch protruding from local Y = 0 (pin-1 short edge). */}
              <rect
                x={result.mcu_placement.cx_mm + MCU_BODY_W_MM * 0.30}
                y={result.mcu_placement.cy_mm - MCU_USB_NOTCH_MM}
                width={MCU_BODY_W_MM * 0.40}
                height={MCU_USB_NOTCH_MM}
                fill="rgba(60, 60, 70, 0.95)"
                stroke="rgba(20, 20, 30, 0.95)"
                strokeWidth={stroke * 0.6}
              />
              {/* Module body (17.78 × 27.94 mm, anchored at pin 1). */}
              <rect
                x={result.mcu_placement.cx_mm}
                y={result.mcu_placement.cy_mm}
                width={MCU_BODY_W_MM}
                height={MCU_BODY_H_MM}
                fill="rgba(40, 70, 200, 0.20)"
                stroke="rgba(40, 70, 200, 0.95)"
                strokeWidth={stroke}
              />
              {/* Anchor dot (pin 1). */}
              <circle
                cx={result.mcu_placement.cx_mm}
                cy={result.mcu_placement.cy_mm}
                r={dotR * 0.8}
                fill="rgba(255, 220, 80, 0.95)"
                stroke="rgba(40, 70, 200, 0.95)"
                strokeWidth={dotR * 0.15}
              >
                <title>
                  Pro Micro U1 — pin 1 (USB end){'\n'}
                  ({result.mcu_placement.cx_mm.toFixed(2)}, {result.mcu_placement.cy_mm.toFixed(2)}) mm{'\n'}
                  rotation {result.mcu_placement.rotation_deg.toFixed(1)}°
                </title>
              </circle>
            </g>
          )}

          {effectiveInspect && inspectInfo?.target && (
            <g className="inspect-crosshair" pointerEvents="none">
              <circle
                cx={inspectInfo.target.x}
                cy={inspectInfo.target.y}
                r={dotR * 1.4}
                fill="none"
                stroke="rgba(255, 220, 80, 0.95)"
                strokeWidth={stroke * 1.4}
              />
              <line
                x1={inspectInfo.target.x - dotR * 2.2}
                y1={inspectInfo.target.y}
                x2={inspectInfo.target.x + dotR * 2.2}
                y2={inspectInfo.target.y}
                stroke="rgba(255, 220, 80, 0.95)"
                strokeWidth={stroke * 1.2}
              />
              <line
                x1={inspectInfo.target.x}
                y1={inspectInfo.target.y - dotR * 2.2}
                x2={inspectInfo.target.x}
                y2={inspectInfo.target.y + dotR * 2.2}
                stroke="rgba(255, 220, 80, 0.95)"
                strokeWidth={stroke * 1.2}
              />
            </g>
          )}
        </svg>
        {effectiveInspect && inspectInfo && (() => {
          const anchor = inspectInfo.target ?? inspectInfo.cursor
          const pix = mmToStagePixel(anchor.x, anchor.y)
          if (!pix) return null
          return (
            <div
              className="inspect-tooltip"
              style={{ left: pix.left + 14, top: pix.top + 14 }}
            >
              {inspectInfo.target ? (
                <>
                  <div className="inspect-tooltip-label">{inspectInfo.target.label}</div>
                  <div className="inspect-tooltip-coords">
                    X {inspectInfo.target.x.toFixed(2)} mm{'  '}
                    Y {inspectInfo.target.y.toFixed(2)} mm
                  </div>
                  {inspectInfo.target.kind === 'center' && (
                    <div className="inspect-tooltip-coords">
                      ⌀ {inspectInfo.target.diameter.toFixed(2)} mm
                    </div>
                  )}
                </>
              ) : (
                <div className="inspect-tooltip-coords">
                  X {inspectInfo.cursor.x.toFixed(2)} mm{'  '}
                  Y {inspectInfo.cursor.y.toFixed(2)} mm
                </div>
              )}
            </div>
          )
        })()}
        {effectiveInspect && inspectInfo && (
          <div className="inspect-cursor-coords">
            cursor: {inspectInfo.cursor.x.toFixed(2)}, {inspectInfo.cursor.y.toFixed(2)} mm
            {altHeld && !inspectMode && (
              <span className="inspect-mode-hint"> · Alt held</span>
            )}
          </div>
        )}
      </div>
      <div className="legend">
        <span>
          <span className="dot dot-sw" /> switch ({result.switches.length})
        </span>
        <span>
          <span className="dot dot-stab" /> stabilizer ({result.stabilizers.length})
        </span>
        {result.mounting_holes.length > 0 && (
          <span>
            <span className="dot dot-mh" /> mounting hole ({result.mounting_holes.length})
          </span>
        )}
        {result.unclassified.length > 0 && (
          <span>
            <span className="dot dot-un" /> unclassified ({result.unclassified.length})
          </span>
        )}
        <span>
          <span className="dot dot-outline" /> PCB outline
        </span>
        <span>
          <span className="line-swatch line-swatch-row" /> rows ({rowLines.length})
        </span>
        <span>
          <span className="line-swatch line-swatch-col" /> cols ({colLines.length})
        </span>
      </div>
    </div>
  )
}
