# Keyboard KiCad AutoDesigner

Web app that turns a kbplate.ai03.com keyboard plate SVG into a complete KiCad project (schematic + PCB layout). See the project plan in Beaver-Blueprint (`keyboard-kicad-autodesigner`) for the full 5-phase scope.

This repo currently implements **Phase 1 — SVG parser**: upload an SVG, see switches and stabilizers detected and overlaid on the plate.

## Run with Docker Compose

```sh
docker compose up --build
```

- Frontend: http://localhost:8011
- Backend:  http://localhost:8010 (health: `/api/health`)

Override ports via `BACKEND_PORT` / `FRONTEND_PORT` env vars.

## Local development

Backend:

```sh
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --reload --port 8010
pytest
```

Frontend:

```sh
cd frontend
npm install
npm run dev   # http://localhost:5173, proxies /api to localhost:8010
```

## Layout

```
backend/   FastAPI + svgpathtools — /api/parse, /api/health
frontend/  React + Vite + TS — upload step + annotated preview
```

## Roadmap (next passes)

- Phase 2: matrix row/column assignment + drag-to-reassign UI
- Phase 3: schematic generation via SKiDL (Cherry MX + 1N4148 diodes)
- Phase 4: PCB layout `.kicad_pcb` writer (S-expression), uses Acheron MXH stabilizer/switch footprints + Waveshare RP2040-Zero MCU
- Phase 5: ZIP export bundling `.kicad_pro`, `.kicad_sch`, `.kicad_pcb`
