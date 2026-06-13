import { useEffect, useMemo, useRef, useState } from 'react'
import type { MouseEvent, PointerEvent as ReactPointerEvent } from 'react'
import type { McuType, ParseResult, SwitchDef } from '../types'

interface Props {
  file: File
  result: ParseResult
  selectedSwitchIds: number[]
  onRotateSwitch: (id: number, deltaDeg: number) => void
  onSelectSwitch: (id: number | null) => void
  onFlipStab: (id: number) => void
  onMcuMove?: (cx: number, cy: number) => void
  mcuType?: McuType
  inspectMode?: boolean
  editPlateMode?: boolean
  addingHole?: boolean
  selectedOutlineNodeIdx?: number | null
  selectedHoleId?: number | null
  onSelectOutlineNode?: (idx: number | null) => void
  onSelectHole?: (id: number | null) => void
  onMoveOutlineNode?: (idx: number, x: number, y: number) => void
  onInsertOutlineNode?: (edgeIdx: number, x: number, y: number) => void
  onAddHole?: (x: number, y: number) => void
  onMoveHole?: (id: number, x: number, y: number) => void
}

const SNAP_AXIS_TOL_MM = 2.0

// SparkFun Pro Micro module — the PCB itself measures 18 × 33 mm, with the
// 2 × 12 pin grid (17.78 × 27.94 mm) inset asymmetrically: pin 1 sits
// ~1.5 mm from the USB-end of the board and ~0.11 mm from the left long
// edge. Pin 1 is the anchor (local (0, 0)); the body extends to (BODY_X,
// BODY_Y) → (BODY_X + W, BODY_Y + H). The USB connector protrudes
// mk.usbNotch further beyond the board's USB-end edge.
//
// Through-hole pads at the body's long edges have ~0.85 mm radius and
// stick out past the body by ~0.74 mm on each side. The dashed marker
// envelope is the union of the body rect + pad clearance so placing the
// marker's edge against a plate edge keeps every pad inside the board.
// Per-MCU marker geometry (mm). bodyXOff/bodyYOff place pin 1 (the
// anchor) relative to the body; pinGrid is the pad field extent; padR is
// the pad clearance radius; usbNotch is the connector stub at the pin-1
// end. Sources match the backend mcu.py profiles.
interface McuMarker {
  bodyW: number
  bodyH: number
  bodyXOff: number
  bodyYOff: number
  pinGridW: number
  pinGridH: number
  padR: number
  usbNotch: number
  label: string
}
const MCU_MARKERS: Record<McuType, McuMarker> = {
  pro_micro: {
    bodyW: 18.0, bodyH: 33.0, bodyXOff: -0.11, bodyYOff: -1.5,
    pinGridW: 17.78, pinGridH: 27.94, padR: 0.85, usbNotch: 4.0,
    label: 'Pro Micro',
  },
  xiao: {
    bodyW: 17.8, bodyH: 21.0, bodyXOff: -1.28, bodyYOff: -2.88,
    pinGridW: 15.24, pinGridH: 15.24, padR: 0.762, usbNotch: 3.0,
    label: 'XIAO',
  },
  xiao_smd: {
    bodyW: 17.8, bodyH: 21.0, bodyXOff: -1.28, bodyYOff: -2.88,
    pinGridW: 15.24 + 0.925, pinGridH: 15.24, padR: 1.0, usbNotch: 3.0,
    label: 'XIAO (SMD)',
  },
  pico: {
    bodyW: 21.0, bodyH: 51.0, bodyXOff: -1.61, bodyYOff: -1.37,
    pinGridW: 17.78, pinGridH: 48.26, padR: 0.8, usbNotch: 4.0,
    label: 'Raspberry Pi Pico',
  },
}

// Derive the marker envelope (union of body + pad-clearance extent) and
// expose all geometry as one object the component references.
function mcuMarker(mcuType: McuType) {
  const m = MCU_MARKERS[mcuType] ?? MCU_MARKERS.pro_micro
  const markerX = Math.min(m.bodyXOff, -m.padR)
  const markerY = Math.min(m.bodyYOff, -m.padR)
  const markerW =
    Math.max(m.bodyXOff + m.bodyW, m.pinGridW + m.padR) - markerX
  const markerH =
    Math.max(m.bodyYOff + m.bodyH, m.pinGridH + m.padR) - markerY
  return { ...m, markerX, markerY, markerW, markerH }
}

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

// Mitered outward offset of a closed polygon. Mirrors Shapely's
// `buffer(growMm, join_style=2)` behavior for convex / mild-concave shapes.
//
// For each vertex we displace its two adjacent edges outward by `growMm`
// (along their outward unit normals) and intersect the displaced edges to
// find the new vertex. Parallel-edge degenerate cases get a simple
// translation by the average outward normal. Spikes from tight reflex
// angles are clipped with a miter limit so very acute concave corners
// don't blow up; for plate outlines this fallback rarely fires.
function offsetPolygon(
  verts: Array<{ x: number; y: number }>,
  growMm: number,
  miterLimit = 10,
): Array<{ x: number; y: number }> {
  const n = verts.length
  if (n < 3 || growMm === 0) return verts.slice()
  // Signed area: positive for CCW, negative for CW.
  let area = 0
  for (let i = 0; i < n; i++) {
    const a = verts[i]
    const b = verts[(i + 1) % n]
    area += a.x * b.y - b.x * a.y
  }
  // SVG Y is down, so a polygon that visually winds clockwise has POSITIVE
  // signed area here. Pick the outward-normal sign so the offset goes away
  // from the polygon centroid regardless of the input winding.
  const outwardSign = area >= 0 ? 1 : -1
  type Edge = { px: number; py: number; nx: number; ny: number }
  const edges: Edge[] = []
  for (let i = 0; i < n; i++) {
    const a = verts[i]
    const b = verts[(i + 1) % n]
    const dx = b.x - a.x
    const dy = b.y - a.y
    const len = Math.hypot(dx, dy) || 1
    // Outward normal = rotate edge direction by 90° using outwardSign.
    const nx = (outwardSign * dy) / len
    const ny = (outwardSign * -dx) / len
    edges.push({ px: a.x + nx * growMm, py: a.y + ny * growMm, nx, ny })
  }
  const out: Array<{ x: number; y: number }> = []
  for (let i = 0; i < n; i++) {
    const e1 = edges[(i - 1 + n) % n]
    const e2 = edges[i]
    // Edge directions are perpendicular to the outward normals.
    const d1x = -e1.ny
    const d1y = e1.nx
    const d2x = -e2.ny
    const d2y = e2.nx
    // Intersect line (e1.p, dir d1) with line (e2.p, dir d2):
    //   e1.p + t1 * d1 = e2.p + t2 * d2
    const det = d1x * d2y - d1y * d2x
    let nvx: number, nvy: number
    if (Math.abs(det) < 1e-9) {
      // Parallel edges (collinear or 180° turn) — just translate the
      // original vertex by the average outward normal × growMm.
      const ax = (e1.nx + e2.nx) * 0.5
      const ay = (e1.ny + e2.ny) * 0.5
      const m = Math.hypot(ax, ay) || 1
      nvx = verts[i].x + (ax / m) * growMm
      nvy = verts[i].y + (ay / m) * growMm
    } else {
      const dx = e2.px - e1.px
      const dy = e2.py - e1.py
      const t1 = (dx * d2y - dy * d2x) / det
      nvx = e1.px + t1 * d1x
      nvy = e1.py + t1 * d1y
    }
    // Miter limit: if the new vertex is unreasonably far from the original,
    // replace it with two bevel vertices (one per offset edge).
    const dxFromOrig = nvx - verts[i].x
    const dyFromOrig = nvy - verts[i].y
    const dist = Math.hypot(dxFromOrig, dyFromOrig)
    // abs(): growMm may be negative (PCB inset) — the limit is a length.
    if (dist > miterLimit * Math.abs(growMm)) {
      out.push({ x: e1.px, y: e1.py })
      out.push({ x: e2.px, y: e2.py })
    } else {
      out.push({ x: nvx, y: nvy })
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
  mcuType = 'pro_micro',
  inspectMode = false,
  editPlateMode = false,
  addingHole = false,
  selectedOutlineNodeIdx = null,
  selectedHoleId = null,
  onSelectOutlineNode,
  onSelectHole,
  onMoveOutlineNode,
  onInsertOutlineNode,
  onAddHole,
  onMoveHole,
}: Props) {
  const mk = mcuMarker(mcuType)
  const [svgUrl, setSvgUrl] = useState<string | null>(null)
  const overlayRef = useRef<SVGSVGElement | null>(null)
  const mcuDragRef = useRef<{ pointerId: number; offsetX: number; offsetY: number } | null>(null)
  const nodeDragRef = useRef<{ pointerId: number; idx: number } | null>(null)
  const holeDragRef = useRef<{ pointerId: number; id: number } | null>(null)
  const [snapGuide, setSnapGuide] = useState<{ x: number | null; y: number | null } | null>(null)
  const [altHeld, setAltHeld] = useState(false)
  const [inspectInfo, setInspectInfo] = useState<{
    cursor: { x: number; y: number }
    target: SnapTarget | null
  } | null>(null)

  const effectiveInspect = inspectMode || altHeld
  // While editing the plate, inspect-mode hover is suppressed so handles don't
  // get visually crowded by the inspect crosshair.
  const inspectActive = effectiveInspect && !editPlateMode

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
    if (!inspectActive) setInspectInfo(null)
  }, [inspectActive])

  useEffect(() => {
    const url = URL.createObjectURL(file)
    setSvgUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [file])

  const { svg_width_mm: w, svg_height_mm: h } = result
  // Plate-anchored viewBox: derived solely from the plate bbox + a fixed
  // margin and any user-edited outline vertices. The PCB inset shrinks
  // INWARD, so it never needs extra canvas. The MCU and mounting holes
  // are deliberately *excluded* — dragging them never shifts the canvas.
  // Placing them outside the plate is an invalid layout anyway; if they
  // end up clipped, drag them back in.
  const VIEW_MARGIN_MM = 2.0
  const padded = useMemo(() => {
    let xmin = -VIEW_MARGIN_MM
    let ymin = -VIEW_MARGIN_MM
    let xmax = w + VIEW_MARGIN_MM
    let ymax = h + VIEW_MARGIN_MM
    if (result.edited_outline_path_d) {
      // User-edited outline can extend past the original viewBox.
      const editVerts = parsePathVertices(result.edited_outline_path_d)
      for (const v of editVerts) {
        xmin = Math.min(xmin, v.x)
        ymin = Math.min(ymin, v.y)
        xmax = Math.max(xmax, v.x)
        ymax = Math.max(ymax, v.y)
      }
    }
    return { xmin, ymin, w: xmax - xmin, h: ymax - ymin }
  }, [w, h, result.mcu_placement, result.edited_outline_path_d])
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
    if (inspectActive || editPlateMode) return
    e.preventDefault()
    e.stopPropagation()
    const delta = e.shiftKey ? -90 : 90
    onRotateSwitch(id, delta)
    onSelectSwitch(id)
  }

  function handleStabClick(e: MouseEvent<SVGElement>, id: number) {
    if (inspectActive || editPlateMode) return
    e.preventDefault()
    e.stopPropagation()
    onFlipStab(id)
  }

  function clientToMm(
    e:
      | ReactPointerEvent<SVGElement>
      | PointerEvent
      | MouseEvent<SVGElement>
      | { clientX: number; clientY: number },
  ): { x: number; y: number } | null {
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
    if (inspectActive || editPlateMode) return
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
    let nx = pos.x - drag.offsetX
    let ny = pos.y - drag.offsetY
    if (altHeld && result.mcu_placement) {
      const snapped = snapMcuBodyToPlateEdges(nx, ny, result.mcu_placement.rotation_deg)
      nx = snapped.x
      ny = snapped.y
      setSnapGuide(snapped.guide)
    } else {
      setSnapGuide(null)
    }
    onMcuMove(nx, ny)
  }

  // Effective PCB-edge snap lines: every axis-aligned edge of the final
  // outline polygon (edited if present, else parsed; then inset by
  // outline_shrink_mm), plus every vertex's X and Y as fallback snap
  // candidates so modified (non-axis-aligned) edges still register their
  // endpoints as alignment cues.
  function getPlateSnapLines(): { xs: number[]; ys: number[] } {
    const basePath =
      result.edited_outline_path_d || result.pcb_outline.path_d
    let verts = parsePathVertices(basePath)
    if (verts.length < 3) {
      // Fall back to the SVG viewBox rectangle.
      verts = [
        { x: 0, y: 0 },
        { x: w, y: 0 },
        { x: w, y: h },
        { x: 0, y: h },
      ]
    }
    if (result.outline_shrink_mm > 0) {
      verts = offsetPolygon(verts, -result.outline_shrink_mm)
    }
    const eps = 0.05
    const xs = new Set<number>()
    const ys = new Set<number>()
    for (let i = 0; i < verts.length; i++) {
      const a = verts[i]
      const b = verts[(i + 1) % verts.length]
      if (Math.abs(a.x - b.x) < eps) {
        xs.add(Number(a.x.toFixed(4)))
      } else if (Math.abs(a.y - b.y) < eps) {
        ys.add(Number(a.y.toFixed(4)))
      }
    }
    for (const v of verts) {
      xs.add(Number(v.x.toFixed(4)))
      ys.add(Number(v.y.toFixed(4)))
    }
    return {
      xs: [...xs].sort((p, q) => p - q),
      ys: [...ys].sort((p, q) => p - q),
    }
  }

  function snapMcuBodyToPlateEdges(
    cx: number,
    cy: number,
    rotDeg: number,
  ): { x: number; y: number; guide: { x: number | null; y: number | null } } {
    // Compute the MCU body's axis-aligned bbox in world coords at the
    // candidate (cx, cy) and current rotation. Then nudge (cx, cy) so the
    // nearest body bbox edge aligns with any axis-aligned plate edge
    // within SNAP_AXIS_TOL_MM.
    const rad = (rotDeg * Math.PI) / 180
    const cos = Math.cos(rad)
    const sin = Math.sin(rad)
    const corners: Array<[number, number]> = [
      [mk.bodyXOff, mk.bodyYOff],
      [mk.bodyXOff + mk.bodyW, mk.bodyYOff],
      [mk.bodyXOff + mk.bodyW, mk.bodyYOff + mk.bodyH],
      [mk.bodyXOff, mk.bodyYOff + mk.bodyH],
    ]
    let bodyXmin = Infinity
    let bodyXmax = -Infinity
    let bodyYmin = Infinity
    let bodyYmax = -Infinity
    for (const [lx, ly] of corners) {
      const wx = cx + lx * cos - ly * sin
      const wy = cy + lx * sin + ly * cos
      bodyXmin = Math.min(bodyXmin, wx)
      bodyXmax = Math.max(bodyXmax, wx)
      bodyYmin = Math.min(bodyYmin, wy)
      bodyYmax = Math.max(bodyYmax, wy)
    }
    const { xs: plateXs, ys: plateYs } = getPlateSnapLines()
    let bestDx = 0
    let bestAxisDx = Infinity
    let guideX: number | null = null
    for (const bodyX of [bodyXmin, bodyXmax]) {
      for (const plateX of plateXs) {
        const d = plateX - bodyX
        if (Math.abs(d) <= SNAP_AXIS_TOL_MM && Math.abs(d) < bestAxisDx) {
          bestAxisDx = Math.abs(d)
          bestDx = d
          guideX = plateX
        }
      }
    }
    let bestDy = 0
    let bestAxisDy = Infinity
    let guideY: number | null = null
    for (const bodyY of [bodyYmin, bodyYmax]) {
      for (const plateY of plateYs) {
        const d = plateY - bodyY
        if (Math.abs(d) <= SNAP_AXIS_TOL_MM && Math.abs(d) < bestAxisDy) {
          bestAxisDy = Math.abs(d)
          bestDy = d
          guideY = plateY
        }
      }
    }
    return { x: cx + bestDx, y: cy + bestDy, guide: { x: guideX, y: guideY } }
  }

  function handleMcuPointerUp(e: ReactPointerEvent<SVGElement>) {
    if (mcuDragRef.current?.pointerId === e.pointerId) {
      mcuDragRef.current = null
      setSnapGuide(null)
      try {
        e.currentTarget.releasePointerCapture(e.pointerId)
      } catch {
        // ignore
      }
    }
  }

  // ----------- Edit-plate mode helpers ------------------------------------

  // Current outline vertices for edit mode (drives handles + snap targets).
  const editedOutlineVerts = useMemo<Array<{ x: number; y: number }> | null>(() => {
    if (!editPlateMode || !result.edited_outline_path_d) return null
    return parsePathVertices(result.edited_outline_path_d)
  }, [editPlateMode, result.edited_outline_path_d])

  // Build the Alt-snap target set: every outline vertex's X and every
  // mounting-hole center's X (and the same for Y). Each entry omits the
  // node/hole that's currently being dragged so a feature doesn't snap to
  // its own coords.
  function buildSnapAxes(excludeNodeIdx: number | null, excludeHoleId: number | null) {
    const xs: number[] = []
    const ys: number[] = []
    if (editedOutlineVerts) {
      editedOutlineVerts.forEach((v, i) => {
        if (i === excludeNodeIdx) return
        xs.push(v.x)
        ys.push(v.y)
      })
    }
    for (const h of result.mounting_holes) {
      if (h.id === excludeHoleId) continue
      xs.push(h.cx_mm)
      ys.push(h.cy_mm)
    }
    return { xs, ys }
  }

  function snapToAxes(
    x: number,
    y: number,
    excludeNodeIdx: number | null,
    excludeHoleId: number | null,
  ): { x: number; y: number; guideX: number | null; guideY: number | null } {
    const { xs, ys } = buildSnapAxes(excludeNodeIdx, excludeHoleId)
    let snappedX = x
    let snappedY = y
    let guideX: number | null = null
    let guideY: number | null = null
    let bestDx = SNAP_AXIS_TOL_MM
    for (const tx of xs) {
      const d = Math.abs(tx - x)
      if (d < bestDx) {
        bestDx = d
        snappedX = tx
        guideX = tx
      }
    }
    let bestDy = SNAP_AXIS_TOL_MM
    for (const ty of ys) {
      const d = Math.abs(ty - y)
      if (d < bestDy) {
        bestDy = d
        snappedY = ty
        guideY = ty
      }
    }
    return { x: snappedX, y: snappedY, guideX, guideY }
  }

  function handleNodePointerDown(e: ReactPointerEvent<SVGElement>, idx: number) {
    if (!editPlateMode || !onMoveOutlineNode) return
    e.stopPropagation()
    e.preventDefault()
    nodeDragRef.current = { pointerId: e.pointerId, idx }
    e.currentTarget.setPointerCapture(e.pointerId)
    onSelectOutlineNode?.(idx)
    onSelectHole?.(null)
  }

  function handleNodePointerMove(e: ReactPointerEvent<SVGElement>) {
    const drag = nodeDragRef.current
    if (!drag || drag.pointerId !== e.pointerId) return
    const pos = clientToMm(e)
    if (!pos) return
    if (altHeld) {
      const snapped = snapToAxes(pos.x, pos.y, drag.idx, null)
      setSnapGuide({ x: snapped.guideX, y: snapped.guideY })
      onMoveOutlineNode?.(drag.idx, snapped.x, snapped.y)
    } else {
      setSnapGuide(null)
      onMoveOutlineNode?.(drag.idx, pos.x, pos.y)
    }
  }

  function handleNodePointerUp(e: ReactPointerEvent<SVGElement>) {
    if (nodeDragRef.current?.pointerId === e.pointerId) {
      nodeDragRef.current = null
      setSnapGuide(null)
      try {
        e.currentTarget.releasePointerCapture(e.pointerId)
      } catch {
        // ignore
      }
    }
  }

  function handleEdgeClick(e: MouseEvent<SVGElement>, edgeIdx: number) {
    if (!editPlateMode || !onInsertOutlineNode || !editedOutlineVerts) return
    e.stopPropagation()
    e.preventDefault()
    const a = editedOutlineVerts[edgeIdx]
    const b = editedOutlineVerts[(edgeIdx + 1) % editedOutlineVerts.length]
    onInsertOutlineNode(edgeIdx, (a.x + b.x) / 2, (a.y + b.y) / 2)
  }

  function handleHolePointerDown(e: ReactPointerEvent<SVGElement>, id: number) {
    if (!editPlateMode || !onMoveHole) return
    e.stopPropagation()
    e.preventDefault()
    holeDragRef.current = { pointerId: e.pointerId, id }
    e.currentTarget.setPointerCapture(e.pointerId)
    onSelectHole?.(id)
    onSelectOutlineNode?.(null)
  }

  function handleHolePointerMove(e: ReactPointerEvent<SVGElement>) {
    const drag = holeDragRef.current
    if (!drag || drag.pointerId !== e.pointerId) return
    const pos = clientToMm(e)
    if (!pos) return
    if (altHeld) {
      const snapped = snapToAxes(pos.x, pos.y, null, drag.id)
      setSnapGuide({ x: snapped.guideX, y: snapped.guideY })
      onMoveHole?.(drag.id, snapped.x, snapped.y)
    } else {
      setSnapGuide(null)
      onMoveHole?.(drag.id, pos.x, pos.y)
    }
  }

  function handleHolePointerUp(e: ReactPointerEvent<SVGElement>) {
    if (holeDragRef.current?.pointerId === e.pointerId) {
      holeDragRef.current = null
      setSnapGuide(null)
      try {
        e.currentTarget.releasePointerCapture(e.pointerId)
      } catch {
        // ignore
      }
    }
  }

  function handleOverlayClick(e: MouseEvent<SVGSVGElement>) {
    // In edit mode, a click on the SVG either places a new hole (if armed)
    // or clears node/hole selection on empty-canvas clicks.
    if (!editPlateMode) return
    if (addingHole && onAddHole) {
      // Hole placement ignores what's underneath the cursor — the plate
      // outline path has a translucent fill that would otherwise swallow
      // every click on the plate area. Node / hole / edge handlers all
      // stopPropagation on pointerdown, so they won't reach this click.
      const pos = clientToMm(e)
      if (!pos) return
      if (altHeld) {
        const snapped = snapToAxes(pos.x, pos.y, null, null)
        onAddHole(snapped.x, snapped.y)
      } else {
        onAddHole(pos.x, pos.y)
      }
      return
    }
    // Otherwise only treat as a "clear selection" gesture when the click
    // truly landed on empty canvas, not on a child element.
    if (e.target !== e.currentTarget) return
    onSelectOutlineNode?.(null)
    onSelectHole?.(null)
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
      // Shrunk PCB outline (only when inset > 0). True mitered offset of
      // the outline polygon — same algorithm the dashed-overlay path uses,
      // so every snap target matches what the user sees.
      if (result.outline_shrink_mm > 0) {
        const pcbVerts = offsetPolygon(verts, -result.outline_shrink_mm)
        pcbVerts.forEach((v, idx) =>
          out.push({
            kind: 'corner',
            x: v.x,
            y: v.y,
            label: `PCB outline node ${idx + 1}`,
          }),
        )
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
    // MCU body corners + pad-clearance envelope corners. Pin 1 is the
    // anchor; body corners walk from (BODY_X, BODY_Y) → (BODY_X+W, BODY_Y+H).
    if (result.mcu_placement) {
      const m = result.mcu_placement
      const rad = (m.rotation_deg * Math.PI) / 180
      const cos = Math.cos(rad)
      const sin = Math.sin(rad)
      const localCornerSets: Array<[number, number, string]> = []
      for (const [lx, ly] of [
        [mk.bodyXOff, mk.bodyYOff],
        [mk.bodyXOff + mk.bodyW, mk.bodyYOff],
        [mk.bodyXOff + mk.bodyW, mk.bodyYOff + mk.bodyH],
        [mk.bodyXOff, mk.bodyYOff + mk.bodyH],
      ]) {
        localCornerSets.push([lx, ly, 'MCU body corner'])
      }
      for (const [lx, ly] of [
        [mk.markerX, mk.markerY],
        [mk.markerX + mk.markerW, mk.markerY],
        [mk.markerX + mk.markerW, mk.markerY + mk.markerH],
        [mk.markerX, mk.markerY + mk.markerH],
      ]) {
        localCornerSets.push([lx, ly, 'MCU outline corner'])
      }
      for (const [lx, ly, label] of localCornerSets) {
        out.push({
          kind: 'corner',
          x: m.cx_mm + lx * cos - ly * sin,
          y: m.cy_mm + lx * sin + ly * cos,
          label,
        })
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
    if (!inspectActive) return
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

  // Shrunk PCB outline: true mitered inward offset of the active base
  // outline (the user-edited polygon if any, else the parsed outline).
  // For a rectangular plate this produces a rectangle `shrink` mm smaller;
  // for a polygonal outline the inset follows every notch — matching what
  // Shapely's buffer() will emit into Edge.Cuts on the PCB side.
  const grownOutlineVerts = useMemo(() => {
    const shrink = result.outline_shrink_mm
    if (!shrink || shrink <= 0) return null
    const basePath =
      result.edited_outline_path_d || result.pcb_outline.path_d
    const verts = parsePathVertices(basePath)
    if (verts.length < 3) return null
    return offsetPolygon(verts, -shrink)
  }, [
    result.outline_shrink_mm,
    result.pcb_outline.path_d,
    result.edited_outline_path_d,
  ])

  const grownOutlinePath = useMemo(() => {
    if (!grownOutlineVerts || grownOutlineVerts.length < 3) return null
    const [v0, ...rest] = grownOutlineVerts
    return (
      `M ${v0.x.toFixed(4)} ${v0.y.toFixed(4)} ` +
      rest.map((v) => `L ${v.x.toFixed(4)} ${v.y.toFixed(4)}`).join(' ') +
      ' Z'
    )
  }, [grownOutlineVerts])

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
              // the surrounding padded region. `inset: auto` must come FIRST
              // — it's shorthand for top/right/bottom/left and would clobber
              // our explicit left/top if declared after them.
              inset: 'auto',
              left: `${((-padded.xmin) / padded.w) * 100}%`,
              top: `${((-padded.ymin) / padded.h) * 100}%`,
              width: `${(w / padded.w) * 100}%`,
              height: `${(h / padded.h) * 100}%`,
            }}
          />
        )}
        <svg
          ref={overlayRef}
          className="preview-overlay"
          viewBox={viewBox}
          preserveAspectRatio="xMidYMid meet"
          xmlns="http://www.w3.org/2000/svg"
          style={{
            cursor:
              inspectActive || (editPlateMode && addingHole)
                ? 'crosshair'
                : undefined,
          }}
          onPointerMove={handleOverlayPointerMove}
          onPointerLeave={handleOverlayPointerLeave}
          onClick={handleOverlayClick}
        >
          {/* Outline rendering — three independent dashed/solid layers:
              1. Shrunk PCB outline inside whichever base outline is
                 active (only when outline_shrink_mm > 0); dashed.
              2. Original parsed outline (only when the user has edits, so
                 they can see what changed); dashed.
              3. Active base outline (edited if present, else parsed);
                 solid.
              The dashed inset is what the PCB Edge.Cuts will use when
              shrink > 0 — applied on top of whatever base outline is
              active. */}
          {grownOutlinePath && (
            <path
              d={grownOutlinePath}
              fill="rgba(220, 50, 50, 0.04)"
              stroke="rgba(220, 50, 50, 0.55)"
              strokeWidth={stroke * 0.8}
              strokeDasharray={`${stroke * 4} ${stroke * 3}`}
            />
          )}
          {result.edited_outline_path_d && (
            <path
              d={result.pcb_outline.path_d}
              fill="none"
              stroke="rgba(220, 50, 50, 0.55)"
              strokeWidth={stroke * 0.8}
              strokeDasharray={`${stroke * 4} ${stroke * 3}`}
            />
          )}
          <path
            d={result.edited_outline_path_d || result.pcb_outline.path_d}
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

          {result.mounting_holes.map((h) => {
            const isSelected = editPlateMode && selectedHoleId === h.id
            return (
              <g key={`mh-${h.id}`}>
                <circle
                  cx={h.cx_mm}
                  cy={h.cy_mm}
                  r={h.diameter_mm / 2}
                  fill="rgba(180, 180, 190, 0.55)"
                  stroke={
                    isSelected
                      ? 'rgba(255, 220, 80, 0.95)'
                      : 'rgba(80, 90, 110, 0.95)'
                  }
                  strokeWidth={
                    isSelected ? stroke * 1.6 : stroke * 0.7
                  }
                  style={{ cursor: editPlateMode ? 'grab' : 'default' }}
                  onPointerDown={
                    editPlateMode
                      ? (e) => handleHolePointerDown(e, h.id)
                      : undefined
                  }
                  onPointerMove={
                    editPlateMode ? handleHolePointerMove : undefined
                  }
                  onPointerUp={
                    editPlateMode ? handleHolePointerUp : undefined
                  }
                  onPointerCancel={
                    editPlateMode ? handleHolePointerUp : undefined
                  }
                >
                  <title>
                    mounting hole #{h.id} — ⌀{h.diameter_mm.toFixed(2)} mm
                  </title>
                </circle>
              </g>
            )
          })}

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
              {/* USB notch protruding past the body's USB-end edge. */}
              <rect
                x={
                  result.mcu_placement.cx_mm +
                  mk.bodyXOff +
                  mk.bodyW * 0.30
                }
                y={
                  result.mcu_placement.cy_mm +
                  mk.bodyYOff -
                  mk.usbNotch
                }
                width={mk.bodyW * 0.40}
                height={mk.usbNotch}
                fill="rgba(60, 60, 70, 0.95)"
                stroke="rgba(20, 20, 30, 0.95)"
                strokeWidth={stroke * 0.6}
              />
              {/* Pad-clearance envelope: union of body + pin-pad extent.
                  Placing this outline against the plate edge keeps every
                  pad safely inside the board. */}
              <rect
                x={result.mcu_placement.cx_mm + mk.markerX}
                y={result.mcu_placement.cy_mm + mk.markerY}
                width={mk.markerW}
                height={mk.markerH}
                fill="rgba(40, 70, 200, 0.10)"
                stroke="rgba(40, 70, 200, 0.55)"
                strokeWidth={stroke * 0.6}
                strokeDasharray={`${stroke * 2} ${stroke * 2}`}
              />
              {/* Module body (18 × 33 mm). Pin 1 sits at the anchor, with
                  the body extending up and out around it per the offsets. */}
              <rect
                x={result.mcu_placement.cx_mm + mk.bodyXOff}
                y={result.mcu_placement.cy_mm + mk.bodyYOff}
                width={mk.bodyW}
                height={mk.bodyH}
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
                  {mk.label} U1 — pin 1 (USB end){'\n'}
                  ({result.mcu_placement.cx_mm.toFixed(2)}, {result.mcu_placement.cy_mm.toFixed(2)}) mm{'\n'}
                  body {mk.bodyW.toFixed(2)} × {mk.bodyH.toFixed(2)} mm{'\n'}
                  rotation {result.mcu_placement.rotation_deg.toFixed(1)}°
                </title>
              </circle>
            </g>
          )}

          {inspectActive && inspectInfo?.target && (
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

          {/* Edit-plate handles: edge midpoints (insert) + vertices (drag). */}
          {editPlateMode && editedOutlineVerts && editedOutlineVerts.length >= 2 && (
            <g className="outline-edit-handles">
              {editedOutlineVerts.map((v, idx) => {
                const next = editedOutlineVerts[(idx + 1) % editedOutlineVerts.length]
                const mx = (v.x + next.x) / 2
                const my = (v.y + next.y) / 2
                return (
                  <circle
                    key={`edge-${idx}`}
                    cx={mx}
                    cy={my}
                    r={dotR * 0.9}
                    fill="rgba(80, 200, 130, 0.55)"
                    stroke="rgba(20, 110, 60, 0.95)"
                    strokeWidth={stroke * 0.6}
                    style={{ cursor: 'copy' }}
                    onClick={(e) => handleEdgeClick(e, idx)}
                  >
                    <title>
                      add node on edge {idx} → ({mx.toFixed(2)}, {my.toFixed(2)})
                    </title>
                  </circle>
                )
              })}
              {editedOutlineVerts.map((v, idx) => {
                const isSel = selectedOutlineNodeIdx === idx
                return (
                  <circle
                    key={`node-${idx}`}
                    cx={v.x}
                    cy={v.y}
                    r={dotR * 1.2}
                    fill={isSel ? 'rgba(255, 220, 80, 0.95)' : 'rgba(220, 100, 100, 0.9)'}
                    stroke="white"
                    strokeWidth={stroke * 0.8}
                    style={{ cursor: 'grab' }}
                    onPointerDown={(e) => handleNodePointerDown(e, idx)}
                    onPointerMove={handleNodePointerMove}
                    onPointerUp={handleNodePointerUp}
                    onPointerCancel={handleNodePointerUp}
                  >
                    <title>
                      outline node {idx} — ({v.x.toFixed(2)}, {v.y.toFixed(2)})
                      {'\n'}drag to move · Alt to snap · Delete to remove
                    </title>
                  </circle>
                )
              })}
            </g>
          )}

          {/* Alt-snap guide lines (rendered during a node or hole drag). */}
          {snapGuide && (
            <g pointerEvents="none">
              {snapGuide.x !== null && (
                <line
                  x1={snapGuide.x}
                  y1={padded.ymin}
                  x2={snapGuide.x}
                  y2={padded.ymin + padded.h}
                  stroke="rgba(255, 220, 80, 0.7)"
                  strokeWidth={stroke * 0.6}
                  strokeDasharray={`${stroke * 3} ${stroke * 2}`}
                />
              )}
              {snapGuide.y !== null && (
                <line
                  x1={padded.xmin}
                  y1={snapGuide.y}
                  x2={padded.xmin + padded.w}
                  y2={snapGuide.y}
                  stroke="rgba(255, 220, 80, 0.7)"
                  strokeWidth={stroke * 0.6}
                  strokeDasharray={`${stroke * 3} ${stroke * 2}`}
                />
              )}
            </g>
          )}
        </svg>
        {inspectActive && inspectInfo && (() => {
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
        {inspectActive && inspectInfo && (
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
          <span className="dot dot-outline" />{' '}
          {result.edited_outline_path_d
            ? 'PCB outline (edited)'
            : 'PCB outline'}
        </span>
        {result.edited_outline_path_d && (
          <span>
            <span className="dot dot-outline-dashed" /> original outline
          </span>
        )}
        {result.outline_shrink_mm > 0 && (
          <span>
            <span className="dot dot-outline-dashed" /> PCB outline (−
            {result.outline_shrink_mm.toFixed(1)} mm)
          </span>
        )}
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
