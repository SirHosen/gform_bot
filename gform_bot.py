"""
Google Form Test Harness
========================
Alat bantu untuk MENGUJI Google Form milik sendiri secara otomatis.
Gunakan hanya pada form yang Anda miliki / kelola. Jangan dipakai untuk
memanipulasi survei orang lain atau memalsukan data penelitian.

Fitur:
- Konfigurasi jawaban via config.json (terpisah dari kode)
- Deteksi tipe field: teks pendek, paragraf, radio, checkbox, dropdown,
  linear scale, tanggal, dan waktu
- Verifikasi submit (mendeteksi halaman konfirmasi) + screenshot
- Logging rapi, retry, dan ringkasan hasil
- Mode headless opsional & delay yang dapat diatur

Pemakaian:
    python gform_bot.py --config config.json
    python gform_bot.py --url "https://docs.google.com/forms/..." --count 5
    python gform_bot.py --config config.json --headless --min-delay 2 --max-delay 5
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gform_bot")


# --------------------------------------------------------------------------- #
# Konfigurasi
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    url: str = ""
    count: int = 1
    headless: bool = False
    min_delay: float = 2.0
    max_delay: float = 4.0
    page_timeout: int = 15
    answers: dict[str, str] = field(default_factory=dict)
    default_answer: str = "Jawaban uji otomatis"
    screenshot_dir: str = "screenshots"
    test_marker: str = ""  # mis. "[TEST]" untuk menandai data uji; kosongkan jika tidak perlu

    @classmethod
    def load(cls, path: str | None) -> "Config":
        data: dict[str, Any] = {}
        if path:
            p = Path(path)
            if not p.exists():
                log.error("File config tidak ditemukan: %s", path)
                sys.exit(1)
            data = json.loads(p.read_text(encoding="utf-8"))
        cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        return cfg


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def build_driver(cfg: Config) -> webdriver.Chrome:
    options = Options()
    if cfg.headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,1600")
    # Selenium 4.6+ punya Selenium Manager bawaan: tidak perlu webdriver-manager.
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(60)
    return driver


# --------------------------------------------------------------------------- #
# Logika jawaban
# --------------------------------------------------------------------------- #
def answer_for(question_text: str, cfg: Config) -> str:
    q = question_text.lower()
    for keyword, value in cfg.answers.items():
        if keyword.lower() in q:
            return f"{cfg.test_marker} {value}".strip() if cfg.test_marker else value
    base = cfg.default_answer
    return f"{cfg.test_marker} {base}".strip() if cfg.test_marker else base


# --------------------------------------------------------------------------- #
# Pengisian field
# --------------------------------------------------------------------------- #
def _js_click(driver, element) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    driver.execute_script("arguments[0].click();", element)


def _find(container, css: str) -> list:
    try:
        return container.find_elements(By.CSS_SELECTOR, css)
    except (NoSuchElementException, StaleElementReferenceException):
        return []


def fill_question(driver, container, answer: str) -> str:
    """Mengisi satu blok pertanyaan. Mengembalikan tipe field yang terdeteksi."""
    # Teks pendek
    text_inputs = _find(container, 'input[type="text"]')
    if text_inputs:
        text_inputs[0].clear()
        text_inputs[0].send_keys(answer)
        return "text"

    # Paragraf
    textareas = _find(container, "textarea")
    if textareas:
        textareas[0].clear()
        textareas[0].send_keys(answer)
        return "paragraph"

    # Tanggal / waktu
    date_inputs = _find(container, 'input[type="date"], input[type="datetime"]')
    if date_inputs:
        date_inputs[0].send_keys(answer)
        return "date"

    # Radio (pilihan ganda)
    radios = _find(container, 'div[role="radio"]')
    if radios:
        for r in radios:
            opt = (r.get_attribute("data-value") or r.text or "").lower()
            if answer.lower() in opt and opt:
                _js_click(driver, r)
                return "radio"
        _js_click(driver, random.choice(radios))
        return "radio(random)"

    # Checkbox
    checkboxes = _find(container, 'div[role="checkbox"]')
    if checkboxes:
        matched = False
        for c in checkboxes:
            opt = (c.get_attribute("data-value") or c.text or "").lower()
            if answer.lower() in opt and opt:
                _js_click(driver, c)
                matched = True
        if not matched:
            _js_click(driver, random.choice(checkboxes))
        return "checkbox"

    # Dropdown
    dropdowns = _find(container, 'div[role="listbox"]')
    if dropdowns:
        _js_click(driver, dropdowns[0])
        time.sleep(0.5)
        options = _find(container, 'div[role="option"]')
        # opsi pertama biasanya placeholder "Pilih"
        real_options = [o for o in options if (o.get_attribute("data-value") or "").strip()]
        if real_options:
            chosen = next(
                (o for o in real_options
                 if answer.lower() in (o.get_attribute("data-value") or "").lower()),
                random.choice(real_options),
            )
            _js_click(driver, chosen)
            return "dropdown"

    return "unknown/skipped"


# --------------------------------------------------------------------------- #
# Submit + verifikasi
# --------------------------------------------------------------------------- #
def click_submit(driver) -> bool:
    buttons = driver.find_elements(
        By.XPATH,
        '//div[@role="button"][.//span[contains(text(),"Kirim") or contains(text(),"Submit")]]',
    )
    if not buttons:
        return False
    _js_click(driver, buttons[0])
    return True


def wait_for_confirmation(driver, timeout: int) -> bool:
    """Mendeteksi halaman 'Tanggapan Anda telah direkam'."""
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: "formResponse" in d.current_url
            or d.find_elements(
                By.XPATH,
                '//*[contains(text(),"telah direkam") or contains(text(),"has been recorded")]',
            ),
        )
        return True
    except TimeoutException:
        return False


# --------------------------------------------------------------------------- #
# Satu iterasi
# --------------------------------------------------------------------------- #
def run_once(driver, cfg: Config, n: int) -> bool:
    log.info("=== Pengisian #%d ===", n)
    driver.get(cfg.url)

    try:
        WebDriverWait(driver, cfg.page_timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'div[role="listitem"]'))
        )
    except TimeoutException:
        log.error("#%d: Pertanyaan tidak muncul (timeout). Cek URL / koneksi.", n)
        return False

    questions = driver.find_elements(By.CSS_SELECTOR, 'div[role="listitem"]')
    log.info("#%d: %d pertanyaan ditemukan.", n, len(questions))

    for idx, q in enumerate(questions, start=1):
        try:
            heading = q.find_elements(By.CSS_SELECTOR, 'div[role="heading"]')
            q_text = heading[0].text if heading else (q.text.split("\n")[0] if q.text else f"Q{idx}")
            ans = answer_for(q_text, cfg)
            ftype = fill_question(driver, q, ans)
            log.info("  Q%d [%s] %.50s", idx, ftype, q_text)
        except (StaleElementReferenceException, ElementClickInterceptedException) as e:
            log.warning("  Q%d gagal diisi: %s", idx, e.__class__.__name__)

    if not click_submit(driver):
        log.error("#%d: Tombol submit tidak ditemukan (mungkin ada field wajib kosong).", n)
        return False

    ok = wait_for_confirmation(driver, cfg.page_timeout)

    # Screenshot
    shot_dir = Path(cfg.screenshot_dir)
    shot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = shot_dir / f"submit_{n:03d}_{stamp}_{'ok' if ok else 'fail'}.png"
    try:
        driver.save_screenshot(str(path))
    except WebDriverException:
        pass

    if ok:
        log.info("#%d: BERHASIL terkirim. Screenshot: %s", n, path)
    else:
        log.error("#%d: Konfirmasi tidak terdeteksi. Cek screenshot: %s", n, path)
    return ok


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Google Form test harness (form milik sendiri).")
    p.add_argument("--config", help="Path ke config.json")
    p.add_argument("--url", help="URL Google Form (menimpa config)")
    p.add_argument("--count", type=int, help="Jumlah pengisian (menimpa config)")
    p.add_argument("--headless", action="store_true", help="Jalankan tanpa jendela browser")
    p.add_argument("--min-delay", type=float, help="Delay minimum antar submit (detik)")
    p.add_argument("--max-delay", type=float, help="Delay maksimum antar submit (detik)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = Config.load(args.config)

    # Override dari CLI
    if args.url:
        cfg.url = args.url
    if args.count is not None:
        cfg.count = args.count
    if args.headless:
        cfg.headless = True
    if args.min_delay is not None:
        cfg.min_delay = args.min_delay
    if args.max_delay is not None:
        cfg.max_delay = args.max_delay

    if not cfg.url:
        cfg.url = input("Masukkan URL Google Form Anda: ").strip()
    if not cfg.url:
        log.error("URL kosong. Keluar.")
        sys.exit(1)

    log.info("Target: %s", cfg.url)
    log.info("Jumlah pengisian: %d | headless=%s", cfg.count, cfg.headless)

    driver = build_driver(cfg)
    success = 0
    try:
        for i in range(1, cfg.count + 1):
            if run_once(driver, cfg, i):
                success += 1
            if i < cfg.count:
                delay = random.uniform(cfg.min_delay, cfg.max_delay)
                log.info("Tunggu %.1f detik...", delay)
                time.sleep(delay)
    finally:
        log.info("Selesai: %d/%d berhasil.", success, cfg.count)
        driver.quit()


if __name__ == "__main__":
    main()
