export function OrientationHelp() {
  return (
    <details className="help" open>
      <summary>How do the orientation indicators work?</summary>
      <div className="help-body">
        <section className="help-section">
          <SwitchDiagram />
          <div className="help-text">
            <h4 className="help-h4">Switches</h4>
            <p>
              Each switch has a small <strong>green tick</strong> radiating from
              its center. The tick points toward the <strong>pin side</strong>{' '}
              of the switch — the edge where the legs and the center alignment
              post stick out the back of the housing. The opposite edge is the
              top, where the LED window sits on most Cherry-style housings.
            </p>
            <p>
              The parser detects cutout rotation modulo 90°, since a
              14×14&nbsp;mm cutout looks the same every quarter turn. <strong>You
              </strong> pick which way the pins should point in 90° increments —
              click a dot to cycle <code>+90°</code>, shift-click for{' '}
              <code>−90°</code>.
            </p>
            <ul className="rotation-cheat">
              <li><span className="swatch r0" /> 0° — pins down</li>
              <li><span className="swatch r90" /> 90° — pins left</li>
              <li><span className="swatch r180" /> 180° — pins up</li>
              <li><span className="swatch r270" /> 270° — pins right</li>
            </ul>
            <p>
              For switches flanked by a pair of stabilizers (spacebars, long
              shifts, etc.), the parser picks a default that puts the pin side{' '}
              <em>perpendicular</em> to the stab axis, so the pins never point
              into a stabilizer footprint.
            </p>
          </div>
        </section>

        <section className="help-section">
          <StabDiagram />
          <div className="help-text">
            <h4 className="help-h4">Stabilizers</h4>
            <p>
              Stabilizers are blue rectangles with a small{' '}
              <span className="stab-arrow-inline" /> arrowhead at one of the two
              narrow edges — the <em>head end</em>. Plate-mount Cherry
              stabilizers are asymmetric (the wire-bend slot vs the housing
              socket), so the layout generator places that asymmetry to match
              this marker.
            </p>
            <p>
              In a typical kbplate layout the stab cutouts are vertical slots
              positioned alongside the spacebar (long axis perpendicular to the
              line connecting them to the switch). Both stabs in a pair get the
              same default head direction so the layout stays consistent — if
              your footprint library expects the opposite convention, click any
              stab to flip&nbsp;180°.
            </p>
          </div>
        </section>
      </div>
    </details>
  )
}

function SwitchDiagram() {
  return (
    <svg
      className="help-svg"
      viewBox="-14 -14 28 32"
      width="160"
      height="180"
      xmlns="http://www.w3.org/2000/svg"
      aria-label="Diagram showing the switch orientation convention"
    >
      <rect
        x={-7}
        y={-7}
        width={14}
        height={14}
        rx={0.6}
        ry={0.6}
        fill="rgba(40,180,70,0.10)"
        stroke="rgba(20,120,50,0.9)"
        strokeWidth={0.35}
      />
      <text
        x={0}
        y={-3.5}
        textAnchor="middle"
        fontSize={2.4}
        fill="#9aa1a8"
        fontFamily="system-ui, sans-serif"
      >
        top of switch
      </text>
      <text
        x={0}
        y={-1}
        textAnchor="middle"
        fontSize={2}
        fill="#788088"
        fontFamily="system-ui, sans-serif"
      >
        (LED side)
      </text>

      <line
        x1={0}
        y1={0}
        x2={0}
        y2={6.3}
        stroke="rgba(20,120,50,1)"
        strokeWidth={0.9}
        strokeLinecap="round"
      />
      <circle
        cx={0}
        cy={0}
        r={1.1}
        fill="rgba(40,180,70,0.9)"
        stroke="white"
        strokeWidth={0.2}
      />
      <line
        x1={0}
        y1={6.3}
        x2={-1.2}
        y2={5.0}
        stroke="rgba(20,120,50,1)"
        strokeWidth={0.7}
        strokeLinecap="round"
      />
      <line
        x1={0}
        y1={6.3}
        x2={1.2}
        y2={5.0}
        stroke="rgba(20,120,50,1)"
        strokeWidth={0.7}
        strokeLinecap="round"
      />

      <text
        x={0}
        y={10.8}
        textAnchor="middle"
        fontSize={2.4}
        fill="#9aa1a8"
        fontFamily="system-ui, sans-serif"
      >
        pin side
      </text>
      <text
        x={0}
        y={13.3}
        textAnchor="middle"
        fontSize={2}
        fill="#788088"
        fontFamily="system-ui, sans-serif"
      >
        (tick points here)
      </text>
    </svg>
  )
}

function StabDiagram() {
  const stabLong = 14
  const stabShort = 7
  const headOffset = stabLong / 2 - stabShort * 0.35
  const tipX = stabLong / 2 - stabShort * 0.05

  function StabBody({
    cx,
    cy,
    rotationDeg,
  }: {
    cx: number
    cy: number
    rotationDeg: number
  }) {
    return (
      <g transform={`rotate(${rotationDeg} ${cx} ${cy})`}>
        <rect
          x={cx - stabLong / 2}
          y={cy - stabShort / 2}
          width={stabLong}
          height={stabShort}
          rx={0.4}
          ry={0.4}
          fill="rgba(50,110,220,0.18)"
          stroke="rgba(50,110,220,0.85)"
          strokeWidth={0.3}
        />
        <polygon
          points={[
            `${cx + headOffset},${cy - stabShort * 0.28}`,
            `${cx + headOffset},${cy + stabShort * 0.28}`,
            `${cx + tipX},${cy}`,
          ].join(' ')}
          fill="rgba(30,80,200,0.95)"
          stroke="white"
          strokeWidth={0.15}
        />
        <circle
          cx={cx}
          cy={cy}
          r={0.85}
          fill="rgba(50,110,220,0.9)"
          stroke="white"
          strokeWidth={0.18}
        />
      </g>
    )
  }

  return (
    <div className="help-diagram-frame">
      <svg
        className="help-svg-inner"
        viewBox="-22 -13 44 24"
        width="280"
        height="153"
        xmlns="http://www.w3.org/2000/svg"
        aria-label="Diagram showing the stabilizer orientation convention"
      >
        <rect
          x={-7}
          y={-7}
          width={14}
          height={14}
          rx={0.6}
          ry={0.6}
          fill="rgba(40,180,70,0.10)"
          stroke="rgba(20,120,50,0.9)"
          strokeWidth={0.35}
        />
        <line
          x1={0}
          y1={0}
          x2={0}
          y2={5.5}
          stroke="rgba(20,120,50,1)"
          strokeWidth={0.7}
          strokeLinecap="round"
        />
        <circle
          cx={0}
          cy={0}
          r={0.9}
          fill="rgba(40,180,70,0.9)"
          stroke="white"
          strokeWidth={0.18}
        />

        <StabBody cx={-15} cy={1.5} rotationDeg={270} />
        <StabBody cx={15} cy={1.5} rotationDeg={270} />

        <text
          x={-15}
          y={-9.5}
          textAnchor="middle"
          fontSize={1.8}
          fill="#788088"
          fontFamily="system-ui, sans-serif"
        >
          stab
        </text>
        <text
          x={0}
          y={-9.5}
          textAnchor="middle"
          fontSize={1.8}
          fill="#788088"
          fontFamily="system-ui, sans-serif"
        >
          switch
        </text>
        <text
          x={15}
          y={-9.5}
          textAnchor="middle"
          fontSize={1.8}
          fill="#788088"
          fontFamily="system-ui, sans-serif"
        >
          stab
        </text>
      </svg>
      <p className="help-caption">
        long axis perpendicular to switch · click any stab to flip its head end
      </p>
    </div>
  )
}
