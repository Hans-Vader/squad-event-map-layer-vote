FROM python:3.14-alpine

# Set environment variables to optimize Python for Docker
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# UID/GID from .env (default 1000)
ARG PUID
ARG PGID

# Set the working directory inside the container
WORKDIR /app

# Install Python dependencies (build deps added temporarily for any source builds)
RUN apk add --no-cache --virtual .build-deps \
        build-base \
        libffi-dev \
    && pip install --no-cache-dir \
        discord.py>=2.0.0 \
        python-dotenv>=0.19.2 \
        aiohttp>=3.8.1 \
        pynacl>=1.5.0 \
    && apk del .build-deps

# Copy the source code from the DebugScriptHelper directory to /app
COPY DebugScriptHelper/ .

# Create app user with host UID/GID and data directory
RUN addgroup -g "$PGID" appuser && \
    adduser -u "$PUID" -G appuser -D appuser && \
    mkdir -p /app/data && \
    chown -R "$PUID:$PGID" /app

USER appuser

CMD ["python", "bot.py"]
