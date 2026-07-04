"""
Запуск отправителя.

Использование (из корня проекта):
    # Отправить файл:
    python -m onix_ft.scripts.run_sender path/to/file.md

    # Отправить каталог (автоматически архивируется и сжимается):
    python -m onix_ft.scripts.run_sender path/to/my_docs/

При первом запуске:
  1. Откроется браузер с Onix.
  2. Если профиль браузера не сохранён — залогиньтесь вручную.
  3. Перейдите в нужный чат.
  4. Нажмите Enter в консоли скрипта — скрипт начнёт передачу.
"""

import argparse
import logging
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from onix_ft.transport.selenium_driver import OnixSeleniumTransport
from onix_ft.core.sender import FileSender

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt = "%H:%M:%S",
)
logger = logging.getLogger("run_sender")


def archive_directory(src_dir: Path) -> Path:
    """
    Создать ZIP-архив каталога во временной директории.

    Структура архива: содержимое src_dir помещается в подкаталог
    с именем src_dir, чтобы при распаковке восстановилась исходная структура.
    Например, каталог my_docs/ → архив my_docs.zip → при распаковке: my_docs/

    Возвращает путь к созданному архиву.
    """
    tmp_dir  = Path(tempfile.mkdtemp())
    zip_path = tmp_dir / f"{src_dir.name}.zip"

    logger.info("Архивирование каталога %s → %s...", src_dir, zip_path.name)

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for file_path in src_dir.rglob('*'):
            if file_path.is_file():
                # Сохраняем путь относительно родителя каталога,
                # чтобы при распаковке восстановился сам каталог.
                # Пример: my_docs/file.md → в архиве: my_docs/file.md
                arcname = src_dir.name / file_path.relative_to(src_dir)
                zf.write(file_path, arcname)

    original_size = sum(f.stat().st_size for f in src_dir.rglob('*') if f.is_file())
    compressed_size = zip_path.stat().st_size
    ratio = round((1 - compressed_size / max(original_size, 1)) * 100, 1)

    logger.info(
        "Архив создан: %s (%d байт → %d байт, сжатие %.1f%%)",
        zip_path.name, original_size, compressed_size, ratio
    )
    return zip_path


def main():
    parser = argparse.ArgumentParser(description="OnixFT — отправитель файла или каталога")
    parser.add_argument(
        "path",
        help="Путь к файлу или каталогу для передачи"
    )
    parser.add_argument(
        "--ckpt-dir",
        default=".",
        help="Директория для чекпойнтов (по умолчанию: текущая)",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Не ждать нажатия Enter перед началом",
    )
    args = parser.parse_args()

    source_path = Path(args.path)
    if not source_path.exists():
        logger.error("Путь не найден: %s", source_path)
        sys.exit(1)

    ckpt_dir    = Path(args.ckpt_dir)
    tmp_archive = None  # временный архив для удаления после передачи
    auto_extract = False

    # Если передан каталог — архивируем
    if source_path.is_dir():
        logger.info("Передан каталог — создаём ZIP-архив...")
        tmp_archive  = archive_directory(source_path)
        source_path  = tmp_archive
        auto_extract = True

    try:
        with OnixSeleniumTransport() as transport:
            transport.wait_ready()

            if not args.no_wait:
                input(
                    "\n[→] Браузер открыт. Убедитесь, что нужный чат активен, "
                    "затем нажмите Enter для старта...\n"
                )

            sender = FileSender(
                transport,
                ckpt_dir=ckpt_dir,
                auto_extract=auto_extract,
            )
            success = sender.send_file(source_path)

    finally:
        # Удаляем временный архив в любом случае (успех или ошибка)
        if tmp_archive and tmp_archive.exists():
            tmp_archive.unlink()
            tmp_archive.parent.rmdir()
            logger.debug("Временный архив удалён: %s", tmp_archive)

    if success:
        logger.info("Готово. Передача завершена успешно.")
        sys.exit(0)
    else:
        logger.error("Передача завершилась с ошибкой.")
        sys.exit(1)


if __name__ == "__main__":
    main()