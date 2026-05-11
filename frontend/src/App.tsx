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
  SwitchDef,
  SwitchType,
} from './types'

export function App() {
  const [file, setFile] = useState<File | null>(null)
  const [result, setResult] = useState<ParseResult | null>(null)
  const [originalSwitches, setOriginalSwitches] = useState<SwitchDef[]>([])
  const [originalStabs, setOriginalStabs] = useState<StabilizerDef[]>([])
  const [strategy, setStrategy] = useState<MatrixStrategy>('row_first')
  const [switchType, setSwitchType] = useState<SwitchType>('soldered')
  const [diodeType, setDiodeType] = useState<DiodeType>('tht')
  const [redetectError, setRedetectError] = useState<string | null>(null)
  const [selectedSwitchId, setSelectedSwitchId] = useState<number | null>(null)
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
    setSelectedSwitchId(null)
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
      setSelectedSwitchId(null)
    } catch (err) {
      setRedetectError(err instanceof Error ? err.message : String(err))
    }
  }

  function moveSwitch(id: number, newRow: number, newCol: number) {
    if (!result) return
    const moving = result.switches.find((s) => s.id === id)
    if (!moving) return
    if (moving.row === newRow && moving.col === newCol) return
    const occupant = result.switches.find(
      (s) => s.row === newRow && s.col === newCol && s.id !== id,
    )
    setResult({
      ...result,
      switches: result.switches.map((s) => {
        if (s.id === id) return { ...s, row: newRow, col: newCol }
        if (occupant && s.id === occupant.id)
          return { ...s, row: moving.row, col: moving.col }
        return s
      }),
    })
    setSelectedSwitchId(id)
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
      () => generatePcb(result!, switchType, diodeType),
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
            selectedSwitchId={selectedSwitchId}
            onRotateSwitch={rotateSwitch}
            onSelectSwitch={setSelectedSwitchId}
            onFlipStab={flipStab}
          />
          <MatrixGrid
            result={result}
            selectedSwitchId={selectedSwitchId}
            onSelectSwitch={setSelectedSwitchId}
            onMoveSwitch={moveSwitch}
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
