"""
Вспомогательный скрипт: открыть браузер, дать время залогиниться/перейти в чат,
затем сохранить HTML страницы в файл для анализа и подбора CSS-селекторов.

Использование:
    python -m onix_ft.scripts.snapshot_dom --out onix_dom.html
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from onix_ft.transport.selenium_driver import OnixSeleniumTransport

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("snapshot_dom")


def main():
    parser = argparse.ArgumentParser(description="Сохранить DOM Onix для анализа")
    parser.add_argument("--out", default="onix_dom.html", help="Путь для сохранения HTML")
    args = parser.parse_args()

    with OnixSeleniumTransport() as t:
        logger.info("Браузер открыт. Залогиньтесь в Onix и перейдите в нужный чат.")
        input("Нажмите Enter, когда чат открыт и видны сообщения...")

        out = t.snapshot_dom_for_selectors(args.out)
        logger.info("DOM сохранён: %s", out)
        logger.info("Откройте файл в браузере (Ctrl+U → view-source) или DevTools")
        logger.info("и найдите CSS-селекторы для поля ввода и контейнера сообщений.")


if __name__ == "__main__":
    main()
