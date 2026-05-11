import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { parseSvg } from '../api/client'
import type { MatrixStrategy, ParseResult } from '../types'

interface Props {
  onParsed: (file: File, result: ParseResult) => void
  strategy: MatrixStrategy
}

export function UploadStep({ onParsed, strategy }: Props) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [filename, setFilename] = useState<string | null>(null)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    const file = inputRef.current?.files?.[0]
    if (!file) {
      setError('Choose an SVG file first.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const result = await parseSvg(file, strategy)
      setFilename(file.name)
      onParsed(file, result)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="upload-step" onSubmit={handleSubmit}>
      <label>
        Plate SVG:&nbsp;
        <input ref={inputRef} type="file" accept=".svg,image/svg+xml" disabled={busy} />
      </label>
      <button type="submit" disabled={busy}>
        {busy ? 'Parsing…' : 'Parse'}
      </button>
      {filename && !busy && !error && <span className="ok">Loaded {filename}</span>}
      {error && <span className="err">{error}</span>}
    </form>
  )
}
