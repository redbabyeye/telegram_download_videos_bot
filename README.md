# Telegram Video Bot с админ-панелью, лимитами, рекламой и Premium

## Что умеет

- скачивает публичные видео YouTube / Instagram / TikTok;
- отправляет видео через Telegram как видео со звуком;
- SQLite-база пользователей;
- админ-панель `/panel`;
- рассылка по всем пользователям;
- реклама после скачивания для бесплатных пользователей;
- дневные лимиты для free / premium;
- Premium через ручную выдачу `/grant` или через Telegram Stars `/buy`;
- приоритетная очередь для Premium;
- экспорт пользователей в CSV;
- бан / разбан пользователей.

## Главное про токен

Никогда не вставляй токен в `bot.py` и не загружай `.env` в GitHub.

Если токен уже был в публичном GitHub, перевыпусти его в BotFather:
`/mybots` → нужный бот → `API Token` → `Revoke current token`.

## Локальный запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python bot.py
```

В `.env` вставь:

```env
BOT_TOKEN=твой_токен
ADMIN_IDS=твой_telegram_user_id
```

## Деплой на бесплатный сервер через Koyeb

1. Создай GitHub-репозиторий.
2. Загрузи туда файлы проекта, кроме `.env`, `bot.db`, `cookies.txt`.
3. Koyeb → Create App → GitHub → выбери репозиторий.
4. Builder: Dockerfile.
5. Port: 8080.
6. Environment Variables:

```env
BOT_TOKEN=твой_токен
ADMIN_IDS=твой_telegram_user_id
PORT=8080
WORKERS=2
DB_PATH=bot.db
```

7. Deploy.

## Админ-команды

- `/panel` — открыть админ-панель.
- `/grant user_id days` — выдать Premium.
- `/revoke user_id` — снять Premium.
- `/ban user_id` — забанить.
- `/unban user_id` — разбанить.
- `/setlimit key value` — изменить лимит.
- `/setad текст` — изменить рекламу.
- `/ad_on` — включить рекламу.
- `/ad_off` — выключить рекламу.

## Основные ключи лимитов

- `free_daily_limit` — скачиваний в день для бесплатных.
- `premium_daily_limit` — скачиваний в день для Premium, `0` = безлимит.
- `free_max_download_mb` — максимум МБ для бесплатных.
- `premium_max_download_mb` — максимум МБ для Premium.
- `free_max_height` — качество free, например `480`.
- `premium_max_height` — качество premium, например `720`.
- `premium_30_stars` — цена Premium на 30 дней в Telegram Stars.
- `ad_enabled` — `1` включить рекламу, `0` выключить.
- `ad_text` — текст рекламы.

## Как работает быстрая загрузка для платных

У всех заявок есть очередь. Premium-заявки получают приоритет `0`, бесплатные — `10`, поэтому Premium проходит раньше бесплатных. Также Premium получает отдельные настройки качества и лимитов.

На обычном бесплатном хостинге нельзя гарантировать реальную высокую скорость, потому что CPU/RAM ограничены. Но приоритетная очередь уже делает платный режим заметно быстрее, когда пользователей много.
