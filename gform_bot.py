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
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import random
import re
import sys
import time
import uuid
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
    max_workers: int = 4

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
    
    # Anti-bot: Sembunyikan status otomatisasi
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    # Gunakan User-Agent modern agar tidak diblokir Google
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(60)
    
    # Sembunyikan properti navigator.webdriver melalui CDP
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })
    except Exception as e:
        log.warning("Gagal menginjeksi skrip bypass webdriver: %s", e)
        
    return driver


# --------------------------------------------------------------------------- #
# Logika jawaban
# --------------------------------------------------------------------------- #
def resolve_answer(val: Any, cfg: Config, is_checkbox: bool = False) -> Any:
    """
    Memproses template variabel dan tipe list.
    - Jika list dan is_checkbox=True: mengembalikan list dengan item yang diproses template-nya.
    - Jika list dan is_checkbox=False: memilih satu item acak dan memproses template-nya.
    - Jika string: memproses template-nya.
    """
    def process_string(s: str) -> str:
        if "{{random_number}}" in s:
            s = s.replace("{{random_number}}", str(random.randint(100000, 999999)))
        if "{{random_id}}" in s:
            s = s.replace("{{random_id}}", str(random.randint(10000000, 99999999)))
        if "{{random_name}}" in s:
            first_names = [
                "Budi", "Andi", "Siti", "Rian", "Joko", "Dewi", "Eko", "Sari", "Agus", "Rina",
                "Heri", "Mega", "Adit", "Putri", "Dimas", "Aulia", "Rahmat", "Laras", "Fajar", "Novi",
                "Hendra", "Fitri", "Taufik", "Wulan", "Bambang", "Kartika", "Surya", "Indah", "Roni", "Ayu"
            ]
            last_names = [
                "Santoso", "Wijaya", "Rahma", "Hidayat", "Pratama", "Kusuma", "Lestari", "Saputra", "Wulandari", "Nugroho",
                "Setiawan", "Hayati", "Fitriani", "Gunawan", "Susanti", "Ramadhan", "Purnama", "Sari", "Utami", "Hadi",
                "Subagyo", "Siregar", "Nasution", "Simanjuntak", "Lubis", "Tanjung", "Ginting", "Pohan", "Pasaribu", "Harahap"
            ]
            name = f"{random.choice(first_names)} {random.choice(last_names)}"
            s = s.replace("{{random_name}}", name)
        if "{{uuid}}" in s:
            s = s.replace("{{uuid}}", str(uuid.uuid4())[:8])
        if "{{random_email}}" in s:
            s = s.replace("{{random_email}}", f"user_{str(uuid.uuid4())[:6]}@example.com")
        if "{{timestamp}}" in s:
            s = s.replace("{{timestamp}}", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
        # Tambahkan test_marker jika ada
        if cfg.test_marker and s and not s.startswith(cfg.test_marker):
            s = f"{cfg.test_marker} {s}".strip()
        return s

    if isinstance(val, list):
        if is_checkbox:
            return [process_string(str(item)) for item in val]
        else:
            if not val:
                return ""
            chosen = random.choice(val)
            return process_string(str(chosen))
            
    if isinstance(val, str):
        return process_string(val)
        
    return val


def answer_for(question_text: str, cfg: Config) -> Any:
    q = question_text.lower()
    for keyword, value in cfg.answers.items():
        if keyword.lower() in q:
            return value
    return cfg.default_answer


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


def get_row_label(row_el) -> str:
    try:
        # 1. Cari elemen dengan role="rowheader"
        headers = row_el.find_elements(By.CSS_SELECTOR, '[role="rowheader"]')
        if headers and headers[0].text.strip():
            return headers[0].text.strip()
            
        # 2. Cari span atau div pertama yang berisi teks dan bukan tombol radio/checkbox
        elements = row_el.find_elements(By.XPATH, ".//*[not(@role='radio') and not(@role='checkbox') and (self::span or self::div)]")
        for el in elements:
            t = el.text.strip()
            if t:
                return t
    except Exception:
        pass
        
    # 3. Fallback: ambil baris pertama dari .text
    try:
        total_text = row_el.text.strip()
        if total_text:
            lines = [line.strip() for line in total_text.split("\n") if line.strip()]
            if lines:
                return lines[0]
    except Exception:
        pass
        
    return "Baris Grid"


def fill_question(driver, container, cfg: Config, q_text: str) -> str:
    """Mengisi satu blok pertanyaan. Mengembalikan tipe field yang terdeteksi."""
    
    # 0. Deteksi File Upload (Bypass / Peringatan)
    file_buttons = container.find_elements(By.XPATH, './/div[@role="button"][.//span[contains(text(),"Tambahkan file") or contains(text(),"Add file")]]')
    if file_buttons:
        log.warning("  [!] Field '%s' terdeteksi tipe 'File Upload'. Ini membutuhkan login Google dan sulit diotomatisasi secara headless. Dilewati.", q_text)
        return "file_upload(skipped)"

    # 1. Deteksi Pertanyaan Grid (Multiple Choice Grid atau Checkbox Grid)
    rows = container.find_elements(By.CSS_SELECTOR, 'div[role="row"]')
    option_rows = []
    for r in rows:
        buttons = r.find_elements(By.CSS_SELECTOR, 'div[role="radio"], div[role="checkbox"]')
        if buttons:
            option_rows.append((r, buttons))
            
    if option_rows:
        is_checkbox_grid = any(b.get_attribute("role") == "checkbox" for _, buttons in option_rows for b in buttons)
        grid_type = "checkbox_grid" if is_checkbox_grid else "radio_grid"
        log.info("  -> Mendeteksi tipe '%s' dengan %d baris.", grid_type, len(option_rows))
        
        for row_el, buttons in option_rows:
            row_label = get_row_label(row_el)
            # Gabungkan Nama Pertanyaan + Label Baris untuk pencarian jawaban yang spesifik
            full_query = f"{q_text} - {row_label}"
            raw_ans = answer_for(full_query, cfg)
            if raw_ans == cfg.default_answer:
                # Coba cari dengan label baris saja
                raw_ans = answer_for(row_label, cfg)
            
            # Isi baris ini
            if is_checkbox_grid:
                # Checkbox Grid (multi-select per baris)
                resolved_ans = resolve_answer(raw_ans, cfg, is_checkbox=True)
                answers_list = [str(a).lower() for a in resolved_ans] if isinstance(resolved_ans, list) else [str(resolved_ans).lower()]
                
                matched = False
                for b in buttons:
                    opt_val = (b.get_attribute("data-value") or b.get_attribute("aria-label") or "").lower()
                    is_match = any(ans in opt_val for ans in answers_list if ans)
                    if is_match:
                        if b.get_attribute("aria-checked") != "true":
                            _js_click(driver, b)
                        matched = True
                if not matched:
                    # Pilih acak jika tidak ada yang cocok
                    _js_click(driver, random.choice(buttons))
            else:
                # Radio Grid (single-select per baris)
                resolved_ans = str(resolve_answer(raw_ans, cfg, is_checkbox=False))
                matched_btn = None
                for b in buttons:
                    opt_val = (b.get_attribute("data-value") or b.get_attribute("aria-label") or "").lower()
                    if resolved_ans.lower() in opt_val and opt_val:
                        matched_btn = b
                        break
                if matched_btn:
                    _js_click(driver, matched_btn)
                else:
                    _js_click(driver, random.choice(buttons))
                    
        return grid_type

    # 2. Ambil Jawaban Terlebih Dahulu (non-checkbox untuk sekarang)
    # Kami akan resolve ulang jika itu checkbox nanti
    raw_answer = answer_for(q_text, cfg)

    # 3. Deteksi Tanggal / Waktu dengan Multi-Input (Hari/Bulan/Tahun atau Jam/Menit)
    # Di Google Form, input terpisah biasanya menggunakan type="text" atau type="number"
    text_inputs = _find(container, 'input[type="text"]')
    if len(text_inputs) > 1:
        hour_input, minute_input = None, None
        day_input, month_input, year_input = None, None, None
        
        for inp in text_inputs:
            label = (inp.get_attribute("aria-label") or "").lower()
            if any(w in label for w in ["jam", "hour"]):
                hour_input = inp
            elif any(w in label for w in ["menit", "minute"]):
                minute_input = inp
            elif any(w in label for w in ["hari", "day", "tanggal"]):
                day_input = inp
            elif any(w in label for w in ["bulan", "month"]):
                month_input = inp
            elif any(w in label for w in ["tahun", "year"]):
                year_input = inp
                
        # Deteksi Waktu
        if hour_input and minute_input:
            ans_str = str(resolve_answer(raw_answer, cfg))
            parts = re.split(r'[:.]', ans_str)
            h = parts[0] if len(parts) > 0 else "12"
            m = parts[1] if len(parts) > 1 else "00"
            hour_input.clear()
            hour_input.send_keys(h)
            minute_input.clear()
            minute_input.send_keys(m)
            return "time(components)"
            
        # Deteksi Tanggal
        if day_input or month_input or year_input:
            ans_str = str(resolve_answer(raw_answer, cfg))
            parts = re.split(r'[-/.]', ans_str)
            d, m, y = "01", "01", "2026"
            if len(parts) == 3:
                if len(parts[0]) == 4:  # YYYY-MM-DD
                    y, m, d = parts[0], parts[1], parts[2]
                else:  # DD-MM-YYYY
                    d, m, y = parts[0], parts[1], parts[2]
                    
            if day_input:
                day_input.clear()
                day_input.send_keys(d)
            if month_input:
                month_input.clear()
                month_input.send_keys(m)
            if year_input:
                year_input.clear()
                year_input.send_keys(y)
            return "date(components)"

    # 4. Tanggal / Waktu bawaan HTML5 (input type="date")
    date_inputs = _find(container, 'input[type="date"], input[type="datetime"]')
    if date_inputs:
        ans_str = str(resolve_answer(raw_answer, cfg))
        date_inputs[0].send_keys(ans_str)
        return "date"

    # 5. Radio (Pilihan Ganda - Single Select)
    radios = _find(container, 'div[role="radio"]')
    if radios:
        ans_str = str(resolve_answer(raw_answer, cfg))
        other_radio = None
        matched_radio = None
        
        for r in radios:
            opt = (r.get_attribute("data-value") or r.text or "").lower()
            has_input = len(r.find_elements(By.CSS_SELECTOR, 'input[type="text"]')) > 0
            if has_input or "__other_option__" in (r.get_attribute("data-value") or "") or "lainnya" in opt or "other" in opt:
                other_radio = r
            if ans_str.lower() in opt and opt:
                matched_radio = r
                
        if matched_radio:
            _js_click(driver, matched_radio)
            return "radio"
        elif other_radio:
            # Isi kolom 'Lainnya' jika opsi standar tidak cocok
            _js_click(driver, other_radio)
            try:
                time.sleep(0.2)
                inp = other_radio.find_element(By.CSS_SELECTOR, 'input[type="text"]')
                inp.clear()
                inp.send_keys(ans_str)
            except Exception:
                try:
                    parent = other_radio.find_element(By.XPATH, "..")
                    inp = parent.find_element(By.CSS_SELECTOR, 'input[type="text"]')
                    inp.clear()
                    inp.send_keys(ans_str)
                except Exception:
                    pass
            return "radio(other)"
        else:
            _js_click(driver, random.choice(radios))
            return "radio(random)"

    # 6. Checkbox (Kotak Centang - Multi Select)
    checkboxes = _find(container, 'div[role="checkbox"]')
    if checkboxes:
        resolved_ans = resolve_answer(raw_answer, cfg, is_checkbox=True)
        answers_list = [str(a).lower() for a in resolved_ans] if isinstance(resolved_ans, list) else [str(resolved_ans).lower()]
        
        matched = False
        other_checkbox = None
        
        for c in checkboxes:
            opt = (c.get_attribute("data-value") or c.text or "").lower()
            has_input = len(c.find_elements(By.CSS_SELECTOR, 'input[type="text"]')) > 0
            if has_input or "__other_option__" in (c.get_attribute("data-value") or "") or "lainnya" in opt or "other" in opt:
                other_checkbox = c
                
            is_match = any(ans in opt for ans in answers_list if ans)
            if is_match and opt:
                if c.get_attribute("aria-checked") != "true":
                    _js_click(driver, c)
                matched = True
                
        if not matched and other_checkbox:
            _js_click(driver, other_checkbox)
            # Gunakan jawaban pertama atau gabungan string
            ans_text = ", ".join(answers_list)
            try:
                time.sleep(0.2)
                inp = other_checkbox.find_element(By.CSS_SELECTOR, 'input[type="text"]')
                inp.clear()
                inp.send_keys(ans_text)
            except Exception:
                try:
                    parent = other_checkbox.find_element(By.XPATH, "..")
                    inp = parent.find_element(By.CSS_SELECTOR, 'input[type="text"]')
                    inp.clear()
                    inp.send_keys(ans_text)
                except Exception:
                    pass
            return "checkbox(other)"
        elif not matched:
            _js_click(driver, random.choice(checkboxes))
            return "checkbox(random)"
            
        return "checkbox"

    # 7. Dropdown (Listbox)
    dropdowns = _find(container, 'div[role="listbox"]')
    if dropdowns:
        ans_str = str(resolve_answer(raw_answer, cfg))
        _js_click(driver, dropdowns[0])
        time.sleep(0.5)
        options = _find(container, 'div[role="option"]')
        real_options = [o for o in options if (o.get_attribute("data-value") or "").strip()]
        if real_options:
            chosen = None
            for o in real_options:
                opt_val = (o.get_attribute("data-value") or "").lower()
                opt_text = o.text.lower()
                if ans_str.lower() in opt_val or ans_str.lower() in opt_text:
                    chosen = o
                    break
            if not chosen:
                chosen = random.choice(real_options)
            _js_click(driver, chosen)
            return "dropdown"

    # 8. Text Input standar (Teks Pendek)
    # Kami taruh di bawah agar tidak mendahului deteksi tanggal/waktu komponen
    if text_inputs:
        ans_str = str(resolve_answer(raw_answer, cfg))
        text_inputs[0].clear()
        text_inputs[0].send_keys(ans_str)
        return "text"

    # 9. Paragraf
    textareas = _find(container, "textarea")
    if textareas:
        ans_str = str(resolve_answer(raw_answer, cfg))
        textareas[0].clear()
        textareas[0].send_keys(ans_str)
        return "paragraph"

    return "unknown/skipped"


# Tombol Navigasi & Validasi Error
# --------------------------------------------------------------------------- #
def find_navigation_buttons(driver) -> tuple[Any, Any, Any]:
    """Mencari tombol navigasi: (next_button, submit_button, back_button)"""
    buttons = driver.find_elements(By.XPATH, '//div[@role="button"]')
    next_btn = None
    submit_btn = None
    back_btn = None
    
    for btn in buttons:
        try:
            # Dapatkan text
            text = btn.text.strip().lower()
            if not text:
                # Cari di span anak
                spans = btn.find_elements(By.TAG_NAME, "span")
                if spans:
                    text = " ".join([s.text.strip().lower() for s in spans if s.text.strip()])
            
            # Jika text masih kosong, coba dari aria-label atau data-value
            if not text:
                text = (btn.get_attribute("aria-label") or btn.get_attribute("data-value") or "").lower()
                
            if any(w in text for w in ["berikutnya", "next", "siguiente", "lanjut"]):
                next_btn = btn
            elif any(w in text for w in ["kirim", "submit", "send", "enviar"]):
                submit_btn = btn
            elif any(w in text for w in ["kembali", "back", "atrás", "previous"]):
                back_btn = btn
        except Exception:
            pass
            
    return next_btn, submit_btn, back_btn


def check_validation_errors(driver) -> list[str]:
    """Mencari pesan error validasi yang muncul di layar."""
    errors = []
    try:
        alerts = driver.find_elements(By.XPATH, '//*[@role="alert" or contains(@id, "i.err.") or contains(@class, "RveJ3c")]')
        for alert in alerts:
            if alert.is_displayed():
                t = alert.text.strip()
                if t and len(t) > 2:
                    errors.append(t)
    except Exception:
        pass
    return errors


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
    
    page_num = 1
    max_pages = 25  # Limit agar tidak terjadi infinite loop jika ada bug navigasi
    
    # Siapkan direktori screenshot
    shot_dir = Path(cfg.screenshot_dir)
    shot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    while page_num <= max_pages:
        log.info("#%d (Halaman %d): Menunggu pertanyaan...", n, page_num)
        try:
            # Tunggu salah satu muncul: input pertanyaan atau tombol navigasi
            WebDriverWait(driver, cfg.page_timeout).until(
                lambda d: d.find_elements(By.CSS_SELECTOR, 'div[role="listitem"]') or find_navigation_buttons(d)[1] is not None
            )
        except TimeoutException:
            log.error("#%d (Halaman %d): Halaman tidak termuat sepenuhnya (timeout).", n, page_num)
            path = shot_dir / f"timeout_{n:03d}_page{page_num}_{stamp}.png"
            try:
                driver.save_screenshot(str(path))
            except Exception:
                pass
            return False
            
        questions = driver.find_elements(By.CSS_SELECTOR, 'div[role="listitem"]')
        log.info("#%d (Halaman %d): Mengisi %d pertanyaan.", n, page_num, len(questions))
        
        for idx, q in enumerate(questions, start=1):
            try:
                heading = q.find_elements(By.CSS_SELECTOR, 'div[role="heading"]')
                q_text = heading[0].text if heading else (q.text.split("\n")[0] if q.text else f"Q{idx}")
                
                # Bersihkan tanda bintang (*) dari nama pertanyaan wajib
                q_text_clean = q_text.replace("*", "").strip()
                
                ftype = fill_question(driver, q, cfg, q_text_clean)
                log.info("  Q%d [%s] %.50s", idx, ftype, q_text_clean)
            except (StaleElementReferenceException, ElementClickInterceptedException) as e:
                log.warning("  Q%d gagal diisi: %s", idx, e.__class__.__name__)
            except Exception as e:
                log.warning("  Q%d error tidak terduga: %s", idx, str(e))
                
        # Cari tombol navigasi
        next_btn, submit_btn, _ = find_navigation_buttons(driver)
        
        # Cek jika ada error sebelum melangkah ke halaman berikutnya atau submit
        errors_before = check_validation_errors(driver)
        if errors_before:
            log.error("#%d (Halaman %d): Error validasi terdeteksi sebelum pindah/submit: %s", n, page_num, errors_before)
            path = shot_dir / f"err_before_{n:03d}_page{page_num}_{stamp}.png"
            try:
                driver.save_screenshot(str(path))
            except Exception:
                pass
            return False
            
        if next_btn:
            log.info("#%d (Halaman %d): Mengklik tombol 'Berikutnya'...", n, page_num)
            _js_click(driver, next_btn)
            page_num += 1
            time.sleep(1.5)  # Tunggu transisi halaman
            
            # Cek jika setelah klik 'Berikutnya' muncul error validasi
            errors_after = check_validation_errors(driver)
            if errors_after:
                log.error("#%d (Halaman %d): Error validasi mencegah pindah halaman: %s", n, page_num - 1, errors_after)
                path = shot_dir / f"err_next_{n:03d}_page{page_num-1}_{stamp}.png"
                try:
                    driver.save_screenshot(str(path))
                except Exception:
                    pass
                return False
        elif submit_btn:
            log.info("#%d (Halaman %d): Mengklik tombol 'Kirim'...", n, page_num)
            _js_click(driver, submit_btn)
            time.sleep(1.5)
            
            # Cek jika setelah klik 'Kirim' muncul error validasi
            errors_after = check_validation_errors(driver)
            if errors_after:
                log.error("#%d (Halaman %d): Error validasi mencegah submit: %s", n, page_num, errors_after)
                path = shot_dir / f"err_submit_{n:03d}_{stamp}.png"
                try:
                    driver.save_screenshot(str(path))
                except Exception:
                    pass
                return False
            break
        else:
            log.error("#%d (Halaman %d): Tombol navigasi ('Berikutnya' atau 'Kirim') tidak ditemukan.", n, page_num)
            path = shot_dir / f"err_nobutton_{n:03d}_page{page_num}_{stamp}.png"
            try:
                driver.save_screenshot(str(path))
            except Exception:
                pass
            return False
            
    # Verifikasi Halaman Konfirmasi
    ok = wait_for_confirmation(driver, cfg.page_timeout)
    path = shot_dir / f"submit_{n:03d}_{stamp}_{'ok' if ok else 'fail'}.png"
    try:
        driver.save_screenshot(str(path))
    except WebDriverException:
        pass
        
    if ok:
        log.info("#%d: BERHASIL terkirim. Screenshot: %s", n, path)
    else:
        log.error("#%d: Konfirmasi tidak terdeteksi setelah submit. Cek screenshot: %s", n, path)
    return ok


def run_thread_task(cfg: Config, n: int) -> bool:
    """Wrapper task untuk thread: menangani pembuatan driver, eksekusi, dan pembersihan driver."""
    # Staggered startup: berikan delay acak di awal untuk membagi beban CPU/RAM
    if cfg.max_workers > 1:
        startup_delay = random.uniform(0.5, 3.5)
        log.info("[T-#%d] Penundaan startup selama %.1fs untuk membagi beban...", n, startup_delay)
        time.sleep(startup_delay)

    driver = None
    try:
        driver = build_driver(cfg)
        return run_once(driver, cfg, n)
    except Exception as e:
        log.error("[T-#%d] Gagal dalam eksekusi thread: %s", n, str(e))
        return False
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Google Form test harness (form milik sendiri).")
    p.add_argument("--config", help="Path ke config.json")
    p.add_argument("--url", help="URL Google Form (menimpa config)")
    p.add_argument("--count", type=int, help="Jumlah pengisian (menimpa config)")
    p.add_argument("--workers", type=int, help="Jumlah thread browser paralel (menimpa config)")
    p.add_argument("--headless", action="store_true", help="Jalankan tanpa jendela browser")
    p.add_argument("--min-delay", type=float, help="Delay minimum antar submit (detik)")
    p.add_argument("--max-delay", type=float, help="Delay maksimum antar submit (detik)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    
    # Auto-detect config.json jika tidak dispesifikasikan di CLI
    config_path = args.config
    if not config_path and Path("config.json").exists():
        config_path = "config.json"
        
    cfg = Config.load(config_path)

    # Override dari CLI
    if args.url:
        cfg.url = args.url
    if args.count is not None:
        cfg.count = args.count
    if args.workers is not None:
        cfg.max_workers = args.workers
    if args.headless:
        cfg.headless = True
    if args.min_delay is not None:
        cfg.min_delay = args.min_delay
    if args.max_delay is not None:
        cfg.max_delay = args.max_delay

    if not cfg.url:
        try:
            cfg.url = input("Masukkan URL Google Form Anda: ").strip()
        except (KeyboardInterrupt, EOFError):
            log.error("\nProses dibatalkan oleh pengguna. Keluar.")
            sys.exit(1)
            
    if not cfg.url:
        log.error("URL kosong. Keluar.")
        sys.exit(1)

    # Minta jumlah pengisian jika tidak ada parameter --count di CLI
    if args.count is None:
        try:
            count_input = input(f"Masukkan jumlah pengisian [default: {cfg.count}]: ").strip()
            if count_input:
                cfg.count = int(count_input)
        except ValueError:
            log.warning("Input tidak valid, menggunakan default: %d", cfg.count)
        except (KeyboardInterrupt, EOFError):
            log.error("\nProses dibatalkan oleh pengguna. Keluar.")
            sys.exit(1)

    log.info("Target: %s", cfg.url)
    log.info("Jumlah pengisian: %d | max_workers=%d | headless=%s", cfg.count, cfg.max_workers, cfg.headless)

    success = 0
    if cfg.max_workers > 1 and cfg.count > 1:
        log.info("Menjalankan dalam mode paralel (konkuren) dengan %d workers...", cfg.max_workers)
        actual_workers = min(cfg.max_workers, cfg.count)
        
        with ThreadPoolExecutor(max_workers=actual_workers) as executor:
            futures = {
                executor.submit(run_thread_task, cfg, i): i
                for i in range(1, cfg.count + 1)
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    ok = fut.result()
                    if ok:
                        success += 1
                except Exception as e:
                    log.error("[T-#%d] Thread menghasilkan error: %s", idx, str(e))
        log.info("Selesai: %d/%d berhasil.", success, cfg.count)
    else:
        log.info("Menjalankan dalam mode sekuensial (satu driver)...")
        driver = build_driver(cfg)
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
