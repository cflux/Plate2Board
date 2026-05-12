export type MatrixStrategy = 'row_first' | 'column_first' | 'stagger_aware' | 'auto'

export type SwitchType = 'soldered' | 'hotswap'

export type DiodeType = 'tht' | 'smd'

export type StabilizerType = 'pcb_mount' | 'plate_mount'

export interface SwitchDef {
  id: number
  cx_mm: number
  cy_mm: number
  rotation_deg: number
  row: number
  col: number
}

export interface StabilizerDef {
  id: number
  cx_mm: number
  cy_mm: number
  width_mm: number
  height_mm: number
  rotation_deg: number
}

export interface PcbOutline {
  width_mm: number
  height_mm: number
  path_d: string
}

export interface MountingHoleDef {
  id: number
  cx_mm: number
  cy_mm: number
  diameter_mm: number
}

export interface UnclassifiedShape {
  id: number
  cx_mm: number
  cy_mm: number
  width_mm: number
  height_mm: number
  rotation_deg: number
}

export interface McuPlacement {
  cx_mm: number
  cy_mm: number
  rotation_deg: number
}

export interface ParseResult {
  svg_width_mm: number
  svg_height_mm: number
  pcb_outline: PcbOutline
  switches: SwitchDef[]
  stabilizers: StabilizerDef[]
  mounting_holes: MountingHoleDef[]
  unclassified: UnclassifiedShape[]
  mcu_placement: McuPlacement | null
  outline_grow_mm: number
  matrix_strategy: MatrixStrategy
}
