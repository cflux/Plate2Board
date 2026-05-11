import type { DragEvent } from 'react'
import type { ParseResult, SwitchDef } from '../types'

const ROW_COLORS = [
  '#e07050', '#d68030', '#c4992a', '#a8a830',
  '#d04070', '#b85050', '#cc6020', '#9c7020',
]

interface Props {
  result: ParseResult
  selectedSwitchId: number | null
  onSelectSwitch: (id: number | null) => void
  onMoveSwitch: (id: number, newRow: number, newCol: number) => void
}

export function MatrixGrid({
  result,
  selectedSwitchId,
  onSelectSwitch,
  onMoveSwitch,
}: Props) {
  if (result.switches.length === 0) return null

  const maxRow = Math.max(...result.switches.map((s) => s.row))
  const maxCol = Math.max(...result.switches.map((s) => s.col))

  const grid: (SwitchDef | undefined)[][] = Array.from(
    { length: maxRow + 1 },
    () => Array.from({ length: maxCol + 1 }, () => undefined),
  )
  for (const s of result.switches) {
    grid[s.row][s.col] = s
  }

  function handleDragStart(e: DragEvent<HTMLTableCellElement>, id: number) {
    e.dataTransfer.setData('text/plain', String(id))
    e.dataTransfer.effectAllowed = 'move'
  }

  function handleDrop(
    e: DragEvent<HTMLTableCellElement>,
    r: number,
    c: number,
  ) {
    e.preventDefault()
    const raw = e.dataTransfer.getData('text/plain')
    const id = Number.parseInt(raw, 10)
    if (Number.isFinite(id)) onMoveSwitch(id, r, c)
  }

  return (
    <div className="matrix-grid-wrapper">
      <div className="matrix-grid-header">
        <h3>
          Matrix — {maxRow + 1} row{maxRow !== 0 ? 's' : ''} × {maxCol + 1} col
          {maxCol !== 0 ? 's' : ''}
        </h3>
        <span className="matrix-hint">
          click a cell to highlight on the SVG · drag cells to swap (row, col)
        </span>
      </div>
      <div className="matrix-grid-scroll">
        <table className="matrix-grid">
          <thead>
            <tr>
              <th></th>
              {Array.from({ length: maxCol + 1 }, (_, c) => (
                <th key={c}>c{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {grid.map((row, r) => (
              <tr key={r}>
                <th>r{r}</th>
                {row.map((sw, c) => {
                  const tint = ROW_COLORS[r % ROW_COLORS.length]
                  const isSelected = sw && sw.id === selectedSwitchId
                  return (
                    <td
                      key={c}
                      className={
                        sw
                          ? `matrix-cell${isSelected ? ' selected' : ''}`
                          : 'matrix-cell empty'
                      }
                      style={
                        sw
                          ? {
                              backgroundColor: tint + (isSelected ? 'd0' : '55'),
                              borderColor: isSelected ? tint : 'transparent',
                            }
                          : undefined
                      }
                      onClick={() => {
                        if (!sw) return
                        onSelectSwitch(isSelected ? null : sw.id)
                      }}
                      draggable={Boolean(sw)}
                      onDragStart={
                        sw ? (e) => handleDragStart(e, sw.id) : undefined
                      }
                      onDragOver={(e) => e.preventDefault()}
                      onDrop={(e) => handleDrop(e, r, c)}
                      title={
                        sw
                          ? `switch #${sw.id} — row ${sw.row}, col ${sw.col}`
                          : `(empty slot — drop here to assign)`
                      }
                    >
                      {sw ? `#${sw.id}` : ''}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
