from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import io, os, tempfile, zipfile, shutil, subprocess

import fitz  # PyMuPDF
from pdf2docx import Converter

app = FastAPI(title="MakerPDF Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://aravindhsurya.in", "http://localhost:8081"],
    allow_methods=["POST"],
    allow_headers=["*"],
)


def tmp_path(suffix=""):
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path


@app.get("/")
def health():
    return {"status": "ok", "service": "MakerPDF Backend"}


# ── PDF to Word ────────────────────────────────────────────────────────────────
@app.post("/convert/pdf-to-word")
async def pdf_to_word(file: UploadFile = File(...)):
    data = await file.read()
    pdf_path = tmp_path(".pdf")
    docx_path = tmp_path(".docx")
    try:
        with open(pdf_path, "wb") as f:
            f.write(data)
        cv = Converter(pdf_path)
        cv.convert(docx_path)
        cv.close()
        with open(docx_path, "rb") as f:
            content = f.read()
        name = file.filename.rsplit(".", 1)[0] + ".docx"
        return StreamingResponse(
            io.BytesIO(content),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    finally:
        for p in (pdf_path, docx_path):
            if os.path.exists(p):
                os.remove(p)


# ── Office to PDF (Word / Excel / PowerPoint) via LibreOffice ─────────────────
@app.post("/convert/office-to-pdf")
async def office_to_pdf(file: UploadFile = File(...)):
    data = await file.read()
    workdir = tempfile.mkdtemp()
    try:
        # keep the original extension so LibreOffice picks the right filter
        safe_name = os.path.basename(file.filename) or "input"
        in_path = os.path.join(workdir, safe_name)
        with open(in_path, "wb") as f:
            f.write(data)

        # LibreOffice needs a writable HOME and a dedicated user-profile dir,
        # otherwise it can exit 0 without producing any output.
        profile = os.path.join(workdir, "lo_profile")
        env = dict(os.environ, HOME=workdir)
        try:
            proc = subprocess.run(
                ["soffice",
                 "-env:UserInstallation=file://" + profile.replace(os.sep, "/"),
                 "--headless", "--nologo", "--norestore", "--nofirststartwizard",
                 "--convert-to", "pdf:writer_pdf_Export" if safe_name.lower().endswith((".doc", ".docx")) else "pdf",
                 "--outdir", workdir, in_path],
                timeout=120, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Conversion timed out. Try a smaller file.")

        base = os.path.splitext(safe_name)[0]
        out_path = os.path.join(workdir, base + ".pdf")
        if not os.path.exists(out_path):
            detail = (
                "Conversion produced no output. "
                "exit=%s stdout=%s stderr=%s files=%s"
                % (proc.returncode,
                   proc.stdout.decode("utf-8", "ignore")[:300],
                   proc.stderr.decode("utf-8", "ignore")[:300],
                   os.listdir(workdir))
            )
            raise HTTPException(status_code=500, detail=detail)
        with open(out_path, "rb") as f:
            content = f.read()
        return StreamingResponse(
            io.BytesIO(content),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{base}.pdf"'},
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ── PDF to JPG (all pages as zip) ─────────────────────────────────────────────
@app.post("/convert/pdf-to-jpg")
async def pdf_to_jpg(file: UploadFile = File(...), dpi: int = Form(150)):
    data = await file.read()
    doc = fitz.open(stream=data, filetype="pdf")
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, page in enumerate(doc):
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat)
            zf.writestr(f"page_{i+1:03d}.jpg", pix.tobytes("jpeg"))
    doc.close()
    zip_buf.seek(0)
    name = file.filename.rsplit(".", 1)[0] + "_pages.zip"
    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ── Compress PDF ──────────────────────────────────────────────────────────────
@app.post("/optimize/compress")
async def compress_pdf(file: UploadFile = File(...), level: int = Form(2)):
    # level: 1=low, 2=medium, 3=high
    data = await file.read()
    doc = fitz.open(stream=data, filetype="pdf")
    out = io.BytesIO()
    deflate = level >= 2
    clean = level >= 2
    doc.save(
        out,
        garbage=3 if level == 3 else 1,
        deflate=deflate,
        clean=clean,
        deflate_images=level == 3,
        deflate_fonts=level == 3,
    )
    doc.close()
    out.seek(0)
    name = file.filename.rsplit(".", 1)[0] + "_compressed.pdf"
    return StreamingResponse(
        out,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ── Protect PDF ───────────────────────────────────────────────────────────────
@app.post("/security/protect")
async def protect_pdf(
    file: UploadFile = File(...),
    user_password: str = Form(...),
    owner_password: str = Form(""),
):
    data = await file.read()
    doc = fitz.open(stream=data, filetype="pdf")
    out = io.BytesIO()
    perm = fitz.PDF_PERM_PRINT | fitz.PDF_PERM_COPY
    doc.save(
        out,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        user_pw=user_password,
        owner_pw=owner_password or user_password,
        permissions=perm,
    )
    doc.close()
    out.seek(0)
    name = file.filename.rsplit(".", 1)[0] + "_protected.pdf"
    return StreamingResponse(
        out,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ── Unlock PDF ────────────────────────────────────────────────────────────────
@app.post("/security/unlock")
async def unlock_pdf(
    file: UploadFile = File(...),
    password: str = Form(...),
):
    data = await file.read()
    doc = fitz.open(stream=data, filetype="pdf")
    if doc.is_encrypted:
        if not doc.authenticate(password):
            raise HTTPException(status_code=400, detail="Wrong password")
    out = io.BytesIO()
    doc.save(out, encryption=fitz.PDF_ENCRYPT_NONE)
    doc.close()
    out.seek(0)
    name = file.filename.rsplit(".", 1)[0] + "_unlocked.pdf"
    return StreamingResponse(
        out,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ── Redact PDF ────────────────────────────────────────────────────────────────
@app.post("/edit/redact")
async def redact_pdf(
    file: UploadFile = File(...),
    text: str = Form(...),
):
    data = await file.read()
    doc = fitz.open(stream=data, filetype="pdf")
    for page in doc:
        areas = page.search_for(text)
        for rect in areas:
            page.add_redact_annot(rect)
        page.apply_redactions()
    out = io.BytesIO()
    doc.save(out)
    doc.close()
    out.seek(0)
    name = file.filename.rsplit(".", 1)[0] + "_redacted.pdf"
    return StreamingResponse(
        out,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ── OCR PDF ───────────────────────────────────────────────────────────────────
@app.post("/edit/ocr")
async def ocr_pdf(file: UploadFile = File(...)):
    try:
        import pytesseract
        from PIL import Image
        import pdf2image
    except ImportError:
        raise HTTPException(status_code=501, detail="OCR dependencies not installed")

    data = await file.read()
    pdf_path = tmp_path(".pdf")
    try:
        with open(pdf_path, "wb") as f:
            f.write(data)
        images = pdf2image.convert_from_path(pdf_path, dpi=200)
        out_pdf = fitz.open()
        for img in images:
            page_text = pytesseract.image_to_pdf_or_hocr(img, extension="pdf")
            sub = fitz.open("pdf", page_text)
            out_pdf.insert_pdf(sub)
        out = io.BytesIO()
        out_pdf.save(out)
        out_pdf.close()
        out.seek(0)
        name = file.filename.rsplit(".", 1)[0] + "_ocr.pdf"
        return StreamingResponse(
            out,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)
