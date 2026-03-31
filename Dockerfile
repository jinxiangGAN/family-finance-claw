FROM node:20-bookworm-slim AS codex-runtime

ARG CODEX_NPM_PACKAGE=@openai/codex

RUN npm install -g ${CODEX_NPM_PACKAGE}

FROM python:3.11-slim-bookworm

WORKDIR /app

# Keep system package usage minimal to avoid slow apt mirrors during cloud builds.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Bring in the preinstalled Node runtime + Codex CLI from the Node stage.
COPY --from=codex-runtime /usr/local/ /usr/local/

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy repository files needed at runtime
COPY app/ ./app/
COPY AGENTS.md ./AGENTS.md
COPY README.md ./README.md
COPY .env.example ./.env.example

# Runtime directories
RUN mkdir -p /app/data /codex-home

# Default environment
ENV DATABASE_PATH=/app/data/expenses.db
ENV TIMEZONE=Asia/Singapore
ENV CURRENCY=SGD
ENV CODEX_HOME=/codex-home
ENV CODEX_WORKDIR=/app

CMD ["python", "-m", "app.main"]
