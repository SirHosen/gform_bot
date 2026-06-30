import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ==========================================
# KONFIGURASI JAWABAN OTOMATIS
# ==========================================
JAWABAN_KUISIONER = {
    "nama": "Budi Santoso",
    "email": "budi@example.com",
    "nim": "123456789",
    "alasan": "Karena saya tertarik dengan topik ini dan ingin belajar lebih lanjut.",
    "hobi": "Membaca", 
    "kendaraan": "Motor" 
}
# ==========================================

def setup_driver():
    options = webdriver.ChromeOptions()
    # options.add_argument('--headless') # Uncomment ini jika Anda TIDAK ingin melihat browsernya jalan (berjalan di background)
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    return driver

def get_answer_for_question(question_text):
    question_lower = question_text.lower()
    for keyword, answer in JAWABAN_KUISIONER.items():
        if keyword in question_lower:
            return answer
    return "Jawaban default otomatis" 

def analyze_and_fill_form(driver, url, iteration_number):
    print(f"\n[=========== MENGISI FORM KE-{iteration_number} ===========]")
    driver.get(url)
    
    try:
        # Tunggu elemen pertanyaan muncul
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'div[role="listitem"]'))
        )
        time.sleep(2) # Jeda agar form selesai rendering
        
        questions = driver.find_elements(By.CSS_SELECTOR, 'div[role="listitem"]')
        print(f"[*] Menemukan {len(questions)} pertanyaan. Memulai pengisian...")

        for idx, q_container in enumerate(questions):
            try:
                title_element = q_container.find_element(By.CSS_SELECTOR, 'div[role="heading"]')
                q_text = title_element.text
            except:
                q_text = q_container.text.split('\n')[0] if q_container.text else f"Pertanyaan {idx+1}"
            
            answer = get_answer_for_question(q_text)

            # Teks Pendek
            text_inputs = q_container.find_elements(By.CSS_SELECTOR, 'input[type="text"]')
            if text_inputs:
                try:
                    text_inputs[0].send_keys(answer)
                except:
                    pass
                continue 
                
            # Paragraf
            textareas = q_container.find_elements(By.CSS_SELECTOR, 'textarea')
            if textareas:
                try:
                    textareas[0].send_keys(answer)
                except:
                    pass
                continue

            # Pilihan Ganda
            radios = q_container.find_elements(By.CSS_SELECTOR, 'div[role="radio"]')
            if radios:
                clicked = False
                for radio in radios:
                    opsi_teks = radio.get_attribute("data-value") or radio.text
                    if answer.lower() in str(opsi_teks).lower():
                        driver.execute_script("arguments[0].click();", radio)
                        clicked = True
                        break
                if not clicked:
                    driver.execute_script("arguments[0].click();", radios[0])
                continue

            # Checkbox
            checkboxes = q_container.find_elements(By.CSS_SELECTOR, 'div[role="checkbox"]')
            if checkboxes:
                clicked = False
                for checkbox in checkboxes:
                    opsi_teks = checkbox.get_attribute("data-value") or checkbox.text
                    if answer.lower() in str(opsi_teks).lower():
                        driver.execute_script("arguments[0].click();", checkbox)
                        clicked = True
                        break
                if not clicked:
                    driver.execute_script("arguments[0].click();", checkboxes[0])
                continue

        # Proses Submit
        submit_buttons = driver.find_elements(By.XPATH, '//div[@role="button"]//span[contains(text(), "Kirim") or contains(text(), "Submit")]')
        if submit_buttons:
            print("[*] Mengklik tombol submit...")
            driver.execute_script("arguments[0].click();", submit_buttons[0])
            
            # Tunggu halaman konfirmasi "Tanggapan Anda telah direkam" muncul sebelum mengulang
            time.sleep(3) 
            print(f"[SUCCESS] Form ke-{iteration_number} berhasil dikirim!")
        else:
            print("[!] Tombol submit tidak ditemukan.")

    except Exception as e:
        print(f"[ERROR] Terjadi kesalahan pada form ke-{iteration_number}: {e}")

if __name__ == "__main__":
    print("=== Bot Google Form Cerdas & Looping ===")
    form_link = input("Masukkan URL Google Form: ").strip()
    
    if form_link:
        try:
            jumlah_loop = int(input("Ingin diisi berapa kali? (misal: 10): ").strip())
        except ValueError:
            print("Jumlah harus berupa angka. Bot akan berjalan 1 kali.")
            jumlah_loop = 1
            
        print("Membuka browser...")
        driver = setup_driver()
        
        for i in range(1, jumlah_loop + 1):
            analyze_and_fill_form(driver, form_link, i)
            # Memberikan jeda antar submit agar tidak dicurigai sebagai spam/DDoS
            if i < jumlah_loop:
                print(f"Menunggu 3 detik sebelum form ke-{i+1}...\n")
                time.sleep(3) 
                
        print("\nSelesai! Semua form telah diproses.")
        input("Tekan Enter untuk menutup browser...")
        driver.quit()
    else:
        print("URL tidak boleh kosong.")
