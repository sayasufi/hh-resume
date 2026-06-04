FROM python:3.13-slim

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
  gcc \
  libc6-dev \
  procps \
  dos2unix \
  tzdata \
  less \
  && rm -rf /var/lib/apt/lists/*

# Настройка пользователя
ARG UID=1000
ARG GID=1000
RUN groupadd -g $GID docker && \
  useradd -u $UID -g docker -m -s /bin/bash docker

WORKDIR /app

# Копируем файлы пакета
COPY src /app/src
COPY pyproject.toml poetry.lock* README.md /app/

# И ставим его
RUN pip install --no-cache-dir -e '.[playwright,pillow]'

# Ставим зависимости хромиума и сам хромиум пользователю docker
RUN playwright install-deps chromium && \
  su docker -c "playwright install chromium"

# Каталог config создаётся пустым; конфиг/секреты НЕ бакаются в образ —
# всё состояние в Postgres, а логи пишутся в bind-mount /app/config.
RUN mkdir -p /app/config



# CMD задаётся в docker-compose.yml для каждого сервиса (web/listener/orchestrator)
