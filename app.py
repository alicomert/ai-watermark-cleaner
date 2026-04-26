"""AI Watermark Cleaner — Gradio web UI.

- Image / ChatGPT modu : C2PA / EXIF / XMP metadata strip
- Image / Gemini modu  : SynthID V3 spektral bypass (upstream: aloshdenny/reverse-SynthID)
- Video tab            : ffmpeg ile container metadata strip (Higgsfield, Runway, Pika, Sora vs.)
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import gradio as gr
from PIL import Image, PngImagePlugin

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


# ────────────────────────────────────────────────────────────────────────
# Image handlers
# ────────────────────────────────────────────────────────────────────────

def _has(keys, *needles) -> bool:
    return any(any(n in k.lower() for n in needles) for k in keys)


def strip_image_metadata(input_path: str):
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


def process_image(input_path, source: str, strength: str):
    if input_path is None:
        return None, "Önce bir resim yükle."
    if source.startswith("ChatGPT"):
        return strip_image_metadata(input_path)
    return run_gemini_bypass(input_path, strength)


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


def strip_video_metadata(input_path: str):
    if input_path is None:
        return None, "Önce bir video yükle."
    if not FFMPEG_AVAILABLE:
        return None, "ffmpeg sistemde bulunamadı. `apt install ffmpeg` ile kur."

    src = Path(input_path)
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

    info = (
        f"### Video — Metadata Strip\n"
        f"- **Süre:** {duration:.1f} s · **Girdi:** {src_size_mb:.1f} MB · "
        f"**Çıktı:** {out_size_mb:.1f} MB\n"
        f"- **Container tag'leri:** {fmt_tags_str}\n"
        f"- **Stream tag'leri:** {stream_tags_str}\n"
        f"- **İşlem:** stream copy (yeniden encode yok), tüm metadata + chapter sıyrıldı.\n"
        f"- **Çıktı dosyası:** `{out_path.name}`"
    )
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
                    inputs=[img_in, img_source, img_strength],
                    outputs=[img_out, img_info],
                )

            # ─────────── Video tab ───────────
            with gr.Tab("Video"):
                with gr.Row():
                    with gr.Column(scale=1):
                        vid_in = gr.Video(label="Girdi videosu", height=360)
                        vid_btn = gr.Button("Metadata strip", variant="primary", size="lg")
                        gr.Markdown(
                            "_Higgsfield, Runway, Pika, Sora vs. çıktıları için. "
                            "Yeniden encode yok — stream copy, kalite kaybı sıfır._"
                        )
                    with gr.Column(scale=1):
                        vid_out = gr.Video(label="Temizlenmiş video", height=360)
                        vid_info = gr.Markdown(value="*Sonuç burada görünecek.*")
                vid_btn.click(
                    strip_video_metadata,
                    inputs=[vid_in],
                    outputs=[vid_out, vid_info],
                )

        gr.Markdown(
            "<div style='text-align:center; opacity:0.6; font-size:12px; "
            "margin-top:18px;'>"
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
