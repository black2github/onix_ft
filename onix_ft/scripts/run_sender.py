"""
Запуск отправителя.

Использование (из корня проекта):
    python -m onix_ft.scripts.run_sender path/to/file.md

При первом запуске:
  1. Откроется браузер с Onix.
  2. Если профиль браузера не сохранён — залогиньтесь вручную.
  3. Перейдите в нужный чат (или он откроется сразу по URL в selenium_driver.py).
  4. Нажмите Enter в консоли скрипта — скрипт начнёт передачу.
"""

import argparse
import logging
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path (для запуска без pip install)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from onix_ft.transport.selenium_driver import OnixSeleniumTransport
from onix_ft.core.sender import FileSender

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt = "%H:%M:%S",
)
logger = logging.getLogger("run_sender")


def main():
    parser = argparse.ArgumentParser(description="OnixFT — отправитель файла")
    parser.add_argument("file", help="Путь к файлу для передачи")
    parser.add_argument(
        "--ckpt-dir",
        default=".",
        help="Директория для чекпойнтов (по умолчанию: текущая)",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Не ждать нажатия Enter перед началом (для автоматических сценариев)",
    )
    args = parser.parse_args()

    file_path = Path(args.file)
    if not file_path.exists():
        logger.error("Файл не найден: %s", file_path)
        sys.exit(1)

    ckpt_dir = Path(args.ckpt_dir)

    with OnixSeleniumTransport() as transport:
        transport.wait_ready()

        if not args.no_wait:
            input("\n[→] Браузер открыт. Убедитесь, что нужный чат активен, затем нажмите Enter для старта...\n")

        sender = FileSender(transport, ckpt_dir=ckpt_dir)
        success = sender.send_file(file_path)

    if success:
        logger.info("Готово. Файл успешно передан.")
        sys.exit(0)
    else:
        logger.error("Передача завершилась с ошибкой.")
        sys.exit(1)


if __name__ == "__main__":
    main()
