"""
Запуск получателя.

Использование (из корня проекта):
    python -m onix_ft.scripts.run_receiver --out-dir ./received

Скрипт ждёт META-кадр от отправителя и принимает файл.
Остановить вручную (Ctrl+C) — можно безопасно, прогресс сохранён в чекпойнте.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from onix_ft.transport.selenium_driver import OnixSeleniumTransport
from onix_ft.core.receiver import FileReceiver

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt = "%H:%M:%S",
)
logger = logging.getLogger("run_receiver")


def main():
    parser = argparse.ArgumentParser(description="OnixFT — получатель файла")
    parser.add_argument(
        "--out-dir",
        default="./received",
        help="Директория для сохранения принятого файла (по умолчанию: ./received)",
    )
    parser.add_argument(
        "--ckpt-dir",
        default=".",
        help="Директория для чекпойнтов",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Не ждать нажатия Enter перед запуском",
    )
    args = parser.parse_args()

    out_dir  = Path(args.out_dir)
    ckpt_dir = Path(args.ckpt_dir)

    with OnixSeleniumTransport() as transport:
        transport.wait_ready()

        if not args.no_wait:
            input("\n[→] Браузер открыт. Убедитесь, что нужный чат активен, затем нажмите Enter...\n")

        receiver = FileReceiver(transport, out_dir=out_dir, ckpt_dir=ckpt_dir)
        result   = receiver.receive_file()

    if result:
        logger.info("Файл сохранён: %s", result)
        sys.exit(0)
    else:
        logger.error("Приём завершился с ошибкой.")
        sys.exit(1)


if __name__ == "__main__":
    main()
