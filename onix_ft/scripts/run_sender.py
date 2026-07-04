"""
Запуск отправителя.

Использование (из корня проекта):
    # Отправить файл (будет сжат автоматически):
    python -m onix_ft.scripts.run_sender path/to/file.md

    # Отправить каталог (будет архивирован и сжат):
    python -m onix_ft.scripts.run_sender path/to/my_docs/

Все файлы и каталоги сжимаются перед передачей (ZIP_DEFLATED).
На стороне получателя файлы восстанавливаются автоматически.
Для несжимаемых форматов (PDF, изображения) накладные расходы
на сжатие незначительны по сравнению со временем передачи.
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


def archive_source(source: Path) -> Path:
    """
    Создать сжатый ZIP-архив файла или каталога во временной директории.

    Для файла:    file.md     → file.md.zip  (при распаковке: file.md)
    Для каталога: my_docs/   → my_docs.zip  (при распаковке: my_docs/)

    Возвращает путь к созданному архиву.
    """
    tmp_dir  = Path(tempfile.mkdtemp())
    zip_name = f"{source.name}.zip" if source.is_file() else f"{source.name}.zip"
    zip_path = tmp_dir / zip_name

    logger.info(
        "Сжатие %s %s → %s...",
        "файла" if source.is_file() else "каталога",
        source.name,
        zip_path.name,
    )

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        if source.is_file():
            # Одиночный файл — кладём в корень архива под своим именем
            zf.write(source, source.name)
            original_size = source.stat().st_size
        else:
            # Каталог — сохраняем структуру с именем каталога как корнем
            # Пример: my_docs/file.md → в архиве: my_docs/file.md
            for file_path in source.rglob('*'):
                if file_path.is_file():
                    arcname = source.name / file_path.relative_to(source)
                    zf.write(file_path, arcname)
            original_size = sum(
                f.stat().st_size for f in source.rglob('*') if f.is_file()
            )

    compressed_size = zip_path.stat().st_size
    ratio = round((1 - compressed_size / max(original_size, 1)) * 100, 1)

    logger.info(
        "Архив создан: %s (%d байт → %d байт, сжатие %.1f%%)",
        zip_path.name, original_size, compressed_size, ratio
    )
    return zip_path


def main():
    parser = argparse.ArgumentParser(
        description="OnixFT — отправитель файла или каталога"
    )
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
    tmp_archive = None

    # Архивируем и сжимаем всё — и файлы и каталоги
    tmp_archive  = archive_source(source_path)
    send_path    = tmp_archive
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
            success = sender.send_file(send_path)

    finally:
        # Удаляем временный архив в любом случае (успех или ошибка)
        if tmp_archive and tmp_archive.exists():
            tmp_archive.unlink()
            try:
                tmp_archive.parent.rmdir()
            except OSError:
                pass
            logger.debug("Временный архив удалён: %s", tmp_archive)

    if success:
        logger.info("Готово. Передача завершена успешно.")
        sys.exit(0)
    else:
        logger.error("Передача завершилась с ошибкой.")
        sys.exit(1)


if __name__ == "__main__":
    main()