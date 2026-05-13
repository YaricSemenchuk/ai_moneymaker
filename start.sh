#!/bin/bash
# 🚀 Запуск MoneyMaker AI-Agent + Dashboard одной командой

cd "$(dirname "$0")"

# Цвета для красивого вывода
GREEN='\033[0;32m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${PURPLE}=================================================${NC}"
echo -e "${PURPLE}🤖 MoneyMaker AI-Agent + Dashboard${NC}"
echo -e "${PURPLE}=================================================${NC}"

# Проверка venv
if [ ! -d "venv" ]; then
    echo -e "${RED}❌ Виртуальное окружение не найдено!${NC}"
    echo -e "${YELLOW}Запусти сначала: ./setup.sh${NC}"
    exit 1
fi

# Проверка .env
if [ ! -f ".env" ]; then
    echo -e "${RED}❌ Файл .env не найден!${NC}"
    echo -e "${YELLOW}Запусти сначала: ./setup.sh${NC}"
    exit 1
fi

# Активируем venv
source venv/bin/activate

# Создаем директорию для логов
mkdir -p logs

# Cleanup function — корректно убивает все процессы при выходе
cleanup() {
    echo ""
    echo -e "${YELLOW}🛑 Остановка процессов...${NC}"

    # Убиваем по PID (мягко, потом принудительно)
    if [ ! -z "$DASHBOARD_PID" ]; then
        kill $DASHBOARD_PID 2>/dev/null
        sleep 0.5
        kill -9 $DASHBOARD_PID 2>/dev/null
        echo -e "${GREEN}  ✓ Dashboard остановлен${NC}"
    fi

    if [ ! -z "$AGENT_PID" ]; then
        kill $AGENT_PID 2>/dev/null
        sleep 0.5
        kill -9 $AGENT_PID 2>/dev/null
        echo -e "${GREEN}  ✓ Agent остановлен${NC}"
    fi

    # Принудительная очистка ВСЕХ python процессов проекта
    # (на случай если осталось от прошлых запусков с Ctrl+Z)
    pkill -9 -f "python.*main.py" 2>/dev/null
    pkill -9 -f "python.*multi_agent" 2>/dev/null
    pkill -9 -f "python.*dashboard" 2>/dev/null

    # Дочерние процессы
    pkill -P $$ 2>/dev/null

    echo -e "${PURPLE}=================================================${NC}"
    echo -e "${GREEN}👋 До свидания!${NC}"
    exit 0
}

# Перехватываем ВСЕ сигналы которые могут прервать скрипт:
# INT  = Ctrl+C
# TERM = kill
# HUP  = закрытие терминала
# QUIT = Ctrl+\
# EXIT = любой выход
trap cleanup INT TERM HUP QUIT
trap cleanup EXIT

# Если уже есть запущенные процессы - убираем их перед стартом
EXISTING=$(pgrep -f "python.*(main.py|multi_agent|dashboard)" | wc -l | xargs)
if [ "$EXISTING" -gt "0" ]; then
    echo -e "${YELLOW}⚠️  Найдено $EXISTING зависших процессов — убираю...${NC}"
    pkill -9 -f "python.*main.py" 2>/dev/null
    pkill -9 -f "python.*multi_agent" 2>/dev/null
    pkill -9 -f "python.*dashboard" 2>/dev/null
    sleep 1
    echo -e "${GREEN}✅ Очищено${NC}"
    echo ""
fi

# Запуск Dashboard в фоне
echo -e "${BLUE}📊 Запускаю Dashboard...${NC}"
python dashboard.py > logs/dashboard.log 2>&1 &
DASHBOARD_PID=$!
sleep 2

# Проверка что dashboard запустился
if ! ps -p $DASHBOARD_PID > /dev/null; then
    echo -e "${RED}❌ Dashboard не запустился! Смотри logs/dashboard.log${NC}"
    cat logs/dashboard.log
    exit 1
fi

echo -e "${GREEN}  ✅ Dashboard запущен (PID: $DASHBOARD_PID)${NC}"
echo -e "${GREEN}  🌐 http://localhost:5001${NC}"
echo ""

# Проверяем есть ли аккаунты в БД
ACCOUNT_COUNT=$(python -c "
import sqlite3
from config_agent import DB_PATH
try:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Только реальные аккаунты (не placeholder)
    c.execute(\"SELECT COUNT(*) FROM agent_accounts WHERE phone_number != 'placeholder'\")
    print(c.fetchone()[0])
    conn.close()
except Exception as e:
    print(0)
" 2>/dev/null)

if [ "$ACCOUNT_COUNT" -eq "0" ]; then
    echo -e "${YELLOW}⚠️  В БД нет аккаунтов! Запускаю однопользовательский режим (main.py)${NC}"
    echo -e "${YELLOW}   Чтобы добавить новые аккаунты: python add_account.py${NC}"
    echo ""

    python main.py 2>&1 | tee logs/agent.log &
    AGENT_PID=$!
else
    echo -e "${GREEN}✅ Найдено аккаунтов: $ACCOUNT_COUNT${NC}"
    echo -e "${BLUE}🤖🤖🤖 Запускаю Multi-Agent...${NC}"
    echo -e "${PURPLE}=================================================${NC}"
    echo ""

    python multi_agent.py 2>&1 | tee logs/agent.log &
    AGENT_PID=$!
fi

# Ждем завершения агента (или Ctrl+C)
wait $AGENT_PID

# Если агент сам завершился — останавливаем dashboard
cleanup
