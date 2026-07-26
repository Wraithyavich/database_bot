# ТУРБОНАЙЗЕР

Telegram-бот ищет артикулы по Turbo P/N, OEM, JRONE, FLP и другим
номерам из компактной поисковой базы `turbo_search.sqlite`. Запрос можно отправить текстом или
фотографией шильдика.

## Установка

```powershell
py -m pip install -r requirements.txt
```

RapidOCR работает локально через ONNX. Для сервера без доступа к интернету
модели нужно скачать во время сборки:

```powershell
rapidocr download_models
```

Для серверного развёртывания рекомендуется использовать `Dockerfile`: он
устанавливает системные библиотеки OpenCV и проверяет инициализацию RapidOCR
ещё во время сборки контейнера.

```powershell
docker build -t database-bot .
docker run --rm -e API_TOKEN="токен Telegram-бота" database-bot
```

Для постоянного запуска используйте `compose.yaml` и файл `.env`:

```env
API_TOKEN=токен Telegram-бота
```

```powershell
docker compose up -d --build
```

Контейнер ограничен по CPU, памяти, количеству процессов и размеру временного
каталога. В самом боте одновременно обрабатывается только одна фотография:
не более трёх фото в минуту от пользователя и не более двенадцати фото в минуту
суммарно. Принимаются изображения до 8 МБ и 16 мегапикселей. Текстовый поиск
также защищён отдельными пользовательским и общим лимитами.

## Поиск по VIN

Бот распознаёт 17-значный VIN как отдельный тип запроса. Проверенные результаты
хранятся в `vin_verified.json` и при запуске импортируются в постоянный
SQLite-кэш `/data/vin_cache.sqlite`.

Если VIN уже проверен, бот показывает автомобиль, позиции турбокомпрессоров,
OEM-номера, Turbo P/N, артикулы картриджей из основной базы и ссылки на
источники.

Неизвестный VIN сначала бесплатно декодируется через NHTSA vPIC, затем бот
выполняет генеративный поиск в интернете. Основной провайдер — Yandex Search API
с Alice AI, резервный — Gemini с Google Search grounding. Возможные OEM/Turbo P/N
сопоставляются точным поиском с основной SQLite-базой, а ответ сохраняется в кэш
и очередь ручной проверки. Такой результат всегда помечается как
предварительный: перед заказом номер нужно сверить с шильдиком турбины или
официальным каталогом. При таком запросе VIN передаётся NHTSA и настроенному
поисковому провайдеру.

Для основного онлайн-поиска создайте API-ключ в
[Yandex AI Studio](https://aistudio.yandex.ru/) и добавьте в `.env`:

```dotenv
YANDEX_API_KEY=replace-with-your-key
YANDEX_FOLDER_ID=replace-with-your-folder-id
YANDEX_SEARCH_TYPE=SEARCH_TYPE_RU
```

Сервисному аккаунту нужна роль `search-api.webSearch.user`, а ключу — область
действия `yc.search-api.execute`. `YANDEX_FOLDER_ID` можно скопировать, нажав на
название каталога в верхней части интерфейса AI Studio. Допустимые типы поиска:
`SEARCH_TYPE_RU`, `SEARCH_TYPE_COM`, `SEARCH_TYPE_KK`, `SEARCH_TYPE_BE` и
`SEARCH_TYPE_UZ`.

Gemini можно оставить резервным провайдером:

```dotenv
GEMINI_API_KEY=replace-with-your-key
GEMINI_MODEL=gemini-3.6-flash
```

Для новых проектов Google Search grounding требует Paid Tier. После подключения
billing рекомендуется задать небольшой project-level
[monthly spend cap](https://ai.google.dev/gemini-api/docs/billing/#spend-caps)
в AI Studio.

Если Yandex полностью настроен, он вызывается первым. При его временной ошибке
бот пробует настроенный Gemini. Без полностью настроенного провайдера проверенные
VIN и очередь продолжают работать, но интернет-поиск отключён. Одновременно
выполняется не более одного онлайн-поиска; действуют лимиты 2 запроса на
пользователя за 10 минут, 5 запросов суммарно за 10 минут и 20 запросов в сутки.
Повторный VIN берётся из SQLite-кэша без нового платного обращения.

Очередь и статистику можно посмотреть внутри контейнера:

```powershell
docker exec database-bot python vin_admin.py stats
docker exec database-bot python vin_admin.py pending
```

Новые подтверждённые соответствия добавляются в `vin_verified.json`, проходят
проверку и доставляются обычным обновлением контейнера. Docker volume `vin-data`
сохраняет очередь VIN при пересборке и замене контейнера.

## Запуск

```powershell
$env:API_TOKEN="токен Telegram-бота"
py script.py
```

По умолчанию используется `turbo_search.sqlite` рядом со скриптом. Другой
путь можно передать через `DATABASE_PATH`:

```powershell
$env:DATABASE_PATH="D:\path\to\turbo_parts.sqlite"
py script.py
```

Если хостинг клонирует репозиторий без Git LFS, бот автоматически распознает
LFS-указатель и загрузит настоящую SQLite-базу из GitHub в системный временный
каталог. Адрес и каталог кеша можно переопределить переменными
`DATABASE_DOWNLOAD_URL` и `DATABASE_CACHE_DIR`.

`turbo_search.sqlite` содержит все данные, используемые поиском, и хранится в
обычном Git. Полная `turbo_parts.sqlite` остаётся исходной базой в Git LFS.
Пересобрать компактную базу можно командой:

```powershell
py build_search_database.py
```

Для поиска по изображению отправьте фото или файл изображения. Лучше всего
работают крупные резкие снимки без бликов, где номер расположен горизонтально.

## Проверка

```powershell
py -m unittest discover -s tests -v
```
