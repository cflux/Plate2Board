import { useEffect, useState } from 'react'
import { UploadStep } from './components/UploadStep'
import { SvgPreview } from './components/SvgPreview'
import { MatrixGrid } from './components/MatrixGrid'
import { OrientationHelp } from './components/OrientationHelp'
import {
  generateNetlist,
  generatePcb,
  generateProjectZip,
  generateSchematic,
  getBackendVersion,
  parseSvg,
  type BackendVersion,
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

export function App() {
  const [file, setFile] = useState<File | null>(null)
  const [result, setResult] = useState<ParseResult | null>(null)
  const [originalSwitches, setOriginalSwitches] = useState<SwitchDef[]>([])
  const [originalStabs, setOriginalStabs] = useState<StabilizerDef[]>([])
  const [strategy, setStrategy] = useState<MatrixStrategy>('auto')
  const [switchType, setSwitchType] = useState<SwitchType>('soldered')
  const [diodeType, setDiodeType] = useState<DiodeType>('tht')
  const [stabilizerType, setStabilizerType] = useState<StabilizerType>('pcb_mount')
  const [inspectMode, setInspectMode] = useState<boolean>(false)
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
  }

  async function redetect(newStrategy: MatrixStrategy) {
    if (!file || newStrategy === strategy) {
      setStrategy(newStrategy)
      return
    }
    setRedetectError(null)
    try {
      const r = await parseSvg(file, newStrategy)
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

  function setOutlineGrow(mm: number) {
    if (!result) return
    setResult({ ...result, outline_grow_mm: Math.max(0, mm) })
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
    null | 'net' | 'sch' | 'pcb' | 'zip'
  >(null)

  async function downloadFile(
    kind: 'net' | 'sch' | 'pcb',
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
    return downloadFile('net', () => generateNetlist(result!.switches), 'net')
  }

  function downloadSchematic() {
    return downloadFile(
      'sch',
      () => generateSchematic(result!.switches),
      'kicad_sch',
    )
  }

  function downloadPcb() {
    return downloadFile(
      'pcb',
      () => generatePcb(result!, switchType, diodeType, stabilizerType),
      'kicad_pcb',
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
          {result.mcu_placement && (
            <div className="toolbar toolbar-strategy">
              <span className="toolbar-label">MCU (Pro Micro):</span>
              <label className="toolbar-input">
                X
                <input
                  type="number"
                  step="0.1"
                  value={result.mcu_placement.cx_mm.toFixed(2)}
                  onChange={(e) => updateMcu({ cx_mm: parseFloat(e.target.value) || 0 })}
                  title="X position (mm) of Pro Micro pin 1 (USB end). Drag the marker on the preview for coarse placement, fine-tune here."
                />
              </label>
              <label className="toolbar-input">
                Y
                <input
                  type="number"
                  step="0.1"
                  value={result.mcu_placement.cy_mm.toFixed(2)}
                  onChange={(e) => updateMcu({ cy_mm: parseFloat(e.target.value) || 0 })}
                  title="Y position (mm) of Pro Micro pin 1 (USB end)."
                />
              </label>
              <label className="toolbar-input">
                Rotation
                <input
                  type="number"
                  step="1"
                  value={result.mcu_placement.rotation_deg.toFixed(1)}
                  onChange={(e) =>
                    updateMcu({ rotation_deg: parseFloat(e.target.value) || 0 })
                  }
                  title="Rotation in degrees, SVG convention (clockwise positive). Pin 1 / USB end of the marker faces the direction USB will exit."
                />
              </label>
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
            <span className="toolbar-label">Outline grow:</span>
            <label className="toolbar-input">
              <input
                type="number"
                step="0.5"
                min={0}
                max={50}
                value={result.outline_grow_mm.toFixed(1)}
                onChange={(e) => setOutlineGrow(parseFloat(e.target.value) || 0)}
                title="Dilate the PCB outline by N mm on all four sides. Useful when the plate SVG has zero clearance around the outermost cutouts and you need room for screw bosses or perimeter routing."
              />
              <span className="toolbar-unit">mm</span>
            </label>
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
