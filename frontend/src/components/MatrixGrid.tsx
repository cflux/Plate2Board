import { useState } from 'react'
import type { DragEvent, MouseEvent } from 'react'
import type { ParseResult, SwitchDef } from '../types'

const ROW_COLORS = [
  '#e07050', '#d68030', '#c4992a', '#a8a830',
  '#d04070', '#b85050', '#cc6020', '#9c7020',
]

export type MatrixClickMode = 'replace' | 'extend' | 'toggle'

interface Props {
  result: ParseResult
  selectedSwitchIds: number[]
  onSelectClick: (id: number, mode: MatrixClickMode) => void
  onClearSelection: () => void
  onMoveSwitch: (id: number, newRow: number, newCol: number) => void
  moveError: string | null
}

interface DragState {
  draggedId: number
  originRow: number
  originCol: number
  groupOffsets: Array<{ id: number; dRow: number; dCol: number }>
}

export function MatrixGrid({
  result,
  selectedSwitchIds,
  onSelectClick,
  onClearSelection,
  onMoveSwitch,
  moveError,
}: Props) {
  const [drag, setDrag] = useState<DragState | null>(null)
  const [hoverCell, setHoverCell] = useState<{ row: number; col: number } | null>(null)

  if (result.switches.length === 0) return null

  // Render with a 1-cell padding on every side of the switch bounding box.
  // The padding row/col lets the user drag the matrix into the padding to
  // grow it in that direction; App.moveSwitches accepts the resulting (-1)
  // or (max+1) indices and renormalizes them back to non-negative.
  const switchMinRow = Math.min(...result.switches.map((s) => s.row), 0)
  const switchMaxRow = Math.max(...result.switches.map((s) => s.row), 0)
  const switchMinCol = Math.min(...result.switches.map((s) => s.col), 0)
  const switchMaxCol = Math.max(...result.switches.map((s) => s.col), 0)
  const rowStart = switchMinRow - 1
  const rowEnd = switchMaxRow + 1
  const colStart = switchMinCol - 1
  const colEnd = switchMaxCol + 1

  const switchAt = new Map<string, SwitchDef>()
  for (const s of result.switches) switchAt.set(`${s.row},${s.col}`, s)

  const selectedSet = new Set(selectedSwitchIds)
  const anchorId = selectedSwitchIds[selectedSwitchIds.length - 1]

  // Drop preview (yellow / red dashed) for the currently hovered cell.
  let dropTargets: Map<string, number> | null = null
  let blockedCells: Set<string> | null = null
  if (drag && hoverCell) {
    const dRow = hoverCell.row - drag.originRow
    const dCol = hoverCell.col - drag.originCol
    const targets = new Map<string, number>()
    const blocked = new Set<string>()
    for (const { id, dRow: oRow, dCol: oCol } of drag.groupOffsets) {
      const r = drag.originRow + oRow + dRow
      const c = drag.originCol + oCol + dCol
      targets.set(`${r},${c}`, id)
    }
    const draggedSet = new Set(drag.groupOffsets.map((g) => g.id))
    // For single-key drags (one offset), occupied target = insert (allowed),
    // so don't mark it blocked. For group drags (multiple offsets),
    // collisions with non-selected switches do block.
    const isGroupDrag = drag.groupOffsets.length > 1
    if (isGroupDrag) {
      for (const sw of result.switches) {
        if (draggedSet.has(sw.id)) continue
        if (targets.has(`${sw.row},${sw.col}`)) blocked.add(`${sw.row},${sw.col}`)
      }
    }
    dropTargets = targets
    blockedCells = blocked
  }

  function handleDragStart(
    e: DragEvent<HTMLTableCellElement>,
    sw: SwitchDef,
  ) {
    e.dataTransfer.setData('text/plain', String(sw.id))
    e.dataTransfer.effectAllowed = 'move'
    const ids = selectedSet.has(sw.id) ? selectedSwitchIds : [sw.id]
    const offsets: Array<{ id: number; dRow: number; dCol: number }> = []
    for (const id of ids) {
      const s = result.switches.find((x) => x.id === id)
      if (!s) continue
      offsets.push({ id, dRow: s.row - sw.row, dCol: s.col - sw.col })
    }
    setDrag({
      draggedId: sw.id,
      originRow: sw.row,
      originCol: sw.col,
      groupOffsets: offsets,
    })
  }

  function handleDragEnd() {
    setDrag(null)
    setHoverCell(null)
  }

  function handleDragOver(
    e: DragEvent<HTMLTableCellElement>,
    r: number,
    c: number,
  ) {
    e.preventDefault()
    if (!hoverCell || hoverCell.row !== r || hoverCell.col !== c) {
      setHoverCell({ row: r, col: c })
    }
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
    setDrag(null)
    setHoverCell(null)
  }

  function handleCellClick(e: MouseEvent<HTMLTableCellElement>, id: number) {
    if (e.shiftKey) onSelectClick(id, 'extend')
    else if (e.ctrlKey || e.metaKey) onSelectClick(id, 'toggle')
    else onSelectClick(id, 'replace')
  }

  function handleEmptyCellClick() {
    // Plain click on an empty cell clears the selection (matches OS file-
    // manager behavior — click whitespace to deselect).
    onClearSelection()
  }

  function handleBackgroundClick(e: MouseEvent<HTMLDivElement>) {
    if (e.target === e.currentTarget) onClearSelection()
  }

  const selectionHint =
    selectedSwitchIds.length === 0
      ? 'click a cell · shift+click to extend (box) · ctrl/cmd+click to toggle · drag onto a switch to insert · drag onto padding to grow the matrix'
      : `${selectedSwitchIds.length} selected · shift+click to extend the box · drag any selected cell to shift the group`

  const cols: number[] = []
  for (let c = colStart; c <= colEnd; c++) cols.push(c)
  const rows: number[] = []
  for (let r = rowStart; r <= rowEnd; r++) rows.push(r)

  return (
    <div className="matrix-grid-wrapper" onClick={handleBackgroundClick}>
      <div className="matrix-grid-header">
        <h3>
          Matrix — {switchMaxRow - switchMinRow + 1} row
          {switchMaxRow - switchMinRow !== 0 ? 's' : ''} ×{' '}
          {switchMaxCol - switchMinCol + 1} col
          {switchMaxCol - switchMinCol !== 0 ? 's' : ''}
        </h3>
        <span className="matrix-hint">{selectionHint}</span>
        {moveError && <span className="err matrix-move-error">{moveError}</span>}
      </div>
      <div className="matrix-grid-scroll">
        <table className="matrix-grid">
          <thead>
            <tr>
              <th></th>
              {cols.map((c) => (
                <th
                  key={c}
                  className={
                    c < switchMinCol || c > switchMaxCol ? 'header-pad' : ''
                  }
                >
                  c{c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const isPadRow = r < switchMinRow || r > switchMaxRow
              return (
                <tr key={r}>
                  <th className={isPadRow ? 'header-pad' : ''}>r{r}</th>
                  {cols.map((c) => {
                    const sw = switchAt.get(`${r},${c}`)
                    const isPadCol = c < switchMinCol || c > switchMaxCol
                    const isPad = isPadRow || isPadCol
                    const tint = ROW_COLORS[((r % ROW_COLORS.length) + ROW_COLORS.length) % ROW_COLORS.length]
                    const isSelected = !!sw && selectedSet.has(sw.id)
                    const isAnchor = !!sw && sw.id === anchorId
                    const key = `${r},${c}`
                    const isDropTarget = dropTargets?.has(key) ?? false
                    const isBlocked = blockedCells?.has(key) ?? false
                    const cellClasses = [
                      'matrix-cell',
                      !sw ? 'empty' : '',
                      isPad && !sw ? 'pad' : '',
                      isSelected ? 'selected' : '',
                      isAnchor ? 'anchor' : '',
                      isDropTarget && !isBlocked ? 'drop-target' : '',
                      isBlocked ? 'drop-blocked' : '',
                    ].filter(Boolean).join(' ')
                    return (
                      <td
                        key={c}
                        className={cellClasses}
                        style={
                          sw
                            ? {
                                backgroundColor:
                                  tint + (isSelected ? 'd0' : '55'),
                                borderColor: isSelected ? tint : 'transparent',
                              }
                            : undefined
                        }
                        onClick={
                          sw
                            ? (e) => handleCellClick(e, sw.id)
                            : handleEmptyCellClick
                        }
                        draggable={Boolean(sw)}
                        onDragStart={
                          sw ? (e) => handleDragStart(e, sw) : undefined
                        }
                        onDragEnd={handleDragEnd}
                        onDragOver={(e) => handleDragOver(e, r, c)}
                        onDrop={(e) => handleDrop(e, r, c)}
                        title={
                          sw
                            ? `switch #${sw.id} — row ${sw.row}, col ${sw.col}`
                            : isPad
                              ? `(padding — drop here to grow the matrix)`
                              : `(empty slot — drop here to assign)`
                        }
                      >
                        {sw ? `#${sw.id}` : ''}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
