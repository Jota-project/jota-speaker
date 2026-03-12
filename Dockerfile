# ── builder ───────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build deps
RUN pip install --upgrade pip hatchling

COPY pyproject.toml ./
COPY src/ ./src/

# Install production dependencies into a prefix we can copy
RUN pip install --prefix=/install --no-cache-dir ".[dev]" || \
    pip install --prefix=/install --no-cache-dir .

# ── runtime ───────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY --from=builder /app/src ./src

# Kokoro models are expected to be mounted at runtime, not baked in
VOLUME ["/models"]

ENV JOTA_ENGINE=mock \
    JOTA_KOKORO_MODEL=/models/kokoro-v0_19.onnx \
    JOTA_AUTH_PROVIDER=stub

EXPOSE 8002

CMD ["python", "-m", "src.main"]
