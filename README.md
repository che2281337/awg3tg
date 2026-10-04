# awg3tg — Telegram-бот для автовыдачи ключей AmneziaVPN (AmneziaWG 3.x)

Бот выдаёт пользователям Telegram ключи для **AmneziaVPN** на вашем собственном сервере
с протоколом **AmneziaWG 3.0 / 3.1** (и совместим со старыми AWG 2.0 и legacy 1.x).

Пользователь получает сразу три варианта ключа:

* строку `vpn://…` — вставляется в AmneziaVPN через «Добавить сервер → Вставить ключ»;
* файл `.conf` — для импорта файлом в AmneziaVPN / AmneziaWG;
* QR-код.

## Почему старые боты не работают

В AmneziaWG 3.x (AmneziaVPN 5.0.x, июнь–август 2026) у протокола появились новые параметры,
которые **обязаны совпадать у клиента и сервера**:

| Параметр | Что это |
|---|---|
| `HeaderProtectionKey` | шифрование заголовков пакетов |
| `ContentPaddingAddition` | случайное дополнение пакетов |
| `RekeyAfterTime`, `RekeyTimeout`, `RejectAfterTime`, `KeepaliveTimeout`, `MaxHandshakeAttempts` | рандомизированные тайминги |
| `RandomTrailers`, `DisableCookies` | переключатели |
| `PersistentKeepalive = 25-35` | теперь задаётся диапазоном |

Кроме того, начиная с AWG 2.0 сервер живёт в контейнере `amnezia-awg2` с конфигом `awg0.conf`
(раньше — `amnezia-awg` / `wg0.conf`), `H1–H4` стали диапазонами, появились `S3`, `S4`, `I1–I5`,
а в ключе `vpn://` — поле `format_version`. Старые боты не знают этих полей, поэтому выдают
конфиг, который не проходит рукопожатие.

Этот бот **не хардкодит параметры**: при каждой выдаче он читает `[Interface]` из
серверного `awg0.conf` и переносит в ключ все параметры обфускации (включая `I1–I5`, которые
приложение Amnezia хранит в серверном конфиге закомментированными строками `# I1 = …`).
Формат `vpn://` повторяет `ExportController` из исходников amnezia-client.

## Как это работает

```
Telegram ──► бот ──► docker exec amnezia-awg2 ──► /opt/amnezia/awg/awg0.conf
                                               └► awg syncconf awg0  (без разрыва соединений)
                                               └► /opt/amnezia/awg/clientsTable
```

* Ключи X25519 генерируются в боте, IP выбирается первый свободный в подсети сервера.
* Новый пир сразу применяется через `awg syncconf` — перезапуск не нужен.
* Клиент добавляется в `clientsTable`, поэтому он **виден в приложении AmneziaVPN**
  (Сервер → Протоколы → AmneziaWG → Пользователи) с именем `TG @username #1`.
  Отозвать ключ можно и из бота, и из приложения.
* Если вы поменяете параметры протокола в приложении, кнопка «📤 Показать» выдаст ключ
  уже с новыми параметрами.

## Установка

### 1. Поднимите сервер AmneziaWG

Установите на VPS протокол **AmneziaWG** через приложение AmneziaVPN
(«Добавить сервер» → «Свой сервер», IP + логин + пароль SSH). Приложение создаст контейнер
`amnezia-awg2`. Проверить:

```bash
docker ps --format '{{.Names}}' | grep amnezia-awg
```

### 2. Создайте бота

Получите токен у [@BotFather](https://t.me/BotFather) и узнайте свой Telegram ID
(например, у [@userinfobot](https://t.me/userinfobot)).

### 3. Запустите бота на том же сервере (Docker)

```bash
git clone https://github.com/che2281337/awg3tg.git
cd awg3tg
cp .env.example .env
nano .env            # BOT_TOKEN, ADMIN_IDS, при желании SERVER_HOST
docker compose up -d --build
docker compose logs -f
```

В логах должно появиться `AWG сервер готов: порт …`.

### Вариант без Docker

```bash
apt install -y python3-venv
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env
python -m bot
```

Пример юнита systemd (`/etc/systemd/system/awg-bot.service`):

```ini
[Unit]
Description=AmneziaWG Telegram bot
After=docker.service
Requires=docker.service

[Service]
WorkingDirectory=/opt/awg3tg
ExecStart=/opt/awg3tg/.venv/bin/python -m bot
Restart=always

[Install]
WantedBy=multi-user.target
```

Бот можно запустить и **на другой машине**: docker CLI умеет ходить на удалённый сервер по SSH —
задайте переменную окружения `DOCKER_HOST=ssh://root@IP_СЕРВЕРА` (нужен вход по ключу) и
`SERVER_HOST`.

## Настройки (`.env`)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `BOT_TOKEN` | — | токен бота |
| `ADMIN_IDS` | — | ID админов через запятую |
| `SERVER_HOST` | авто | публичный IP/домен для `Endpoint` |
| `SERVER_NAME` | `AmneziaWG` | имя сервера в приложении |
| `ACCESS_MODE` | `approval` | `open` / `approval` / `closed` |
| `MAX_KEYS_PER_USER` | `1` | лимит ключей на пользователя |
| `DNS1`, `DNS2` | `1.1.1.1`, `1.0.0.1` | DNS клиентов (AmneziaDNS: `172.29.172.254`) |
| `CLIENT_MTU` | — | MTU в `.conf` |
| `AWG_CONTAINER` | авто | `amnezia-awg2` или `amnezia-awg` |
| `AWG_CONFIG_PATH`, `AWG_INTERFACE`, `AWG_BIN` | авто | для нестандартных установок |
| `DB_PATH` | `data/bot.db` | SQLite-база |

## Команды

Пользователь: кнопки «🔑 Получить ключ», «📋 Мои ключи» (статистика, повторная отправка,
удаление), «❓ Как подключиться»; команды `/start`, `/key`, `/mykeys`, `/help`.

Администратор (`/admin`):

| Команда | Действие |
|---|---|
| `/server` | контейнер, версия протокола, порт, параметры обфускации, онлайн |
| `/users` | список пользователей |
| `/approve ID` | выдать доступ (в режиме `closed` можно заранее) |
| `/ban ID` | заблокировать и отозвать все ключи |
| `/limit ID N` / `/limit ID default` | индивидуальный лимит ключей |
| `/keys` | все ключи: последнее подключение и трафик |
| `/revoke KEY_ID` | отозвать ключ |
| `/issue имя` | создать ключ без привязки к пользователю |

В режиме `approval` админам приходит заявка с кнопками «Одобрить / Отклонить».

## Клиенты

* **AmneziaVPN 5.0.1.5+** — нужен для AWG 3.1 (self-hosted). Для серверов AWG 2.0 подойдут и
  более старые версии 4.8.x.
* Файл `.conf` можно импортировать и в **AmneziaWG**, если его версия поддерживает параметры AWG 3.x.

## Безопасность

* Доступ к `/var/run/docker.sock` фактически равен root на сервере — не давайте доступ к контейнеру
  бота посторонним.
* Приватные ключи клиентов хранятся в `data/bot.db` (нужны для повторной отправки ключа) — так же,
  как их хранит само приложение Amnezia. Делайте резервную копию и ограничьте доступ к каталогу.

## Разработка

```bash
pip install -r requirements-dev.txt
pytest
```

Тесты включают сквозной прогон выдачи/отзыва ключа на эмуляции контейнера `amnezia-awg2`
(`tests/fakebin/docker`).
