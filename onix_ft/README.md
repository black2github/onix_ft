# OnixFT — передача файлов через Onix

Протокол передачи файлов поверх текстового чата Onix (через Selenium).

## Структура

```
onix_ft/
├── core/
│   ├── protocol.py      # кодек кадров, фабрики, утилиты файла
│   ├── checkpoint.py    # чекпойнты отправителя и получателя
│   ├── sender.py        # конечный автомат отправителя (FileSender)
│   └── receiver.py      # конечный автомат получателя (FileReceiver)
├── transport/
│   ├── base.py          # BaseTransport + StubTransport (для тестов)
│   └── selenium_driver.py  # OnixSeleniumTransport (заполнить селекторы!)
├── scripts/
│   ├── run_sender.py    # точка входа: отправитель
│   ├── run_receiver.py  # точка входа: получатель
│   └── snapshot_dom.py  # утилита снятия DOM для подбора селекторов
└── tests/
    └── test_protocol_roundtrip.py  # тесты без браузера
```

## Быстрый старт

### 1. Установить зависимости

```
pip install selenium
```
Положить `chromedriver.exe` (точно под версию Chrome в контуре) рядом
или указать путь в `transport/selenium_driver.py → CHROMEDRIVER_PATH`.

### 2. Заполнить селекторы

Запустить `snapshot_dom.py`, открыть сохранённый HTML, найти
CSS-селекторы поля ввода и контейнера сообщений в `selenium_driver.py`.

### 3. Запуск

**Получатель** (запускать первым, во внешнем контуре):
```
python -m onix_ft.scripts.run_receiver --out-dir ./received
```

**Отправитель** (внутренний контур):
```
python -m onix_ft.scripts.run_sender path/to/requirements.md
```

### 4. Тесты без браузера

```
python onix_ft/tests/test_protocol_roundtrip.py
```

## Параметры протокола

| Параметр | Значение | Файл |
|---|---|---|
| Размер блока | 2700 байт → ~3637 символов b64 | protocol.py |
| Лимит сообщения | 4000 символов | protocol.py |
| Таймаут ACK | 120 сек | sender.py |
| Повторов при NACK | 5 | sender.py |
| Таймаут ожидания META | 3600 сек | receiver.py |

## Формат кадра

```
##FT|v1|<TYPE>|<file_id>|<seq>|<total>|<payload>|<crc32>##
```
Любая строка без `##FT|v1|` в начале игнорируется — обычный чат работает нормально.
