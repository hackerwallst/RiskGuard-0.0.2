# reports/render_pdf.py — versão Playwright (PDF idêntico ao HTML)
from __future__ import annotations
from pathlib import Path
import img2pdf

def _pdf_vetorial(html_path: Path, pdf_path: Path, wait_ms=2000, usar_css_tela=True) -> bool:
    from playwright.sync_api import sync_playwright
    html_uri = html_path.resolve().as_uri()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="pt-BR", device_scale_factor=1.0)
        page = context.new_page()
        page.goto(html_uri, wait_until="networkidle")
        if wait_ms: page.wait_for_timeout(wait_ms)
        page.emulate_media(media="screen" if usar_css_tela else "print")
        page.pdf(
            path=str(pdf_path),
            print_background=True,
            prefer_css_page_size=True,
            format="A4",
            margin={"top":"10mm","right":"10mm","bottom":"12mm","left":"10mm"},
        )
        context.close(); browser.close()
    return pdf_path.exists() and pdf_path.stat().st_size > 0


def _pdf_screenshot(html_path: Path, pdf_path: Path, wait_ms=2000) -> bool:
    """Modo pixel-perfeito: screenshot full-page -> PDF."""
    from playwright.sync_api import sync_playwright
    html_uri = html_path.resolve().as_uri()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width":1280,"height":720}, device_scale_factor=1.0)
        page = context.new_page()
        page.goto(html_uri, wait_until="networkidle")
        if wait_ms: page.wait_for_timeout(wait_ms)
        png = pdf_path.with_suffix(".tmp.png")
        page.screenshot(path=str(png), full_page=True)
        context.close(); browser.close()
    if not png.exists(): return False
    pdf_path.write_bytes(img2pdf.convert(png.read_bytes()))
    png.unlink(missing_ok=True)
    return pdf_path.exists() and pdf_path.stat().st_size > 0


def html_to_pdf(
    html_path: Path,
    pdf_path: Path,
    mode: str = "browser_pdf",
    wait_ms: int = 2000,
    usar_css_tela: bool = True
) -> bool:
    """
    Converte HTML para PDF idêntico (usando Chromium).
    - mode="browser_pdf": mantém texto selecionável e layout da TELA.
    - mode="raster_pdf": 100% igual visualmente (screenshot).
    """
    html_path = Path(html_path)
    pdf_path = Path(pdf_path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if mode == "browser_pdf":
            return _pdf_vetorial(html_path, pdf_path, wait_ms, usar_css_tela)
        elif mode == "raster_pdf":
            return _pdf_screenshot(html_path, pdf_path, wait_ms)
        else:
            raise ValueError("mode deve ser 'browser_pdf' ou 'raster_pdf'")
    except Exception as e:
        print(f"[render_pdf] erro: {e}")
        return False
