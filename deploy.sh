#!/usr/bin/env bash
set -euo pipefail

# ============================================================
#  EVA Risk Assessor — Deploy Script
# ============================================================

REPO_URL="https://github.com/sircuspelle/cosmohackaton.git"
PROJECT_DIR="/opt/cosmohackaton"
HEALTH_URL="http://localhost/health"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}    $*"; }
success() { echo -e "${GREEN}[OK]${NC}      $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}    $*"; }
error()   { echo -e "${RED}[ERROR]${NC}   $*"; }
step()    { echo -e "\n${CYAN}${BOLD}=> $*${NC}"; }
divider() { echo -e "${BLUE}────────────────────────────────────────────${NC}"; }

wait_healthy() {
    local max_wait=60
    local waited=0
    info "Ожидание готовности сервера (макс. ${max_wait} сек)..."
    while [ $waited -lt $max_wait ]; do
        if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
            success "Сервер готов (${waited} сек)"
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
        printf "."
    done
    echo ""
    error "Сервер не ответил за ${max_wait} сек"
    return 1
}

cmd_deploy() {
    echo ""
    echo -e "${BOLD}${CYAN}"
    echo "  ╔═══════════════════════════════════════════╗"
    echo "  ║      EVA Risk Assessor — DEPLOY           ║"
    echo "  ╚═══════════════════════════════════════════╝"
    echo -e "${NC}"

    step "1/4 Клонирование репозитория"
    if [ -d "$PROJECT_DIR/.git" ]; then
        warn "Директория $PROJECT_DIR уже существует — git pull"
        cd "$PROJECT_DIR"
        git pull 2>/dev/null || warn "git pull не удался, используем текущую версию"
    else
        info "Клонируем $REPO_URL ..."
        git clone "$REPO_URL" "$PROJECT_DIR"
        cd "$PROJECT_DIR"
        success "Репозиторий клонирован"
    fi

    step "2/4 Проверка конфигурации"
    if [ -f "config/events/config.json" ]; then
        success "config/events/config.json"
        cat config/events/config.json
    else
        error "config/events/config.json не найден"
        exit 1
    fi

    step "3/4 Сборка и запуск"
    docker compose up --build -d
    success "Контейнеры запущены"

    step "4/4 Проверка"
    docker compose ps
    echo ""
    wait_healthy

    local IP
    IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
    echo ""
    divider
    echo -e "${GREEN}${BOLD}"
    echo "  ╔═══════════════════════════════════════════╗"
    echo "  ║           DEPLOY ЗАВЕРШЁН                 ║"
    echo "  ╠═══════════════════════════════════════════╣"
    echo "  ║  API:     http://$IP:80                   ║"
    echo "  ║  Health:  http://$IP/health               ║"
    echo "  ╚═══════════════════════════════════════════╝"
    echo -e "${NC}"
}

cmd_stop() {
    step "Остановка"
    docker compose down
    success "Остановлено"
}

cmd_restart() {
    step "Перезапуск"
    docker compose restart
    success "Перезапущено"
    divider
    curl -sf "$HEALTH_URL" 2>/dev/null && echo "" && success "Сервис доступен" || error "Сервис недоступен"
}

cmd_update() {
    cd "$PROJECT_DIR"
    step "Pull"
    git pull 2>/dev/null || warn "Нет новых изменений"
    step "Пересборка"
    docker compose up --build -d
    success "Обновлено"
    docker compose ps
    echo ""
    curl -sf "$HEALTH_URL" 2>/dev/null && echo "" && success "Сервис доступен" || error "Сервис недоступен"
}

cmd_logs() {
    docker compose logs -f ${1:-}
}

cmd_status() {
    echo ""
    step "Контейнеры"
    docker compose ps
    echo ""
    divider
    curl -sf "$HEALTH_URL" 2>/dev/null && echo "" && success "Сервис доступен" || error "Сервис недоступен"
    echo ""
    divider
    docker stats --no-stream --format "  {{.Name}}  CPU: {{.CPUPerc}}  MEM: {{.MemUsage}}" 2>/dev/null || true
    echo ""
}

cmd_clean() {
    warn "Удалит контейнеры и volume (включая SQLite-базу)!"
    read -rp "Продолжить? [y/N] " confirm
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
        docker compose down -v
        success "Очищено"
    else
        info "Отмена"
    fi
}

cmd_help() {
    echo ""
    echo -e "${BOLD}EVA Risk Assessor — Deploy Script${NC}"
    echo ""
    echo "Использование: ./deploy.sh <команда>"
    echo ""
    echo "Команды:"
    echo -e "  ${GREEN}deploy${NC}     Клонирование + сборка + запуск"
    echo -e "  ${GREEN}update${NC}     Pull + пересборка"
    echo -e "  ${GREEN}stop${NC}       Остановка"
    echo -e "  ${GREEN}restart${NC}    Перезапуск"
    echo -e "  ${GREEN}status${NC}     Статус и healthcheck"
    echo -e "  ${GREEN}logs${NC}       Логи (logs server / logs nginx)"
    echo -e "  ${GREEN}clean${NC}      Полная очистка"
    echo -e "  ${GREEN}help${NC}       Справка"
    echo ""
}

case "${1:-help}" in
    deploy)  cmd_deploy  ;;
    stop)    cmd_stop    ;;
    restart) cmd_restart ;;
    update)  cmd_update  ;;
    logs)    cmd_logs "${2:-}" ;;
    status)  cmd_status  ;;
    clean)   cmd_clean   ;;
    help|*)  cmd_help    ;;
esac
