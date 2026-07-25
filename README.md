# ТУРБОНАЙЗЕР

Telegram-бот ищет артикулы по Turbo P/N, OEM, JRONE, FLP и другим
номерам из `turbo_parts.sqlite`. Запрос можно отправить текстом или
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

## Запуск

```powershell
$env:API_TOKEN="токен Telegram-бота"
py script.py
```

По умолчанию используется `turbo_parts.sqlite` рядом со скриптом. Другой
путь можно передать через `DATABASE_PATH`:

```powershell
$env:DATABASE_PATH="D:\path\to\turbo_parts.sqlite"
py script.py
```

Для поиска по изображению отправьте фото или файл изображения. Лучше всего
работают крупные резкие снимки без бликов, где номер расположен горизонтально.

## Проверка

```powershell
py -m unittest discover -s tests -v
```
