FROM python:3.12-slim

WORKDIR /app

# KiCad symbol libraries are needed by SKiDL to generate .kicad_sch.
RUN apt-get update \
    && apt-get install -y --no-install-recommends kicad-symbols \
    && rm -rf /var/lib/apt/lists/*
ENV KICAD_SYMBOL_DIR=/usr/share/kicad/symbols

# Custom keyboard MCU symbols (ProMicro etc.) — bundled with the image so
# SKiDL can resolve `Keyboard_MCU:ProMicro` without external lib paths.
COPY kicad_symbols/ /opt/keeb-symbols/
ENV KEEB_SYMBOL_DIR=/opt/keeb-symbols

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY app/ app/
RUN date -u +%Y-%m-%dT%H:%M:%SZ > /app/BUILD_TIME

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
