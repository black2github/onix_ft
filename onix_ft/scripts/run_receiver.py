"""
Запуск получателя.

Использование:
    python -m onix_ft.scripts.run_receiver --out-dir .\received
    python -m onix_ft.scripts.run_receiver --out-dir .\received --clear-history

Флаг --clear-history очищает историю чата перед началом приёма.
Рекомендуется использовать всегда, чтобы получатель не читал старые кадры.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from onix_ft.transport.selenium_driver import OnixSeleniumTransport
from onix_ft.core.receiver import FileReceiver

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
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
        help="Директория для чекпойнтов (по умолчанию: текущая)",
    )
    parser.add_argument(
        "--clear-history",
        action="store_true",
        help="Очистить историю чата перед началом приёма (рекомендуется)",
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
            input("\n[→] Браузер открыт. Убедитесь, что нужный чат активен, "
                  "затем нажмите Enter...\n")

        if args.clear_history:
            logger.info("Очистка истории чата перед приёмом...")
            ok = transport.clear_chat_history()
            if not ok:
                logger.error(
                    "Не удалось очистить историю чата. "
                    "Продолжить без очистки? (y/n)"
                )
                if input().strip().lower() != 'y':
                    sys.exit(1)

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
