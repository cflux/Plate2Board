import { useEffect, useRef, useState } from 'react'
import type { ChangeEvent, KeyboardEvent } from 'react'

/** Controlled numeric input that doesn't fight the user's typing.
 *
 * The classic React pitfall: bind `value={n.toFixed(2)}` and every
 * keystroke re-formats the field mid-type, dropping characters and
 * jumping the caret. This component keeps a local draft string while the
 * input is focused, parses on each keystroke (so external listeners can
 * react live), and re-syncs from the `value` prop only when the input
 * is NOT focused — so an external drag or another component can still
 * update the field without yanking it away from the user. */
interface NumberInputProps {
  value: number
  onChange: (n: number) => void
  step?: number | string
  min?: number
  max?: number
  decimals?: number
  className?: string
  title?: string
  disabled?: boolean
}

export function NumberInput({
  value,
  onChange,
  step,
  min,
  max,
  decimals = 2,
  className,
  title,
  disabled,
}: NumberInputProps) {
  const [draft, setDraft] = useState<string>(() => format(value, decimals))
  const focusedRef = useRef(false)

  // Keep the draft in sync with external value changes — but only when
  // the user isn't actively typing in this field.
  useEffect(() => {
    if (focusedRef.current) return
    setDraft(format(value, decimals))
  }, [value, decimals])

  function handleChange(e: ChangeEvent<HTMLInputElement>) {
    const next = e.target.value
    setDraft(next)
    // Commit on every keystroke if the draft parses, so live updates
    // (drag, dependent computations) stay synced. Empty / partial inputs
    // (e.g. "-", ".") just don't fire onChange.
    const n = parseFloat(next)
    if (Number.isFinite(n)) onChange(n)
  }

  function handleBlur() {
    focusedRef.current = false
    const n = parseFloat(draft)
    if (Number.isFinite(n)) {
      onChange(n)
      setDraft(format(n, decimals))
    } else {
      setDraft(format(value, decimals))
    }
  }

  function handleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter') {
      ;(e.target as HTMLInputElement).blur()
    }
  }

  return (
    <input
      type="number"
      step={step}
      min={min}
      max={max}
      value={draft}
      onChange={handleChange}
      onFocus={() => {
        focusedRef.current = true
      }}
      onBlur={handleBlur}
      onKeyDown={handleKeyDown}
      className={className}
      title={title}
      disabled={disabled}
    />
  )
}

function format(value: number, decimals: number): string {
  if (!Number.isFinite(value)) return ''
  // Strip insignificant trailing zeros so "10.00" doesn't end up in a
  // freshly-rendered field where it would be confusing to extend.
  const fixed = value.toFixed(decimals)
  return fixed.includes('.') ? fixed.replace(/\.?0+$/, '') || '0' : fixed
}
