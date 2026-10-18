# Aachen Termin Bot

[Städteregion Aachen Ausländeramt](https://termine.staedteregion-aachen.de/auslaenderamt/) termin sitesindeki **tüm kategorilerin** en yakın boş termin tarihlerini takip eder. Bir kategoride daha erken bir termin açıldığında bildirim gönderir (terminal + macOS bildirimi + isteğe bağlı Telegram).

Bot sadece okuma yapar, **termin rezerve etmez** — termini siteden kendin almalısın.

## Kurulum

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Web arayüzü

```bash
.venv/bin/python web.py
```

Sonra tarayıcıda **http://127.0.0.1:8000** adresini aç. Arayüz termin sitesinin temasındadır ve şunları gösterir:

- **Termin varsa:** en yakın tarih ve saat (kaç gün sonra olduğu, takipte açıldığı görüldüyse ne zaman açıldığı).
- **Termin yoksa:** yeni termin açılma sıklığı — ortalama kaç saatte/günde bir açıldığı, son açılma zamanı, genelde hangi saatte açıldığı, açık kalma süresi. Bu istatistikler uygulamanın **takip başladığından beri gözlediği** değişikliklere dayanır; ilk günlerde "veri toplanıyor" görünür.
- **E-posta alarmı** butonu: kategori için e-posta adresi kaydeder. Kayıtlar `data/termin.db` içinde saklanır. ⚠️ E-posta *gönderimi* henüz bağlı değil (`web.py` içindeki `send_email` şimdilik sadece log yazar); hosting kurulunca SMTP/servis ile bağlanacak.

Arka planda varsayılan olarak 5 dakikada bir tüm kategoriler kontrol edilir (`--interval 600` ile değiştirilebilir, `--port` ile port). "Şimdi kontrol et" butonu en fazla dakikada bir çalışır. Geçmiş `data/termin.db` (SQLite) dosyasında tutulur — silersen istatistikler sıfırlanır.

Testler: `.venv/bin/python -m unittest test_web`

## Komut satırı

Kategorileri ve ID'lerini listele:

```bash
.venv/bin/python termin_bot.py --list
```

Tek seferlik kontrol (tüm kategoriler):

```bash
.venv/bin/python termin_bot.py --once
```

Sürekli takip (varsayılan her 5 dakikada bir):

```bash
.venv/bin/python termin_bot.py
```

Sadece belirli kategoriler, 10 dakikada bir:

```bash
.venv/bin/python termin_bot.py --categories 344,346 --interval 600
```

Örnek çıktı:

```
=== 18.10.2026 06:38:22 — en yakın terminler ===
  [345] Freizügigkeit/ Daueraufenthaltsbescheinigung   21.10.2026 (Çar) 09:30
  [346] FH Studenten                                   25.11.2026 (Çar) 08:30
  [344] RWTH Studenten                                 boş termin yok
```

### Seçenekler

| Seçenek | Açıklama |
|---|---|
| `--list` | Kategorileri ve ID'lerini listeler |
| `--once` | Bir kez kontrol eder ve çıkar |
| `--interval N` | Kontrol aralığı, saniye (varsayılan 300, en az 60) |
| `--categories 344,346` | Sadece bu kategori ID'lerini takip eder |
| `--json` | Sonuçları JSON olarak yazdırır |
| `--test-notify` | Test bildirimi gönderir |

## Bildirimler

Bot her kontrolde en yakın tarihi `state.json` dosyasına kaydeder. Bir sonraki kontrolde bir kategoride **daha erken** bir termin çıkarsa (veya hiç termin yokken termin açılırsa) bildirim gönderir. İlk çalıştırmada bildirim gönderilmez, sadece başlangıç durumu kaydedilir.

- **Terminal:** her zaman.
- **macOS bildirimi:** macOS'ta otomatik. Kapatmak için `NO_MAC_NOTIFY=1`.
- **Telegram (isteğe bağlı):**
  1. Telegram'da [@BotFather](https://t.me/BotFather) ile bot oluştur, token'ı al.
  2. Bota bir mesaj at, sonra `https://api.telegram.org/bot<TOKEN>/getUpdates` adresinden `chat.id` değerini bul.
  3. Ortam değişkenleriyle çalıştır:

     ```bash
     export TELEGRAM_BOT_TOKEN="..."
     export TELEGRAM_CHAT_ID="..."
     .venv/bin/python termin_bot.py --test-notify
     ```

## GitHub Actions ile otomatik çalıştırma

Bilgisayarın kapalıyken de takip için [`.github/workflows/termin-bot.yml`](.github/workflows/termin-bot.yml) botu GitHub sunucularında **her 15 dakikada bir** `python termin_bot.py --once` ile çalıştırır. `state.json` çalıştırmalar arasında GitHub Actions cache'inde saklanır, böylece daha erken termin tespiti çalışmaya devam eder. Bildirimler Telegram üzerinden gelir (macOS bildirimi kapalıdır).

Kurulum:

1. Repo sayfasında **Settings → Secrets and variables → Actions → New repository secret** ile iki secret ekle:
   - `TELEGRAM_BOT_TOKEN` — BotFather'dan aldığın token
   - `TELEGRAM_CHAT_ID` — yukarıda bulduğun `chat.id`

   veya terminalden:

   ```bash
   gh secret set TELEGRAM_BOT_TOKEN
   ```

   ```bash
   gh secret set TELEGRAM_CHAT_ID
   ```

2. **Actions** sekmesinde workflow'ların açık olduğundan emin ol. Zamanlanmış çalıştırma kendiliğinden başlar.
3. Elle çalıştırmak için: Actions → **Termin Bot** → **Run workflow**, veya:

   ```bash
   gh workflow run termin-bot.yml
   ```

Notlar:

- İlk çalıştırma (veya cache silindiğinde/7 gün kullanılmadığında) sadece başlangıç durumunu kaydeder, bildirim göndermez.
- GitHub zamanlanmış çalıştırmaları garanti etmez; yoğun saatlerde birkaç dakika (bazen daha fazla) gecikebilir veya atlanabilir.
- **Dakika kotası:** Özel (private) repolarda ücretsiz hesapta ayda 2000 Actions dakikası var. Bir çalıştırma ~1,5 dakika sürüyor ve 2 dakika olarak sayılıyor; her 15 dakikada bir çalıştırma ayda ~5800 dakika eder, yani kota ayın ortasından önce biter ve bot durur. Çözümler: repoyu public yapmak (public repolarda dakika sınırsız), sıklığı azaltmak (`cron: "0 * * * *"` = saatte bir, ~1450 dk/ay) veya workflow'daki komuta `--categories 344,346` ekleyip sadece ilgilendiğin kategorileri takip etmek (çalıştırma 1 dakikanın altına iner; `cron: "*/30 * * * *"` ile birlikte ~1450 dk/ay).
- Durdurmak için: Actions → Termin Bot → **⋯ → Disable workflow**.

## Nasıl çalışır

Site TEVIS randevu sistemini kullanıyor. Bot her kategori için tarayıcının izlediği adımları taklit eder:

1. `select2?md=1` — kategori listesi (Anliegen) okunur, yeni kategoriler otomatik eklenir.
2. `location?...&cnc-<ID>=1` — kategori seçilir, standort formu okunur.
3. Standort POST edilir → `suggest` sayfası; buradaki her `suggestion_form` boş bir saat demektir (dolu saatler `disabled` buton olarak görünür). Günler tarih sırasıyla listelendiği için sayfa **sadece ilk boş saate kadar** okunur ve bağlantı kesilir — birkaç MB'lık sayfaların tamamı indirilmez.

Sunucuyu yormamak için kategoriler arasında 1 saniye beklenir ve kontrol aralığı en az 60 saniyedir.
