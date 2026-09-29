#!/usr/bin/env bash
# Выкладка hh-applicant-tool на сервер из git (каталог сервера — git-чекаут ветки).
#
#   ssh ds1908 /var/www1/hh-applicant-tool/scripts/deploy.sh [ветка]   # по умолчанию pg-async
#
# 1. fetch; уже на последней ревизии — выходим.
# 2. На сервере есть незакоммиченные правки в отслеживаемых файлах — стоп (ничего не затираем).
# 3. Тесты НОВОЙ ревизии в отдельном worktree и одноразовом контейнере; упали — стоп.
# 4. Только fast-forward.
# 5. Перезапуск того, что держит код в памяти: orchestration -> hh-orchestrator,
#    web_app/webapp_static -> hh-web, tg_connect_bot -> hh-listener. Остальное (операции,
#    services/*) джобы подхватывают сами на следующем запуске.
#    Изменились образ/compose — только предупреждение: пересборку делаем руками.
# Откат: git reset --hard <старый sha> && docker compose restart <сервисы>.
set -euo pipefail
cd "$(dirname "$0")/.."
BRANCH="${1:-pg-async}"
IMAGE="hh_applicant_tool:latest"

git fetch --quiet origin "$BRANCH"
OLD=$(git rev-parse HEAD)
NEW=$(git rev-parse "origin/$BRANCH")
if [ "$OLD" = "$NEW" ]; then
  echo "уже на ${NEW:0:7} — выкладывать нечего"
  exit 0
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "СТОП: на сервере незакоммиченные правки — сначала разберись с ними:" >&2
  git status --short --untracked-files=no >&2
  exit 1
fi

CHANGED=$(git diff --name-only "$OLD" "$NEW")
echo "выкладка ${OLD:0:7} -> ${NEW:0:7}:"
git log --oneline "$OLD..$NEW" | sed 's/^/  /'

TMP=$(mktemp -d /tmp/hh-deploy.XXXXXX)
cleanup() { git worktree remove --force "$TMP" >/dev/null 2>&1 || rm -rf "$TMP"; }
trap cleanup EXIT
git worktree add --detach --quiet "$TMP" "$NEW"
echo "тесты ${NEW:0:7}…"
docker run --rm -v "$TMP":/app -w /app -e PYTHONPATH=/app/src:/app "$IMAGE" \
  sh -c "pip install -q pytest >/dev/null 2>&1; python -m pytest -q -p no:cacheprovider tests" \
  || { echo "СТОП: тесты упали — ничего не выложено" >&2; exit 1; }

git merge --ff-only --quiet "$NEW"
echo "код обновлён до ${NEW:0:7}"

restart=()
grep -q '^orchestration/' <<<"$CHANGED" && restart+=(hh-orchestrator)
grep -qE '^(services/web_app\.py|webapp_static/)' <<<"$CHANGED" && restart+=(hh-web)
grep -q '^services/tg_connect_bot\.py$' <<<"$CHANGED" && restart+=(hh-listener)
if grep -qE '^(Dockerfile|pyproject\.toml|poetry\.lock|docker-compose\.yml)$' <<<"$CHANGED"; then
  echo "ВНИМАНИЕ: изменились образ/compose — пересобери вручную: docker compose build && docker compose up -d"
fi
if [ ${#restart[@]} -gt 0 ]; then
  echo "перезапуск: ${restart[*]}"
  docker compose restart "${restart[@]}"
else
  echo "перезапуск не нужен — джобы возьмут новый код на следующем запуске"
fi
echo "готово"
