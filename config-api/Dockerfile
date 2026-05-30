# ── Stage 1: build the React flow editor ─────────────────────────────────────
FROM node:20-slim AS frontend
WORKDIR /build
COPY flow-editor/package*.json ./
RUN npm ci --prefer-offline
COPY flow-editor/ ./
RUN npm run build
# Output lands in /build/../static/flow-editor = /static/flow-editor relative to parent

# ── Stage 2: Python app ───────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Copy the built frontend bundle (produced by stage 1 into ../static/flow-editor)
COPY --from=frontend /static/flow-editor ./static/flow-editor

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]
