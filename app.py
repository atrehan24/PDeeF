import os
import io
from flask import Flask, render_template, request, send_file
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
import pikepdf
import fitz
from pypdf import PdfReader, PdfWriter
from pdf2image import convert_from_path
import pytesseract
from docx import Document
from PIL import Image

# ---------------- LOAD ENV ----------------
load_dotenv()

# ---------------- APP INIT ----------------
app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ---------------- OCR CONFIG ----------------
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
POPPLER_PATH = r"C:\poppler\poppler-25.12.0\Library\bin"

# ---------------- DATABASE ----------------
class FileRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    operation = db.Column(db.String(50), nullable=False)
    file_data = db.Column(db.LargeBinary, nullable=False)  # 🔥 binary storage

# ---------------- FOLDERS ----------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOADS = os.path.join(BASE_DIR, "uploads")

PDF_DIR = os.path.join(UPLOADS, "pdf")
PREVIEW_DIR = os.path.join(UPLOADS, "preview")

for f in [PDF_DIR, PREVIEW_DIR]:
    os.makedirs(f, exist_ok=True)

# ---------------- ROUTES ----------------
@app.route("/")
def index():
    return render_template("index.html")

# -------- UPLOAD & PROCESS --------
@app.route("/process", methods=["POST"])
def process_pdf():
    file = request.files.get("file")
    operation = request.form.get("operation")

    if not file:
        return "No file uploaded", 400

    pdf_path = os.path.join(PDF_DIR, file.filename)
    file.save(pdf_path)

    output_filename = ""
    output_bytes = None

    # ---------- SPLIT → PREVIEW ----------
    if operation == "split":
        images = convert_from_path(pdf_path, dpi=120, poppler_path=POPPLER_PATH)

        img_paths = []
        for i, img in enumerate(images):
            img_name = f"{file.filename}_{i}.png"
            img_path = os.path.join(PREVIEW_DIR, img_name)
            img.save(img_path, "PNG")
            img_paths.append(img_name)

        return render_template("preview.html", pdf_name=file.filename, images=img_paths)

    # ---------- PDF → WORD ----------
    elif operation == "convert":
        output_filename = file.filename.replace(".pdf", ".docx")

        pages = convert_from_path(pdf_path, poppler_path=POPPLER_PATH)
        doc = Document()

        for p in pages:
            doc.add_paragraph(pytesseract.image_to_string(p))

        buffer = io.BytesIO()
        doc.save(buffer)
        buffer.seek(0)
        output_bytes = buffer.read()

    # ---------- HIGH COMPRESSION ----------
    elif operation == "compress":
        output_filename = file.filename.replace(".pdf", "_compressed.pdf")
        output_bytes = None
        DPI_THRESHOLD = 200
        TARGET_DPI = 150
        JPEG_QUALITY = 85
        try:
            pdf = pikepdf.open(pdf_path)
            has_vector = any(page.get("/Contents") for page in pdf.pages)
        except:
            has_vector = False
        if has_vector:
            buffer = io.BytesIO()
            pdf.save(buffer, optimize_streams=True)
            buffer.seek(0)
            output_bytes = buffer.read()

            
            buffer = io.BytesIO()
            pdf.save(buffer, optimize_streams=True, compression=pikepdf.CompressionLevel.default)
            buffer.seek(0)
            output_bytes = buffer.read()
            print("Digital PDF compressed using vector optimization.")
        else:
            doc = fitz.open(pdf_path)
            for page_index in range(len(doc)):
                page = doc[page_index]
                images = page.get_images(full=True)
                for img_index, img in enumerate(images):
                    xref = img[0]
                    base_image = doc.extract_image(xref)
                    img_bytes = base_image["image"]
                    image = Image.open(io.BytesIO(img_bytes))
                    if image.mode == "1":
                        compression = "CCITT"
                    elif image.mode == "L":
                        compression = "JPEG"
                    else:
                        compression = "JPEG"
                    dpi = image.info.get("dpi", (TARGET_DPI, TARGET_DPI))[0]
                    if dpi > DPI_THRESHOLD:
                        factor = TARGET_DPI / dpi
                        new_width = int(image.width * factor)
                        new_height = int(image.height * factor)
                        image = image.resize((new_width, new_height), Image.LANCZOS)
                    img_bytes_io = io.BytesIO()
                    if compression == "CCITT":
                        image.save(img_bytes_io, format="TIFF", compression="group4")
                    else:
                        image.save(img_bytes_io, format="JPEG", quality=JPEG_QUALITY, optimize=True)
                    img_bytes_io.seek(0)
                    doc.update_image(xref, stream=img_bytes_io.read())
        buffer = io.BytesIO()
        doc.save(buffer, garbage=4, deflate=True)
        buffer.seek(0)
        output_bytes = buffer.read()
        print("Scanned PDF compressed with image optimization.")


    else:
        return "Invalid operation", 400

    # -------- SAVE PROCESSED FILE AS BINARY --------
    record = FileRecord(
        filename=output_filename,
        operation=operation,
        file_data=output_bytes
    )
    db.session.add(record)
    db.session.commit()

    return render_template(
        "result.html",
        filename=output_filename,
        operation=operation,
        file_id=record.id
    )

# -------- FINAL SPLIT --------
@app.route("/split-final", methods=["POST"])
def split_final():
    pdf_name = request.form.get("pdf_name")
    pages = request.form.getlist("pages")

    reader = PdfReader(os.path.join(PDF_DIR, pdf_name))
    writer = PdfWriter()

    for p in pages:
        writer.add_page(reader.pages[int(p)])

    buffer = io.BytesIO()
    writer.write(buffer)
    buffer.seek(0)

    output_filename = pdf_name.replace(".pdf", "_selected.pdf")

    record = FileRecord(
        filename=output_filename,
        operation="split",
        file_data=buffer.read()
    )
    db.session.add(record)
    db.session.commit()

    return render_template(
        "result.html",
        filename=output_filename,
        operation="split",
        file_id=record.id
    )

# -------- DOWNLOAD FROM DATABASE --------
@app.route("/download/<int:file_id>")
def download(file_id):
    record = FileRecord.query.get_or_404(file_id)

    return send_file(
        io.BytesIO(record.file_data),
        download_name=record.filename,
        as_attachment=True
    )

# -------- PREVIEW IMAGE --------
@app.route("/uploads/preview/<filename>")
def preview_image(filename):
    return send_file(os.path.join(PREVIEW_DIR, filename))

# ---------------- MAIN ----------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
