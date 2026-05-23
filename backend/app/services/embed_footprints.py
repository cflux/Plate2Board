"""Convert the inline (footprint …) blocks in a generated kicad_pcb into
stand-alone .kicad_mod template files that ship in the project's embedded
`footprints.pretty` library.

Why this exists
---------------
KiCad's "Update PCB from Schematic" sync resolves every component's
footprint name against the active fp-lib-table. Even if the .kicad_pcb
already contains a perfectly-good inline footprint, KiCad still needs the
named footprint to be discoverable in a library, or sync fails per-symbol
with ``Cannot update SW28 (footprint 'keeb:…' not found)``.

We don't ship the whole KiCad footprint distribution — the user may not
even have KiCad's stock libs installed. Instead we extract one template
per unique footprint name from the PCB we just emitted, write each as a
.kicad_mod under the project's local ``footprints.pretty`` folder, and
add an fp-lib-table entry pointing at it. Whichever footprints actually
appear in the PCB (matrix-dependent — soldered vs hotswap, THT vs SMD,
mounting-hole diameters) are exactly what ships.

Transform: instance → template
-------------------------------
Each inline ``(footprint "lib:Name" …)`` block in the PCB carries
per-instance fields (uuid, world placement, refdes, per-pad net + uuid).
For a library template we keep the geometry / properties and strip the
per-instance bits, then re-wrap with the .kicad_mod header
(``version`` + ``generator``).
"""

from __future__ import annotations

import re

from .pcb import KICAD_PCB_VERSION

GENERATOR_NAME = "keeb-layout-bot"

# Match a top-level (footprint "Lib:Name") opening — we walk from the `(`
# and balance parentheses to find the matching close.
_FOOTPRINT_OPEN_RE = re.compile(r'\(footprint\s+"([^"]+)"', re.MULTILINE)

# Strip ` (net N "name")` from inside a (pad …) line. Net assignment is
# per-instance, not part of the template.
_PAD_NET_RE = re.compile(r'\s*\(net\s+\d+\s+"[^"]*"\)')

# Refdes property line: replace e.g. `"Reference" "SW12"` with
# `"Reference" "REF**"`. Refdes is per-instance; the .kicad_mod uses the
# canonical placeholder so KiCad assigns one on placement.
_REF_PROPERTY_RE = re.compile(r'\(property\s+"Reference"\s+"[A-Z]+\d*"')


def _find_balanced_block(text: str, start: int) -> int:
    """Return the index just past the matching ``)`` for the ``(`` at
    ``text[start]``. Raises if parens never balance (malformed pcb)."""
    depth = 0
    in_string = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError(f"unbalanced parens starting at {start}")


def _bare_name(qualified: str) -> str:
    return qualified.split(":", 1)[-1]


def _to_template(block: str, qualified_name: str) -> str:
    """Strip per-instance fields and re-wrap as .kicad_mod content."""
    bare = _bare_name(qualified_name)

    # 1. Replace the opening with bare name + version/generator. The
    #    original line is `\t(footprint "Lib:Name"`; KiCad accepts the
    #    version/generator tokens immediately after the name on their
    #    own lines.
    block = re.sub(
        r'\(footprint\s+"[^"]+"',
        f'(footprint "{bare}"\n\t(version {KICAD_PCB_VERSION})'
        f'\n\t(generator "{GENERATOR_NAME}")',
        block,
        count=1,
    )

    # 2. Drop the footprint-level (uuid "...") line — it's the instance
    #    identity. Match the indentation pcb.py uses (one extra tab past
    #    the (footprint open).
    block = re.sub(r'\n\s*\(uuid\s+"[^"]+"\)\s*(?=\n)', "", block, count=1)

    # 3. Drop the (at x y rot) line that placed this instance.
    block = re.sub(
        r'\n\s*\(at\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\)\s*(?=\n)',
        "", block, count=1,
    )

    # 4. Reference property: instance refdes → "REF**" placeholder.
    block = _REF_PROPERTY_RE.sub(
        '(property "Reference" "REF**"', block, count=1,
    )

    # 5. Per-pad net assignments are per-instance; drop them.
    block = _PAD_NET_RE.sub("", block)

    return block


def extract_kicad_mod_templates(pcb_text: str) -> dict[str, str]:
    """Walk a generated kicad_pcb, find every ``(footprint "lib:Name" …)``
    block, and return ``{bare_name: kicad_mod_text}`` — one entry per
    unique footprint name (subsequent occurrences are skipped). Templates
    are ready to write as ``{bare_name}.kicad_mod`` inside a .pretty
    folder."""
    out: dict[str, str] = {}
    for m in _FOOTPRINT_OPEN_RE.finditer(pcb_text):
        qualified = m.group(1)
        bare = _bare_name(qualified)
        if bare in out:
            continue
        # Walk back to the `(` that opens this footprint block.
        paren_start = pcb_text.rfind("(", 0, m.start() + 1)
        end = _find_balanced_block(pcb_text, paren_start)
        block = pcb_text[paren_start:end]
        out[bare] = _to_template(block, qualified)
    return out


def fp_lib_table_text(lib_name: str = "keeb", folder: str = "footprints.pretty") -> str:
    """Render the project-root ``fp-lib-table`` text that points KiCad at
    the embedded .pretty folder. ``${KIPRJMOD}`` is KiCad's substitution
    variable for the project directory, so the project relocates safely
    when the user extracts the ZIP anywhere."""
    return (
        "(fp_lib_table\n"
        "\t(version 7)\n"
        f'\t(lib (name "{lib_name}")(type "KiCad")'
        f'(uri "${{KIPRJMOD}}/{folder}")(options "")'
        f'(descr "Embedded keeb-layout-bot footprint library"))\n'
        ")\n"
    )
