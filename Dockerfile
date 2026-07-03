FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NPM_CONFIG_CACHE=/home/localforge/.npm

WORKDIR /app

COPY pyproject.toml README.md ./
COPY localforge ./localforge
COPY docker-entrypoint.sh /usr/local/bin/localforge-entrypoint

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      docker-cli \
      git \
      nodejs \
      npm \
    && rm -rf /var/lib/apt/lists/* \
    && chmod 0755 /usr/local/bin/localforge-entrypoint \
    && python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir .

RUN useradd --create-home --shell /bin/bash localforge \
    && mkdir -p /workspace /home/localforge/.npm \
    && chown -R localforge:localforge /workspace /home/localforge/.npm
WORKDIR /workspace

ENTRYPOINT ["localforge-entrypoint", "localforge"]
CMD ["--help"]
