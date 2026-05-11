import { useEffect, useMemo, useState } from 'react'
import type { MouseEvent } from 'react'
import type { ParseResult, SwitchDef } from '../types'

interface Props {
  file: File
  result: ParseResult
  selectedSwitchId: number | null
  onRotateSwitch: (id: number, deltaDeg: number) => void
  onSelectSwitch: (id: number | null) => void
  onFlipStab: (id: number) => void
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
  selectedSwitchId,
  onRotateSwitch,
  onSelectSwitch,
  onFlipStab,
}: Props) {
  const [svgUrl, setSvgUrl] = useState<string | null>(null)

  useEffect(() => {
    const url = URL.createObjectURL(file)
    setSvgUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [file])

  const { svg_width_mm: w, svg_height_mm: h } = result
  const viewBox = `0 0 ${w} ${h}`
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
    e.preventDefault()
    e.stopPropagation()
    const delta = e.shiftKey ? -90 : 90
    onRotateSwitch(id, delta)
    onSelectSwitch(id)
  }

  function handleStabClick(e: MouseEvent<SVGElement>, id: number) {
    e.preventDefault()
    e.stopPropagation()
    onFlipStab(id)
  }

  return (
    <div className="preview">
      <div className="preview-stage" style={{ aspectRatio: `${w} / ${h}` }}>
        {svgUrl && (
          <img
            className="preview-img"
            src={svgUrl}
            alt="Plate preview"
            draggable={false}
          />
        )}
        <svg
          className="preview-overlay"
          viewBox={viewBox}
          preserveAspectRatio="xMidYMid meet"
          xmlns="http://www.w3.org/2000/svg"
        >
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
              {s.id === selectedSwitchId && (
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
        </svg>
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
