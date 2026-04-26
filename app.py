"""AI Watermark Cleaner — Gradio web UI.

ChatGPT modu: C2PA / EXIF / XMP metadata strip.
Gemini modu : SynthID V3 spektral bypass (upstream: aloshdenny/reverse-SynthID).
"""
from __future__ import annotations

import os
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


def _has(keys, *needles) -> bool:
    return any(any(n in k.lower() for n in needles) for k in keys)


def strip_metadata(input_path: str):
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
        f"### Mode: ChatGPT — Metadata Strip\n"
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
        f"### Mode: Gemini — SynthID V3 Bypass\n"
        f"- **Strength:** `{strength}`\n"
        f"- **PSNR:** {result.psnr:.2f} dB\n"
        f"- **SSIM:** {result.ssim:.4f}\n"
        f"- **Profil:** {result.details['profile_resolution']} "
        f"(exact match: {result.details['exact_match']})\n"
        f"- **Pass schedule:** {', '.join(result.stages_applied)}\n"
        f"- **Sonuç:** {success_badge}"
    )
    return str(out_path), info


def process(input_path, source: str, strength: str):
    if input_path is None:
        return None, "Önce bir resim yükle."
    if source.startswith("ChatGPT"):
        return strip_metadata(input_path)
    return run_gemini_bypass(input_path, strength)


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
        '<span class="badge green">Gemini: hazır</span>'
        if GEMINI_AVAILABLE
        else '<span class="badge amber">Gemini: kurulmamış (bash setup.sh)</span>'
    )

    with gr.Blocks(title="AI Watermark Cleaner") as demo:
        gr.HTML(
            f"""
            <div id="hero">
                <h1>AI Watermark Cleaner</h1>
                <p>ChatGPT görsellerinden C2PA / EXIF / XMP metadata'yı sıyır,
                Gemini görsellerinden SynthID watermark'ını spektral bypass ile düşür.</p>
                <div style="margin-top: 12px;">
                    <span class="badge green">ChatGPT: hazır</span>
                    {gemini_badge}
                    <span class="badge">Gradio · Pillow · NumPy</span>
                </div>
            </div>
            """
        )

        with gr.Row():
            with gr.Column(scale=1):
                inp = gr.Image(type="filepath", label="Girdi resmi", height=360)
                source = gr.Radio(
                    choices=[
                        "ChatGPT (metadata strip)",
                        "Gemini (SynthID V3 bypass)",
                    ],
                    value="ChatGPT (metadata strip)",
                    label="Kaynak modeli",
                )
                strength = gr.Radio(
                    choices=["gentle", "moderate", "aggressive", "maximum"],
                    value="aggressive",
                    label="Strength (sadece Gemini modunda etkili)",
                )
                btn = gr.Button("Temizle", variant="primary", size="lg")
                gr.Markdown(
                    "_ChatGPT/DALL·E için **metadata strip** yeterlidir — "
                    "OpenAI SynthID kullanmaz._"
                )

            with gr.Column(scale=1):
                out = gr.Image(
                    type="filepath",
                    label="Temizlenmiş çıktı (sağ üstten indir)",
                    height=360,
                )
                info = gr.Markdown(
                    value="*Sonuç burada görünecek.*",
                    label="Detaylar",
                )

        btn.click(process, inputs=[inp, source, strength], outputs=[out, info])

        gr.Markdown(
            "<div style='text-align:center; opacity:0.6; font-size:12px; "
            "margin-top:18px;'>"
            "Gemini bypass kodu: "
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
