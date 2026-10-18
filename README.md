# Aachen Termin Bot

[Städteregion Aachen Ausländeramt](https://termine.staedteregion-aachen.de/auslaenderamt/) termin sitesindeki **tüm kategorilerin** en yakın boş termin tarihlerini takip eder. Bir kategoride daha erken bir termin açıldığında bildirim gönderir (terminal + macOS bildirimi + isteğe bağlı Telegram).

Bot sadece okuma yapar, **termin rezerve etmez** — termini siteden kendin almalısın.

## Kurulum

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Kullanım

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
  [345] Freizügigkeit/ Daueraufenthaltsbescheinigung   21.10.2026 (Çar) 09:30  (270 boş saat)
  [346] FH Studenten                                   25.11.2026 (Çar) 08:30  (128 boş saat)
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

## Nasıl çalışır

Site TEVIS randevu sistemini kullanıyor. Bot her kategori için tarayıcının izlediği adımları taklit eder:

1. `select2?md=1` — kategori listesi (Anliegen) okunur, yeni kategoriler otomatik eklenir.
2. `location?...&cnc-<ID>=1` — kategori seçilir, standort formu okunur.
3. Standort POST edilir → `suggest` sayfası; buradaki her `suggestion_form` boş bir saat demektir (dolu saatler `disabled` buton olarak görünür).

Sunucuyu yormamak için kategoriler arasında 1 saniye beklenir ve kontrol aralığı en az 60 saniyedir.
