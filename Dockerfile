FROM python:3.11-slim

ARG CODEX_NPM_PACKAGE=@openai/codex

WORKDIR /app

# System dependencies for Codex CLI + Python app
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    npm \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Codex CLI
RUN npm install -g ${CODEX_NPM_PACKAGE}

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
