"""AI Watermark Cleaner — Gradio web UI.

- Image / ChatGPT modu : C2PA / EXIF / XMP metadata strip
- Image / Gemini modu  : SynthID V3 spektral bypass (upstream: aloshdenny/reverse-SynthID)
- Video tab            : ffmpeg ile container metadata strip (Higgsfield, Runway, Pika, Sora vs.)
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import gradio as gr
from PIL import Image, PngImagePlugin

try:
    import gdown  # type: ignore
except ImportError:
    gdown = None
import requests

# Disable Gradio's anonymous usage telemetry.
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

TMP_ROOT = Path(tempfile.gettempdir())
TMP_PREFIXES = ("clean_", "bypass_", "vidclean_", "dl_")
RETENTION_SECONDS = 10 * 60  # 10 dakikadan eski tüm çıktılar silinir


def _sweep_old_outputs() -> None:
    """Remove temp dirs older than RETENTION_SECONDS so cleaned files don't linger."""
    now = time.time()
    for entry in TMP_ROOT.iterdir() if TMP_ROOT.is_dir() else []:
        if not entry.is_dir():
            continue
        if not entry.name.startswith(TMP_PREFIXES):
            continue
        try:
            if now - entry.stat().st_mtime > RETENTION_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass


def _purge_all_outputs() -> None:
    """Wipe every output dir on shutdown."""
    for entry in TMP_ROOT.iterdir() if TMP_ROOT.is_dir() else []:
        if entry.is_dir() and entry.name.startswith(TMP_PREFIXES):
            shutil.rmtree(entry, ignore_errors=True)


atexit.register(_purge_all_outputs)

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor" / "reverse-SynthID"
BYPASS_SRC = VENDOR / "src" / "extraction"
CODEBOOK_PATH = VENDOR / "artifacts" / "spectral_codebook_v3.npz"

GEMINI_AVAILABLE = BYPASS_SRC.is_dir() and CODEBOOK_PATH.is_file()
GEMINI_LOAD_ERROR: str | None = None

codebook = None
bypass = None
if GEMINI_AVAILABLE:
    sys.path.insert(0, str(BYPASS_SRC))
    try:
        from synthid_bypass import SynthIDBypass, SpectralCodebook
        codebook = SpectralCodebook()
        codebook.load(str(CODEBOOK_PATH))
        bypass = SynthIDBypass()
    except Exception as exc:
        GEMINI_AVAILABLE = False
        GEMINI_LOAD_ERROR = f"{type(exc).__name__}: {exc}"

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
FFPROBE_AVAILABLE = shutil.which("ffprobe") is not None

MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # 200 MB


# ────────────────────────────────────────────────────────────────────────
# URL → local file (Drive + generic HTTPS)
# ────────────────────────────────────────────────────────────────────────

def _gdrive_id(url: str) -> str | None:
    parsed = urlparse(url)
    if "drive.google.com" not in (parsed.netloc or ""):
        return None
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", parsed.path)
    if m:
        return m.group(1)
    qs = parse_qs(parsed.query or "")
    if "id" in qs:
        return qs["id"][0]
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", parsed.path)
    if m:
        return m.group(1)
    return None


def fetch_url_to_temp(url: str, dir_prefix: str = "dl_") -> Path:
    """Download a Drive or HTTPS URL into a temp dir; raises on failure / oversize."""
    url = (url or "").strip()
    if not url:
        raise ValueError("URL boş.")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("URL http(s):// ile başlamalı.")

    out_dir = Path(tempfile.mkdtemp(prefix=dir_prefix))

    drive_id = _gdrive_id(url)
    if drive_id:
        if gdown is None:
            raise RuntimeError("gdown kurulu değil — `pip install gdown`.")
        out_path = out_dir / "download"
        gdown.download(id=drive_id, output=str(out_path), quiet=True, fuzzy=True)
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError(
                "Drive dosyası indirilemedi. Link 'linke sahip olan herkes' "
                "modunda paylaşılmış olmalı."
            )
        size = out_path.stat().st_size
        if size > MAX_DOWNLOAD_BYTES:
            out_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Dosya çok büyük: {size / 1e6:.1f} MB > "
                f"{MAX_DOWNLOAD_BYTES / 1e6:.0f} MB sınırı."
            )
        # Try to detect a sensible extension from magic bytes
        ext = _guess_ext(out_path)
        final = out_dir / f"download{ext}"
        if final != out_path:
            out_path.rename(final)
        return final

    # Generic HTTPS download with size guard
    with requests.get(url, stream=True, timeout=30, allow_redirects=True) as r:
        r.raise_for_status()
        cl = int(r.headers.get("Content-Length", "0") or 0)
        if cl and cl > MAX_DOWNLOAD_BYTES:
            raise RuntimeError(
                f"Dosya çok büyük: {cl / 1e6:.1f} MB > "
                f"{MAX_DOWNLOAD_BYTES / 1e6:.0f} MB sınırı."
            )
        # Filename from URL path or fallback
        suffix = Path(urlparse(url).path).suffix or ""
        out_path = out_dir / f"download{suffix or '.bin'}"
        downloaded = 0
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > MAX_DOWNLOAD_BYTES:
                    f.close()
                    out_path.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"İndirme {MAX_DOWNLOAD_BYTES / 1e6:.0f} MB sınırını aştı."
                    )
                f.write(chunk)
        if not suffix:
            ext = _guess_ext(out_path)
            final = out_dir / f"download{ext}"
            out_path.rename(final)
            return final
        return out_path


def _guess_ext(path: Path) -> str:
    """Sniff magic bytes for a few common formats."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return ".bin"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[4:8] == b"ftyp":  # mp4 / mov
        return ".mp4"
    if head[:4] == b"\x1aE\xdf\xa3":  # mkv / webm
        return ".webm"
    return ".bin"


# ────────────────────────────────────────────────────────────────────────
# Image handlers
# ────────────────────────────────────────────────────────────────────────

def _has(keys, *needles) -> bool:
    return any(any(n in k.lower() for n in needles) for k in keys)


def strip_image_metadata(input_path: str):
    _sweep_old_outputs()
    img = Image.open(input_path)
    img.load()

    src_keys = list((img.info or {}).keys())
    has_exif = bool(img.info.get("exif")) or _has(src_keys, "exif")
    has_xmp = _has(src_keys, "xmp")
    has_c2pa = _has(src_keys, "c2pa", "manifest", "provenance")

    out_dir = Path(tempfile.mkdtemp(prefix="clean_"))
    out_path = out_dir / "cleaned.png"

    clean = Image.new(img.mode, img.size)
    clean.putdata(list(img.getdata()))
    clean.save(out_path, format="PNG", pnginfo=PngImagePlugin.PngInfo())

    info = (
        f"### Image — Metadata Strip\n"
        f"- **Çözünürlük:** {img.size[0]} × {img.size[1]}\n"
        f"- **Algılanan metadata:** {', '.join(src_keys) if src_keys else '(yok)'}\n"
        f"- **EXIF:** {'temizlendi ✓' if has_exif else 'yoktu'}\n"
        f"- **XMP:** {'temizlendi ✓' if has_xmp else 'yoktu'}\n"
        f"- **C2PA / provenance:** {'temizlendi ✓' if has_c2pa else 'işaretli chunk yoktu'}\n"
        f"- **Çıktı:** PNG, hiçbir text chunk yok, piksel verisi orijinal."
    )
    return str(out_path), info


def run_gemini_bypass(input_path: str, strength: str):
    _sweep_old_outputs()
    if not GEMINI_AVAILABLE:
        msg = "Gemini modu yüklü değil. `bash setup.sh` ile reverse-SynthID vendor'ını kur."
        if GEMINI_LOAD_ERROR:
            msg += f"\n\nDetay: {GEMINI_LOAD_ERROR}"
        return None, msg

    pil = Image.open(input_path).convert("RGB")
    arr = np.array(pil)
    result = bypass.bypass_v3(arr, codebook, strength=strength, verify=False)

    out_dir = Path(tempfile.mkdtemp(prefix="bypass_"))
    out_path = out_dir / "cleaned.png"
    Image.fromarray(result.cleaned_image).save(
        out_path, format="PNG", pnginfo=PngImagePlugin.PngInfo()
    )

    success_badge = "✓ başarılı" if result.success else "⚠ kısmen"
    info = (
        f"### Image — Gemini SynthID V3 Bypass\n"
        f"- **Strength:** `{strength}`\n"
        f"- **PSNR:** {result.psnr:.2f} dB\n"
        f"- **SSIM:** {result.ssim:.4f}\n"
        f"- **Profil:** {result.details['profile_resolution']} "
        f"(exact: {result.details['exact_match']})\n"
        f"- **Pass schedule:** {', '.join(result.stages_applied)}\n"
        f"- **Sonuç:** {success_badge}"
    )
    return str(out_path), info


def _resolve_input(input_path, url: str | None, prefix: str) -> tuple[str | None, str | None]:
    """Return (path, error). If url given, download; else use uploaded path."""
    url = (url or "").strip()
    if url:
        try:
            p = fetch_url_to_temp(url, dir_prefix=prefix)
            return str(p), None
        except Exception as exc:
            return None, f"İndirme hatası: {exc}"
    if input_path:
        return input_path, None
    return None, "Dosya yükle ya da Drive/URL gir."


def process_image(input_path, url, source: str, strength: str):
    path, err = _resolve_input(input_path, url, prefix="dl_img_")
    if err:
        return None, err
    if source.startswith("ChatGPT"):
        return strip_image_metadata(path)
    return run_gemini_bypass(path, strength)


# ────────────────────────────────────────────────────────────────────────
# Video handler
# ────────────────────────────────────────────────────────────────────────

def _ffprobe_metadata(path: str) -> dict:
    if not FFPROBE_AVAILABLE:
        return {}
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", path]
    try:
        out = subprocess.check_output(cmd, timeout=30).decode("utf-8", "replace")
        return json.loads(out)
    except Exception:
        return {}


VIDEO_SOURCES = [
    "Higgsfield",
    "Runway",
    "Pika",
    "Sora (OpenAI)",
    "Gemini Veo",
    "Diğer / bilinmiyor",
]


def _video_source_warning(source: str) -> str:
    if source.startswith("Gemini Veo"):
        return (
            "> ⚠️ **Gemini Veo içinde SynthID watermark frame-frame piksele gömülüdür.** "
            "ffmpeg sadece container metadata'sını sıyırır — pixel watermark kalır. "
            "Bu tool Veo videolarındaki SynthID'yi kaldıramaz."
        )
    if source.startswith("Sora"):
        return (
            "> ℹ️ **Sora videolarında sağ alt köşede görünür watermark vardır.** "
            "Bu metadata strip onu kaldırmaz; görünür watermark için inpainting gerekir."
        )
    return ""


def strip_video_metadata(input_path: str, url: str, source: str):
    _sweep_old_outputs()
    if not FFMPEG_AVAILABLE:
        return None, "ffmpeg sistemde bulunamadı. `apt install ffmpeg` ile kur."
    path, err = _resolve_input(input_path, url, prefix="dl_vid_")
    if err:
        return None, err

    src = Path(path)
    src_size_mb = src.stat().st_size / (1024 * 1024)

    probe = _ffprobe_metadata(str(src))
    fmt_tags = (probe.get("format", {}) or {}).get("tags", {}) or {}
    stream_tags = []
    for s in probe.get("streams", []) or []:
        t = (s.get("tags") or {})
        if t:
            stream_tags.append({k: v for k, v in t.items() if k.lower()
                                not in ("language", "handler_name")})
    duration = float((probe.get("format", {}) or {}).get("duration", 0) or 0)

    out_dir = Path(tempfile.mkdtemp(prefix="vidclean_"))
    out_path = out_dir / f"cleaned{src.suffix or '.mp4'}"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-c", "copy",
        "-movflags", "+faststart",
        "-fflags", "+bitexact",
        "-flags:v", "+bitexact",
        "-flags:a", "+bitexact",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        return None, f"ffmpeg hata:\n```\n{proc.stderr[-1500:]}\n```"

    out_size_mb = out_path.stat().st_size / (1024 * 1024)

    fmt_tags_str = (
        ", ".join(f"`{k}`" for k in fmt_tags.keys())
        if fmt_tags else "(yok)"
    )
    stream_tags_str = (
        "; ".join(", ".join(f"`{k}`" for k in t.keys()) or "(yok)" for t in stream_tags)
        if stream_tags else "(yok)"
    )

    warning = _video_source_warning(source)
    info = (
        f"### Video — Metadata Strip\n"
        f"- **Kaynak:** {source}\n"
        f"- **Süre:** {duration:.1f} s · **Girdi:** {src_size_mb:.1f} MB · "
        f"**Çıktı:** {out_size_mb:.1f} MB\n"
        f"- **Container tag'leri:** {fmt_tags_str}\n"
        f"- **Stream tag'leri:** {stream_tags_str}\n"
        f"- **İşlem:** stream copy (yeniden encode yok), tüm metadata + chapter sıyrıldı.\n"
        f"- **Çıktı dosyası:** `{out_path.name}`"
    )
    if warning:
        info += "\n\n" + warning
    return str(out_path), info


# ────────────────────────────────────────────────────────────────────────
# UI
# ────────────────────────────────────────────────────────────────────────

CUSTOM_CSS = """
#hero {
    background: linear-gradient(135deg, #0f172a 0%, #1e293b 60%, #312e81 100%);
    border-radius: 18px;
    padding: 28px 32px;
    color: #e2e8f0;
    margin-bottom: 18px;
    border: 1px solid rgba(99, 102, 241, 0.25);
}
#hero h1 {
    margin: 0 0 6px 0;
    font-size: 28px;
    color: #ffffff;
    letter-spacing: -0.02em;
}
#hero p {
    margin: 0;
    color: #cbd5e1;
    font-size: 14px;
    line-height: 1.55;
}
.badge {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
    margin-right: 6px;
    background: rgba(99, 102, 241, 0.18);
    color: #c7d2fe;
    border: 1px solid rgba(165, 180, 252, 0.35);
}
.badge.green { background: rgba(34, 197, 94, 0.18); color: #bbf7d0;
               border-color: rgba(134, 239, 172, 0.35); }
.badge.amber { background: rgba(245, 158, 11, 0.18); color: #fde68a;
               border-color: rgba(252, 211, 77, 0.35); }
footer { display: none !important; }
"""


def build_ui():
    gemini_badge = (
        '<span class="badge green">Image · Gemini: hazır</span>'
        if GEMINI_AVAILABLE
        else '<span class="badge amber">Image · Gemini: kurulmamış (bash setup.sh)</span>'
    )
    video_badge = (
        '<span class="badge green">Video: hazır</span>'
        if FFMPEG_AVAILABLE
        else '<span class="badge amber">Video: ffmpeg yok</span>'
    )

    with gr.Blocks(title="AI Watermark Cleaner") as demo:
        gr.HTML(
            f"""
            <div id="hero">
                <h1>AI Watermark Cleaner</h1>
                <p>ChatGPT &amp; Higgsfield &amp; Runway gibi servislerin görsel/video çıktılarından
                C2PA / EXIF / XMP metadata'yı sıyır. Gemini görselleri için SynthID watermark'ını
                spektral bypass ile düşür.</p>
                <div style="margin-top: 12px;">
                    <span class="badge green">Image · ChatGPT: hazır</span>
                    {gemini_badge}
                    {video_badge}
                    <span class="badge">Drive / URL: hazır</span>
                    <span class="badge">Ephemeral · 10 dk sonra otomatik silinir</span>
                </div>
            </div>
            """
        )

        with gr.Tabs():
            # ─────────── Image tab ───────────
            with gr.Tab("Görsel"):
                with gr.Row():
                    with gr.Column(scale=1):
                        img_in = gr.Image(type="filepath", label="Girdi resmi", height=360)
                        img_url = gr.Textbox(
                            label="…veya Drive / HTTPS linki",
                            placeholder="https://drive.google.com/file/d/...",
                            lines=1,
                        )
                        img_source = gr.Radio(
                            choices=[
                                "ChatGPT (metadata strip)",
                                "Gemini (SynthID V3 bypass)",
                            ],
                            value="ChatGPT (metadata strip)",
                            label="Kaynak modeli",
                        )
                        img_strength = gr.Radio(
                            choices=["gentle", "moderate", "aggressive", "maximum"],
                            value="aggressive",
                            label="Strength (sadece Gemini modunda etkili)",
                        )
                        img_btn = gr.Button("Temizle", variant="primary", size="lg")
                        gr.Markdown(
                            "_OpenAI **SynthID kullanmaz** — ChatGPT/DALL·E "
                            "için metadata strip yeterlidir._"
                        )
                    with gr.Column(scale=1):
                        img_out = gr.Image(
                            type="filepath",
                            label="Temizlenmiş çıktı",
                            height=360,
                        )
                        img_info = gr.Markdown(value="*Sonuç burada görünecek.*")
                img_btn.click(
                    process_image,
                    inputs=[img_in, img_url, img_source, img_strength],
                    outputs=[img_out, img_info],
                )

            # ─────────── Video tab ───────────
            with gr.Tab("Video"):
                with gr.Row():
                    with gr.Column(scale=1):
                        vid_in = gr.Video(label="Girdi videosu", height=360)
                        vid_url = gr.Textbox(
                            label="…veya Drive / HTTPS linki",
                            placeholder="https://drive.google.com/file/d/...",
                            lines=1,
                        )
                        vid_source = gr.Dropdown(
                            choices=VIDEO_SOURCES,
                            value="Higgsfield",
                            label="Video kaynağı",
                            info="Veo seçersen SynthID uyarısı gösterilir.",
                        )
                        vid_btn = gr.Button(
                            "Metadata strip", variant="primary", size="lg"
                        )
                        gr.Markdown(
                            "_Stream copy — yeniden encode yok, kalite kaybı sıfır. "
                            "Yalnızca **container/stream metadata** ve chapter bilgisi sıyrılır._"
                        )
                    with gr.Column(scale=1):
                        vid_out = gr.Video(label="Temizlenmiş video", height=360)
                        vid_info = gr.Markdown(value="*Sonuç burada görünecek.*")
                vid_btn.click(
                    strip_video_metadata,
                    inputs=[vid_in, vid_url, vid_source],
                    outputs=[vid_out, vid_info],
                )

        gr.Markdown(
            "<div style='text-align:center; opacity:0.6; font-size:12px; "
            "margin-top:18px;'>"
            "🔒 <b>Gizlilik:</b> hiçbir dosya kalıcı olarak saklanmaz; "
            "çıktılar 10 dakika sonra sunucudan silinir, telemetri kapalı.<br/>"
            "Gemini bypass çekirdeği: "
            "<a href='https://github.com/aloshdenny/reverse-SynthID' "
            "target='_blank'>aloshdenny/reverse-SynthID</a> · "
            "Bu UI MIT lisansı altında dağıtılır."
            "</div>"
        )

    return demo


if __name__ == "__main__":
    demo = build_ui()
    theme = gr.themes.Soft(
        primary_hue="indigo",
        secondary_hue="slate",
        neutral_hue="slate",
        font=("Inter", "ui-sans-serif", "system-ui"),
    )
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        theme=theme,
        css=CUSTOM_CSS,
    )
