# AI Watermark Cleaner

Tek bir Gradio web arayüzünde iki iş:

| Mode | Ne yapar | Kim için |
|------|----------|----------|
| **ChatGPT (metadata strip)** | C2PA / EXIF / XMP metadata'yı sıyırır, piksel verisi aynı kalır. | OpenAI / DALL·E / GPT-image çıktıları |
| **Gemini (SynthID V3 bypass)** | Spektral codebook subtraction ile SynthID watermark'ını düşürür. | Google Gemini / Imagen çıktıları |

> **Not:** OpenAI **SynthID kullanmaz** — SynthID, Google DeepMind'ın tescilli teknolojisidir ve sadece Gemini/Imagen/Veo/Lyria çıktılarında bulunur. ChatGPT görselleri için metadata strip genelde yeterlidir.

---

## Hızlı Başlangıç

```bash
git clone https://github.com/alicomert/ai-watermark-cleaner.git
cd ai-watermark-cleaner
bash setup.sh
source venv/bin/activate
python app.py
```

Tarayıcı: `http://<vps-ip>:7860` (lokalse `http://localhost:7860`).

`setup.sh` venv'i kurar, bağımlılıkları yükler ve **Gemini modu** için
[`aloshdenny/reverse-SynthID`](https://github.com/aloshdenny/reverse-SynthID) reposunu `vendor/` altına klonlar (V3 codebook ~5 MB).

---

## Kullanım

1. **Resmi yükle** — soldaki kutuya sürükle-bırak.
2. **Modu seç** — ChatGPT veya Gemini.
3. **Strength** (sadece Gemini için): `gentle` → `maximum`. Logo / düz alan içeren görseller için `gentle` veya `moderate` öneririm.
4. **Temizle**'ye bas. Sağda metrikler (PSNR/SSIM, profil, pass schedule) ve indirilebilir çıktı.

### Gemini modu nasıl çalışır?

Repo, Gemini'nin enkode ettiği taşıyıcı frekansları (carrier frequencies) önceden çıkarılmış spektral codebook ile FFT alanında çıkarır. V3, çoklu çözünürlük desteği için profile-based subtraction kullanır. Detay: [reverse-SynthID README](https://github.com/aloshdenny/reverse-SynthID).

### ChatGPT modu nasıl çalışır?

PIL ile resmi açıp piksel verisini boş bir PNG container'ına yazar — tüm `iTXt`/`tEXt`/`eXIf` chunk'ları (C2PA manifest'leri dahil) düşer. Görsel piksel-piksel aynı kalır.

---

## Sunucuda kalıcı çalıştırma

```bash
nohup python app.py > /tmp/cleaner.log 2>&1 & disown
```

systemd servisi olarak çalıştırmak istersen `app.service` dosyası ekleyebilirsin (PR'a açığım).

---

## Lisans ve Atıf

- Bu UI: **MIT** ([`LICENSE`](LICENSE))
- Gemini bypass çekirdeği: [aloshdenny/reverse-SynthID](https://github.com/aloshdenny/reverse-SynthID) — kendi lisansı altında, vendor/ ile ayrı klonlanır.

## Sorumluluk

Yalnızca **araştırma ve eğitim** amaçlıdır. AI üretimi içeriği insan üretimi gibi sunmak için kullanma.
