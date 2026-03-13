# ── builder ───────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

RUN pip install --upgrade pip hatchling

COPY pyproject.toml ./
COPY src/ ./src/

RUN pip install --prefix=/install --no-cache-dir .

# ── runtime ───────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# espeak-ng required by kokoro-onnx phonemizer
RUN apt-get update && apt-get install -y --no-install-recommends espeak-ng && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY --from=builder /app/src ./src

# Models are mounted at runtime via volume or bind-mount
VOLUME ["/models"]

ENV JOTA_ENGINE=mock \
    JOTA_KOKORO_MODEL=/models/kokoro-v1.0.int8.onnx \
    JOTA_KOKORO_VOICES=/models/voices-v1.0.bin \
    JOTA_KOKORO_VOICE=af_heart \
    JOTA_KOKORO_LANG=en-us \
    JOTA_AUTH_PROVIDER=stub

EXPOSE 8005

CMD ["python", "-m", "src.main"]
