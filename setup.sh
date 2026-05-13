#!/bin/bash

# 🚀 Setup скрипт для MoneyMaker AI-Agent

echo "=========================================="
echo "🚀 MoneyMaker AI-Agent Setup"
echo "=========================================="
echo ""

# Проверка Python
echo "📋 Checking Python version..."
python3 --version

if [ $? -ne 0 ]; then
    echo "❌ Python 3 не установлен!"
    exit 1
fi

# Установка зависимостей
echo ""
echo "📦 Installing dependencies..."
pip3 install -r requirements.txt

if [ $? -ne 0 ]; then
    echo "❌ Failed to install dependencies"
    exit 1
fi

# Копирование .env
echo ""
echo "⚙️  Setting up .env file..."

if [ ! -f .env ]; then
    cp .env.example .env
    echo "✅ Created .env file from .env.example"
    echo "⚠️  Please edit .env file with your credentials:"
    echo "   - TELEGRAM_API_ID"
    echo "   - TELEGRAM_API_HASH"
    echo "   - OPENROUTER_API_KEY"
else
    echo "✅ .env file already exists"
fi

# Проверка конфигурации
echo ""
echo "🔍 Checking configuration..."

source .env

if [ -z "$TELEGRAM_API_ID" ] || [ "$TELEGRAM_API_ID" = "1234567" ]; then
    echo "⚠️  TELEGRAM_API_ID not set! Get it from https://my.telegram.org/apps"
fi

if [ -z "$TELEGRAM_API_HASH" ] || [ "$TELEGRAM_API_HASH" = "YOUR_API_HASH" ]; then
    echo "⚠️  TELEGRAM_API_HASH not set! Get it from https://my.telegram.org/apps"
fi

if [ -z "$OPENROUTER_API_KEY" ]; then
    echo "⚠️  OPENROUTER_API_KEY not set! Get it from https://openrouter.ai"
fi

echo ""
echo "=========================================="
echo "✅ Setup completed!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "1. Edit .env file with your credentials"
echo "2. Run: python main.py"
echo ""
echo "For more info, see README.md"
echo ""
