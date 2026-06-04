import streamlit as st
import sqlite3
import pandas as pd
import requests
import re
import urllib3
from apify_client import ApifyClient
from bs4 import BeautifulSoup

# Güvenlik (SSL) sertifikası bozuk sitelere girerken uyarı vermesini engelle
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

# --- 2. FONKSİYONEL MOTORLAR (APIFY, SCRAPER, AI, BREVO) ---

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

def scrape_website_details(url):
    if not url: return None, None
    if not url.startswith("http"): url = "http://" + url
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    
    def extract_contact(html):
        soup = BeautifulSoup(html, 'html.parser')
        text_content = soup.get_text(separator=' ')
        
        email = None
        for a in soup.find_all('a', href=True):
            if a['href'].lower().startswith('mailto:'):
                email = a['href'].replace('mailto:', '').split('?')[0].strip()
                break
        
        if not email:
            emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', html)
            valid_emails = [e for e in emails if not e.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.js', '.css', '.woff'))]
            if valid_emails: email = valid_emails[0] 
            
        phone = None
        phones = re.findall(r'(?:0|\+90|90)?\s*\(?\d{3}\)?\s*\d{3}\s*\d{2}\s*\d{2}', text_content)
        if phones: phone = phones[0]

        final_contact = ""
        if email: final_contact += f"{email}"
        if phone: final_contact += f" | Site Tel: {phone}" if email else f"Site Tel: {phone}"
            
        return final_contact if final_contact else None

    try:
        res = requests.get(url, timeout=10, headers=headers, verify=False)
        if res.status_code != 200: return None, None
            
        html = res.text
        soup = BeautifulSoup(html, 'html.parser')
        contact_info = extract_contact(html)
        
        form_url = None
        for a in soup.find_all('a', href=True):
            href = a.get('href', '').lower()
            if any(word in href for word in ['iletisim', 'contact', 'bize-ulasin', 'hakkimizda']):
                form_url = a['href']
                if not form_url.startswith('http'):
                    base_url = url.split('/')[0] + "//" + url.split('/')[2] 
                    form_url = base_url + form_url if form_url.startswith('/') else base_url + '/' + form_url
                break
        
        if (not contact_info or "@" not in contact_info) and form_url:
            try:
                res_contact = requests.get(form_url, timeout=10, headers=headers, verify=False)
                if res_contact.status_code == 200:
                    new_contact = extract_contact(res_contact.text)
                    if new_contact and "@" in new_contact:
                        contact_info = new_contact
            except Exception:
                pass
                
        return contact_info, form_url
    except Exception:
        return None, None

def generate_personalized_email(company_name, website, groq_key):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {groq_key}",
        "Content-Type": "application/json"
    }
    prompt = f"""
    Sen profesyonel bir B2B pazarlama uzmanısın. Aşağıdaki bilgilere sahip şirkete, bizimle iş birliği yapmaları için TAMAMEN KİŞİSELLEŞTİRİLMİŞ, çok ikna edici ve samimi bir soğuk satış (cold email) metni yaz. 
    
    Şirket Adı: {company_name}
    Web Sitesi: {website}
    
    Kurallar:
    1. Genel geçer şablon olmasın. Şirketin ismine atıfta bulun.
    2. Adreslerinin/konumlarının getirdiği potansiyelin sunduğumuz hizmetlerle nasıl örtüştüğünü samimi anlat.
    3. Sadece mailin gövde metnini yaz.
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
        else:
            return f"Groq API Hatası! Kod: {response.status_code}, Detay: {response.text}"
    except Exception as e:
        return f"Sistem Hatası: {str(e)}"

def send_email_via_brevo(to_email, subject, html_content, brevo_key, sender_email):
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

# --- 3. STREAMLIT KULLANICI ARAYÜZÜ ---
st.set_page_config(page_title="B2B Pazarlama Otomasyonu", layout="wide")
st.title("🤖 Gelişmiş B2B Pazarlama ve Lead Otomasyon Sistemi")
st.write("Google Maps entegrasyonlu, Yapay Zeka destekli kişiselleştirilmiş soğuk satış aracı.")

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
                st.info("1. Adım: Google Maps üzerinde şirket araması yapılıyor...")
                companies = fetch_companies_from_maps(keyword_input, apify_input)
                
                if not companies:
                    st.warning("Hiçbir şirket bulunamadı veya Apify hatası oluştu.")
                else:
                    st.success(f"Haritadan {len(companies)} adet potansiyel şirket başarıyla çekildi.")
                    
                    for comp in companies:
                        st.markdown(f"--- \n**İşlenen Firma:** {comp['name']}")
                        
                        st.info(f"👉 {comp['name']} web sitesi detayları taranıyor...")
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
                        
            st.success("🎉 Seçilen kelime için tüm akış başarıyla tamamlandı! Rapor sekmesini inceleyebilirsiniz.")

with tab2:
    st.subheader("Sistem Veritabanı Güncel Durumu")
    if st.button("Rapor Tablosunu Yenile"):
        st.cache_data.clear()
    
    df = pd.read_sql_query("SELECT * FROM Leads ORDER BY id DESC", conn)
    if not df.empty:
        st.dataframe(df, use_container_width=True)
    else:
        st.info("Sistemde henüz kayıtlı veri akışı bulunmuyor.")
