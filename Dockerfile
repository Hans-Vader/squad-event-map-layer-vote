FROM python:3.12-slim

# Set environment variables to optimize Python for Docker
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# UID/GID from .env (default 1000)
ARG PUID
ARG PGID

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies directly
RUN pip install --no-cache-dir \
    discord.py>=2.0.0 \
    python-dotenv>=0.19.2 \
    aiohttp>=3.8.1 \
    pynacl>=1.5.0

# Copy the source code from the DebugScriptHelper directory to /app
COPY DebugScriptHelper/ .

# Create app user with host UID/GID and data directory
RUN groupadd -g "$PGID" appuser && \
    useradd -u "$PUID" -g "$PGID" -m appuser && \
    mkdir -p /app/data && \
    chown -R "$PUID:$PGID" /app

USER appuser

CMD ["python", "bot.py"]
