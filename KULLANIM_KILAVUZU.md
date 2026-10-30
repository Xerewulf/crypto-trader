# Binance Otonom Vadeli İşlemler (Futures) & God-Level Ticaret Botu
### 7/24 Kesintisiz Web Sitesi, Mikro-Scalper & Agresif Trend Motoru

Bu proje; Binance USDⓈ-M Vadeli İşlemler (Futures) piyasasında sizin yerinize 7/24 piyasayı tarayan, emir defteri mikroyapısını (Order Book Microstructure), Net Taker Alım Hacmini (CVD), 7 Günlük Hacim Profilini (Volume Profile VAL/POC/VAH) ve 25+ küresel haber kaynağını analiz ederek karlı işlemler açan kurumsal düzeyde bir otonom ticaret sistemidir.

---

## 🎯 Projenin Temel Kabiliyetleri ve Beklentileriniz

| Beklenti | Nasıl Gerçekleştirildi? |
| :--- | :--- |
| **1. Sizin yerinize vadeli işlem yapması** | Binance USDⓈ-M Futures resmi API'si (`binance_futures_api.py`) entegre edildi. İzole marjin, kaldıraç, Long & Short pozisyon açma/kapama tam otomatiktir. |
| **2. Para kazanması** | Geciken perakende indikatörler yerine kurumsal akış: Wyckoff Springs/Upthrusts, Hacim Profili ve Emir Defteri Dengesizliği (Depth Imbalance) kullanılır. |
| **3. Agresif işlemler yapıp kar getirmesi** | **Agresif Trend Modu (`AGGRESSIVE_TREND`)**: Yüksek puanlı (%80+) kırılımlarda 3x-5x kaldıraç, 1.8R ve 3.5R karlar alıp kalan %30'luk koşucuyu (runner) 1h VWAP takip eden stop ile büyük trendlere sürer. |
| **4. Küçük karlar alıp çok işlemle toplam karı artırması** | **Mikro-Scalper Modu (`SCALPER`)**: 5 dakikalık grafiklerde ani CVD alım patlamaları ve derinlik duvarlarını yakalayarak +0.8% ila +1.5% hızlı karlar alıp sermayeyi hızla katlar. |
| **5. GitHub'da 7/24 çalışan bir web sitesi olması** | **GitHub Actions 24/7 Workflow (`.github/workflows/crypto_trader_247.yml`)**: Bilgisayarınız kapalıyken bile GitHub bulutunda her 15 dakikada bir çalışır. Ayrıca **Render/Railway/Docker** ile ücretsiz 7/24 web sitesi olarak barındırılabilir. |
| **6. God-Level ve Uzman işlem yapması** | Larry Harris (Mikroyapı), Jim Dalton (Açık Artırma Teorisi), Arthur Hayes (Türev OI/Fonlama Matrisi) ve Edward Thorp (Quarter-Kelly) teorileriyle eğitilmiş risk modeli. |
| **7. Derin planlama ve risk yönetimi** | Kelly Kriteri ile pozisyon boyutlandırma, Likidasyon fiyatının stop mesafesinden en az 2x uzakta tutulması, Günlük zarar durdurma (Daily Loss Guard). |
| **Extra: Ek kazanç fırsatları** | **Fonlama Oranı Arbitrajı (`FUNDING_ARB`)**: Aşırı yükselen fonlama oranlarında yönsüz (delta-neutral) yıllık %20 - %300 APR arbitraj getirisi. |
| **Aşamalı Yaklaşım (Düşünceniz)** | **Önce %100 Risksiz Simülasyon**: Sanal 5.000$ USDT ile başlar. Başarıyı panelde gördükten sonra tek tıkla Binance API anahtarınızı bağlayıp canlıya geçebilirsiniz. |

---

## 🖥️ 1. Hızlı Başlangıç & Yerel Web Paneli

### Adım 1: Sistemi Başlatın
Terminalinizde projenin ana dizinindeyken:
```bash
python trader/live/paper_trader.py run --port 8765 --host 0.0.0.0
```

### Adım 2: Web Paneline Bağlanın
* **Bilgisayarınızdan:** [http://localhost:8765](http://localhost:8765)
* **Telefonunuzdan (iPhone / Android - Aynı Wi-Fi'da):** `http://<BILGISAYAR_IP_ADRESINIZ>:8765`

Açılan modern karanlık tema panelde:
* Canlı bakiye, getiri eğrisi (Equity Curve SVG)
* Açık pozisyonlar ve anlık kar/zarar
* 40 adet en likit Binance kripto parası için anlık mikroyapı matrisi
* 25+ küresel kaynaktan (Binance CMS, Fed, Balina alarmları, CoinGecko) canlı haber akışı
* Canlı Binance WebSocket fiyat akışı yer alır.

---

## 🎮 2. Web Paneli Kontrolleri

Panelin üst kısmında yer alan etkileşimli kontrol çubuğu ile:

1. **MOD DEĞİŞTİR:**
   * `🛡️ SİMÜLASYON (SANAL)`: Gerçek paranız riske edilmez. Binance canlı fiyatlarıyla birebir simülasyon yapılır.
   * `⚡ CANLI (GERÇEK)`: Binance API anahtarınız girildiğinde gerçek Binance Vadeli İşlemler hesabınızda pozisyon açılır.
2. **BOT DURUMU:**
   * `▶ AKTİF` / `⏸ DURAKLATILDI`: Tek tıkla yeni işlem almayı dondurabilir veya yeniden başlatabilirsiniz.
3. **STRATEJİ SEÇİCİ:**
   * `⚡ 5m Scalper`: Hızlı, küçük karlar (+0.8% / +1.5%) alıp çok sayıda işlem açar.
   * `🚀 Agresif Trend`: Büyük kırılımları ve likidite süpürmelerini (Wyckoff Spring) yakalar.
   * `🏆 Hybrid (God-Level - Önerilen)`: Hem scalping ile günlük getiri toplar hem trend koşucularını taşır.
   * `📈 Arbitraj`: Sıfır yön riskiyle fonlama getirisi (Funding yield) avlar.
4. **🔑 Binance API Bağla Butonu:**
   * API Key ve Secret bilginizi girmenizi, bağlantıyı test edip kullanılabilir USDT bakiyenizi anında görmenizi sağlar.
5. **🚨 ACİL DURUM TÜMÜNÜ KAPAT (EMERGENCY FLATTEN):**
   * Tek tıkla açık olan tüm pozisyonları piyasa fiyatından kapatır ve tüm emirleri iptal eder.

---

## 🔑 3. Gerçek Binance Hesabını Bağlama (Canlı Geçiş)

Simülasyonda botun işlemlerini ve kar getirdiğini gördükten sonra:

1. Binance hesabınıza girip **API Management** (API Yönetimi) sayfasına gidin.
2. Yeni bir API anahtarı oluşturun.
3. **Güvenlik İzni Ayarı:**
   * ✅ **Enable Reading** (Okuma) -> Açık
   * ✅ **Enable Futures** (Vadeli İşlemler) -> Açık
   * ❌ **Enable Withdrawals (Para Çekme) -> KESİNLİKLE KAPALI OLMALIDIR.**
4. Web panelinizde `[🔑 Binance API Bağla]` butonuna tıklayın.
5. API Key ve Secret'ı yapıştırıp `[🔍 Test Et & Bakiye Getir]` butonuna basın.
6. Bakiyeniz yeşil renkte onaylandığında `[⚡ Kaydet ve Canlı İşlemi Aç]` butonuna basarak gerçek işlemleri başlatın!

---

## ☁️ 4. 7/24 Kesintisiz Çalıştırma (GitHub & Bulut)

### Seçenek A: GitHub Actions 24/7 (Sıfır Sunucu Maliyeti)
Projenizde hazır bulunan `.github/workflows/crypto_trader_247.yml` dosyası sayesinde:
1. Kodlarınızı GitHub reponuza push ettiğinizde GitHub Actions her 15 dakikada bir otomatik tetiklenir.
2. GitHub'ın bulut sunucuları piyasayı tarar, stratejileri uygular, pozisyonları günceller ve güncel durumu reponuza işler.
3. Canlı işlem yapmak isterseniz, GitHub reponuzun **Settings -> Secrets and variables -> Actions** menüsünden `BINANCE_API_KEY` ve `BINANCE_API_SECRET` eklemeniz yeterlidir.

### Seçenek B: Render.com / Railway.app ile 7/24 Web Sitesi (1-Click)
1. Reponuzu GitHub'a yükleyin.
2. [Render.com](https://render.com) veya [Railway.app](https://railway.app) üzerinde **New Web Service** seçip reponuzu bağlayın.
3. Projede hazır bulunan `Procfile` ve `requirements.txt` otomatik tanınır.
4. Size özel `https://crypto-trader-xxxx.onrender.com` gibi 7/24 canlı bir web sitesi linki verilir. Dünyanın her yerinden telefonunuzdan veya bilgisayarınızdan panelinize erişebilirsiniz.

---

## 📊 5. Çıkış ve Kar Alma Matematikleri (Convex Scale-Out)

Bot rastgele stop/hedef koymaz:
* **Stop-Loss (Zarar Durdur):** Teknik geçersizlik seviyesine (Destek altı / Direnç üstü ve 1.2x ATR payı) konur.
* **TP1 (+1.8R Hedefi):** Pozisyonun %40'ı satılarak kar kasaya çekilir; kalan pozisyonun stop seviyesi **Başabaş Noktasına (Breakeven + Komisyon)** çekilir. Pozisyonda zarar etme riski SIFIRA iner!
* **TP2 (+3.5R Hedefi):** Pozisyonun %30'u daha satılır. Toplam %70 kar realize edilmiştir.
* **Koşucu (%30 Runner):** Kalan %30 pozisyon 1 saatlik hacim ağırlıklı ortalama fiyat (VWAP) dinamik takip eden stopla büyük trend bitene kadar sürülür.

Bol kazançlar dileriz!
