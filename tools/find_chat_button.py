# tools/find_chat_button.py
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from onix_ft.transport.selenium_driver import OnixSeleniumTransport

with OnixSeleniumTransport() as t:
    t.wait_ready()
    input("Убедитесь что список чатов виден (чат 'Сохраненные сообщения' в списке), нажмите Enter...")

    # Сохраняем DOM
    t.snapshot_dom_for_selectors("onix_chat_list.html")

    # Дополнительно — выводим все элементы содержащие нужный текст
    from selenium.webdriver.common.by import By

    els = t._driver.find_elements(By.XPATH, "//*[contains(text(),'Сохранен')]")
    print(f"\nНайдено элементов с текстом 'Сохранен': {len(els)}")
    for el in els:
        print(f"  tag={el.tag_name}, class={el.get_attribute('class')!r}, text={el.text!r}")
        # Выводим родителей до глубины 3
        parent = el
        for i in range(3):
            try:
                parent = t._driver.execute_script("return arguments[0].parentElement;", parent)
                print(f"    parent[{i + 1}]: tag={parent.tag_name}, class={parent.get_attribute('class')!r}")
            except:
                break