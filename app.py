import streamlit as st
import sqlite3
import pandas as pd
import re
import time
import shutil
import urllib3
from apify_client import ApifyClient
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 1. VERİTABANI ALTYAPISI ---
conn = sqlite3.connect('b2b_automation.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS Settings (api_name TEXT PRIMARY KEY, api_key TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS Leads 
             (id INTEGER PRIMARY KEY AUTOINCREMENT, keyword TEXT, company_name TEXT, website TEXT, email TEXT, form_url TEXT, ai_message TEXT, status TEXT)''')
conn.commit()

def get_setting(api_name):
    c.execute("SELECT api_key FROM Settings WHERE api_name=?", (api_name,))
    row = c.fetchone()
    return row[0] if row else ""

def save_setting(api_name, api_key):
    c.execute("REPLACE INTO Settings (api_name, api_key) VALUES (?, ?)", (api_name, api_key))
    conn.commit()

# --- 2. APIFY: SADECE LİNK VE TEMEL BİLGİ TOPLAYICI ---
def fetch_companies_from_maps(keyword, apify_key):
    client = ApifyClient(apify_key)
    run_input = {
        "searchStringsArray": [keyword],
        "maxCrawledPlacesPerSearch": 2,  
        "language": "tr",
    }
    try:
        run = client.actor("compass/google-maps-extractor").call(run_input=run_input)
        results = []
        for item in client.dataset(run.default_dataset_id).iterate_items():
            results.append({
                "name": item.get("title"),
                "website": item.get("website"),
                "maps_phone": item.get("phoneUnformatted") or item.get("phone") 
            })
        return results
    except Exception as e:
        st.error(f"Apify Harita Hatası: {e}")
        return []

# --- 3. KENDİ TOOL'UMUZ: JS DESTEKLİ DOM OKUYUCU (SELENIUM) ---
def get_dom_content(url):
    """Sitenin JS ile oluşturulmuş SON HALİNİ (DOM) okuyan kendi aracımız"""
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920x1080")
    chrome_options.page_load_strategy = 'eager' # Tüm resimlerin yüklenmesini beklemez, DOM'u alır
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    driver = None
    try:
        chromedriver_path = shutil.which("chromedriver")
        service = Service(chromedriver_path)
        driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.set_page_load_timeout(15)
        driver.get(url)
        time.sleep(3) # JS'nin çalışıp verileri ekrana basması için bekle
        html = driver.page_source
        return html
    except Exception:
        return ""
    finally:
        if driver:
            driver.quit() # RAM'i boşaltmak için hayati önem taşır

def scrape_website_details(url):
    """DOM üzerinden e-posta, telefon ve iletişim sayfalarını süzer"""
    if not url: return None, None
    if not url.startswith("http"): url = "http://" + url
    
    def extract_from_html(html_code):
        soup = BeautifulSoup(html_code, 'html.parser')
        text = soup.get_text(separator=' ')
        
        for a in soup.find_all('a', href=True):
            if a['href'].lower().startswith('mailto:'):
                return a['href'].replace('mailto:', '').split('?')[0].strip(), None
                
        emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,4}', html_code)
        valid_emails = [e for e in emails if not e.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.js', '.css'))]
        
        phones = re.findall(r'(?:0|\+90|90)?\s*\(?[1-9]\d{2}\)?\s*\d{3}\s*\d{2}\s*\d{2}', text)
        
        return (valid_emails[0] if valid_emails else None), (phones[0] if phones else None)

    # Kendi DOM aracımızla ana sayfayı çek
    main_html = get_dom_content(url)
    if not main_html: return None, None
    
    email, phone = extract_from_html(main_html)
    
    soup = BeautifulSoup(main_html, 'html.parser')
    form_url = None
    contact_pages = []
    
    # İletişim sayfalarını bul
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        href_lower = href.lower()
        if any(word in href_lower for word in ['iletisim', 'contact', 'bize-ulasin']):
            full_url = urljoin(url, href)
            contact_pages.append(full_url)
            if not form_url: form_url = full_url

    # Eğer ana sayfada mail yoksa, bulduğumuz iletişim sayfasına da DOM aracıyla gir
    if not email and contact_pages:
        contact_html = get_dom_content(contact_pages[0])
        if contact_html:
            sub_email, sub_phone = extract_from_html(contact_html)
            if sub_email: email = sub_email
            if sub_phone and not phone: phone = sub_phone

    final_contact = ""
    if email: final_contact += f"{email}"
    if phone: final_contact += f" | Site Tel: {phone}" if email else f"Site Tel: {phone}"
    
    return (final_contact if final_contact else None), form_url

# --- 4. YAPAY ZEKA VE MAİL GÖNDERİMİ ---
def generate_personalized_email(company_name, website, groq_key):
    import requests
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {groq_key}",
        "Content-Type": "application/json"
    }
    prompt = f"""
    Sen profesyonel bir B2B pazarlama uzmanı ve yazılım danışmanısın. Aşağıdaki bilgilere sahip şirkete, bizimle iş birliği yapmaları için TAMAMEN KİŞİSELLEŞTİRİLMİŞ, çok ikna edici ve samimi bir soğuk satış (cold email) metni yaz. 
    
    Şirket Adı: {company_name}
    Web Sitesi: {website}
    
    Kurallar:
    1. Maile KESİNLİKLE "Merhaba, ben Bünyamin." diyerek sıcak ve kişisel bir giriş yap.
    2. Genel geçer şablon olmasın. Şirketin ismine atıfta bulun.
    3. Onların sektörünün/konumlarının getirdiği potansiyelin, sunduğumuz teknoloji ve hizmetlerle nasıl örtüştüğünü samimi bir dille anlat.
    4. Sadece mailin gövde metnini yaz.
    """
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content']
        return f"Groq API Hatası! Kod: {response.status_code}"
    except Exception as e:
        return f"Sistem Hatası: {str(e)}"

def send_email_via_brevo(to_email, subject, html_content, brevo_key, sender_email):
    import requests
    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "accept": "application/json",
        "api-key": brevo_key,
        "content-type": "application/json"
    }
    payload = {
        "sender": {"name": "İş Geliştirme Departmanı", "email": sender_email},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": f"<html><body>{html_content.replace(chr(10), '<br>')}</body></html>"
    }
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        return res.status_code in [200, 201, 202]
    except Exception:
        return False

# --- 5. ARAYÜZ ---
st.set_page_config(page_title="B2B Pazarlama Otomasyonu", layout="wide")
st.title("🤖 Gelişmiş B2B Pazarlama ve Lead Otomasyon Sistemi")
st.write("JS DOM Rendering Destekli Kapsamlı Kazıma ve AI Satış Aracı")

with st.sidebar:
    st.header("⚙️ Sistem Entegrasyonları")
    apify_input = st.text_input("Apify API Token", value=get_setting("Apify"), type="password")
    groq_input = st.text_input("Groq AI API Key", value=get_setting("Groq"), type="password")
    brevo_input = st.text_input("Brevo API Key", value=get_setting("Brevo"), type="password")
    sender_input = st.text_input("Onaylı Kurumsal Gönderici Mailiniz", value=get_setting("SenderMail"), placeholder="info@sirketiniz.com")
    test_receiver = st.text_input("Test Alıcı Maili (Örn: Kendi Mailiniz)", value=get_setting("TestReceiver"), placeholder="Kendi mailinizi girin")
    
    if st.button("Sistem Ayarlarını Kaydet"):
        save_setting("Apify", apify_input)
        save_setting("Groq", groq_input)
        save_setting("Brevo", brevo_input)
        save_setting("SenderMail", sender_input)
        save_setting("TestReceiver", test_receiver)
        st.success("Tüm konfigürasyonlar başarıyla veritabanına işlendi!")

tab1, tab2 = st.tabs(["🚀 Canlı Test Çalıştırma", "📊 Veritabanı & Gönderim Raporu"])

with tab1:
    st.subheader("Hedef Anahtar Kelime ile Otomasyonu Tetikle")
    keyword_input = st.text_input("Aranacak Sektör / Kelime", placeholder="Örn: Sirkeci Kuaför")
    
    if st.button("Sistemi Başlat (Pipeline'ı Tetikle)"):
        if not all([apify_input, groq_input, brevo_input, sender_input, test_receiver]):
            st.error("Lütfen önce sol menüdeki tüm API ayarlarını eksiksiz doldurun ve kaydedin!")
        elif not keyword_input:
            st.warning("Lütfen taratmak için bir anahtar kelime girin.")
        else:
            with st.spinner("Sistem çalışıyor... Lütfen adımları takip edin."):
                st.info("1. Adım: Apify ile şirket linkleri toplanıyor...")
                companies = fetch_companies_from_maps(keyword_input, apify_input)
                
                if not companies:
                    st.warning("Hiçbir şirket bulunamadı veya Apify hatası oluştu.")
                else:
                    st.success(f"Haritadan {len(companies)} adet potansiyel şirket başarıyla çekildi.")
                    
                    for comp in companies:
                        st.markdown(f"--- \n**İşlenen Firma:** {comp['name']}")
                        
                        st.info(f"👉 {comp['name']} için kendi DOM aracımız çalıştırılıyor (JS bekleniyor)...")
                        email, form_url = scrape_website_details(comp['website'])
                        
                        final_contact_display = email
                        if not final_contact_display and comp.get('maps_phone'):
                            final_contact_display = f"Maps Tel: {comp['maps_phone']}"
                        
                        st.write(f"Bulunan İletişim Bilgisi: `{final_contact_display}`")
                        st.write(f"Bulunan İletişim Formu: `{form_url}`")
                        
                        st.info(f"🧠 Yapay zeka {comp['name']} için özel metin kurguluyor...")
                        ai_msg = generate_personalized_email(comp['name'], comp['website'], groq_input)
                        st.text_area("Üretilen Kişiselleştirilmiş Metin", value=ai_msg, height=150, key=comp['name'])
                        
                        target_email = test_receiver if not email or "Tel:" in email else email.split(" | ")[0]
                        st.info(f"📧 Mail Brevo üzerinden {target_email} adresine yönlendiriliyor...")
                        
                        is_sent = send_email_via_brevo(
                            to_email=target_email,
                            subject=f"{comp['name']} ile İş Birliği Fırsatı",
                            html_content=ai_msg,
                            brevo_key=brevo_input,
                            sender_email=sender_input
                        )
                        
                        status = "Gönderildi" if is_sent else "Brevo Gönderim Hatası"
                        if ("Tel:" in str(email) or not email) and is_sent:
                            status = "Gönderildi (Şirket maili bulunamadığından Test Mailine İletildi)"
                        
                        if is_sent:
                            st.success("✔ Mail başarıyla iletildi!")
                        else:
                            st.error("❌ Mail iletiminde bir sorun oluştu.")
                        
                        c.execute("""INSERT INTO Leads (keyword, company_name, website, email, form_url, ai_message, status) 
                                     VALUES (?, ?, ?, ?, ?, ?, ?)""", 
                                  (keyword_input, comp['name'], comp['website'], final_contact_display, form_url, ai_msg, status))
                        conn.commit()
                        
            st.success("🎉 Seçilen kelime için tüm akış başarıyla tamamlandı!")

with tab2:
    st.subheader("Sistem Veritabanı Güncel Durumu")
    if st.button("Rapor Tablosunu Yenile"):
        st.cache_data.clear()
    
    df = pd.read_sql_query("SELECT * FROM Leads ORDER BY id DESC", conn)
    if not df.empty:
        st.dataframe(df, use_container_width=True)
    else:
        st.info("Sistemde henüz kayıtlı veri akışı bulunmuyor.")
