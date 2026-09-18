import io
import os
import json
import mimetypes
import uuid
import re
import difflib
import shutil
import hashlib
import time
from datetime import datetime, date
from functools import wraps

from flask import (
    Flask, render_template, redirect, url_for, request,
    flash, jsonify, abort, Response, stream_with_context
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ---- Optional dependencies -------------------------------------------------
# OCR is a "nice to have" feature. If the packages are missing (e.g. a fresh
# dev machine before `pip install -r requirements.txt`), the app must still
# boot and every *other* feature must keep working — we just show a clear
# message wherever OCR is used.
try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False

try:
    import pytesseract
    from PIL import Image, ImageOps
    from pytesseract import Output as TesseractOutput
    PYTESSERACT_AVAILABLE = True
except ImportError:
    PYTESSERACT_AVAILABLE = False

try:
    import openpyxl
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
VIDEO_EXTENSIONS = {"mp4", "webm", "mov"}
VIDEO_MIME_PREFIXES = ("video/",)
OCR_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "pdf"}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS  # kept for backward compatibility

DEFAULT_BOARD_TITLE = "KLS VDIT EEE DEPARTMENT SMART NOTICE BOARD"
GRID_PHOTO_COUNT = 2     # grid mode is fixed at exactly 2 photos, side-by-side 50/50
ASSET_VERSION = "17"  # bump this whenever display.css/display.js change, to bust the 1-year static cache

# ---- Timezone ----
# The admin enters class times as local wall-clock time (this department is
# in India), but the hosting server's own system clock may run in a
# different timezone (Render's servers run UTC). Without accounting for
# that, "is this class happening right now?" checks compare the wrong hour
# entirely, so the timetable's "currently in session" slide never lines up
# with reality once deployed. now_local() below is the one place "what time
# is it right now, for this department" is answered from, regardless of
# what timezone the underlying server machine itself is set to.
try:
    from zoneinfo import ZoneInfo
    APP_TIMEZONE = ZoneInfo(os.environ.get("APP_TIMEZONE", "Asia/Kolkata"))
except Exception:
    # Extremely defensive fallback — should not happen as long as the
    # `tzdata` package (see requirements.txt) is installed, but the app
    # must never crash over this.
    APP_TIMEZONE = None


def now_local():
    """The current date/time in the department's local timezone (IST by
    default), independent of the server machine's own system timezone."""
    if APP_TIMEZONE is not None:
        return datetime.now(APP_TIMEZONE)
    return datetime.now()

# ---- OCR config ----
# Render/Linux normally exposes Tesseract as /usr/bin/tesseract after the
# package is installed. Windows commonly uses the Program Files path.
# If TESSERACT_CMD is set to a stale path (for example a Windows path on
# Render), ignore it and automatically fall back to a real executable.
_configured_tesseract = (os.environ.get("TESSERACT_CMD") or "").strip()
_tesseract_candidates = [
    _configured_tesseract,
    shutil.which("tesseract") or "",
    "/usr/bin/tesseract",
    "/usr/local/bin/tesseract",
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]
TESSERACT_CMD = next((p for p in _tesseract_candidates if p and os.path.isfile(p) and os.access(p, os.X_OK)), "")
OCR_LANGUAGES = (os.environ.get("OCR_LANGUAGES") or "eng").strip()
OCR_MAX_PDF_PAGES = 10
OCR_MAX_PDF_BYTES = 20 * 1024 * 1024  # 20 MB
OCR_MAX_DISPLAY_CHARS = 1200  # beyond this, warn the admin the text is too long for a readable slide

if PYTESSERACT_AVAILABLE and TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "noticeboard.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
# 200 MB per request — large enough for a video upload plus form fields.
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000  # cache static files hard — filenames are unique per upload

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access the admin portal."
login_manager.login_message_category = "info"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

CATEGORIES = {
    "notice": {"label": "Notice", "color": "#3E7CB1"},
    "achievement": {"label": "Achievement", "color": "#C99A2E"},
    "important": {"label": "Important", "color": "#C1443C"},
    "event": {"label": "Event", "color": "#2E8B72"},
}
DISPLAY_TYPES = ["text", "image", "grid", "video"]
DISPLAY_TYPE_LABELS = {
    "text": "Text",
    "image": "Single image",
    "grid": "Grid — 2 photos, side by side (50% / 50%)",
    "video": "Video",
}
IMAGE_FIT_OPTIONS = {
    "contain": "Show the whole photo (nothing cropped)",
    "cover": "Fill the screen (crops edges if needed)",
}
DEFAULT_IMAGE_FIT = "contain"
DEFAULT_POPUP_DURATION = 15  # seconds an "important" notice's full-screen popup stays up

# ---- Timetable module ----
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
TIMETABLE_SLIDE_SECONDS = 10  # how long the "current lecture status" slide stays up in the display rotation

# Year -> the semesters that are valid for it. 1st year deliberately has no
# entry here — it has been removed from the flexible timetable interface.
YEAR_SEMESTER_MAP = {
    "2nd Year": ["Semester 3", "Semester 4"],
    "3rd Year": ["Semester 5", "Semester 6"],
    "4th Year": ["Semester 7", "Semester 8"],
}
TIMETABLE_YEARS = list(YEAR_SEMESTER_MAP.keys())
TIMETABLE_UPLOAD_EXTENSIONS = {"xlsx", "xls", "csv", "pdf", "jpg", "jpeg", "png", "webp"}


def grid_slot_names():
    """File-field names for the (fixed, 2-photo) grid: photo_1, photo_2."""
    return [f"photo_{i}" for i in range(1, GRID_PHOTO_COUNT + 1)]


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    name = db.Column(db.String(128), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(16), nullable=False, default="staff")  # admin | staff
    created_at = db.Column(db.DateTime, default=datetime.now)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == "admin"


class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    board_title = db.Column(db.String(200), nullable=False, default=DEFAULT_BOARD_TITLE)


def get_settings():
    settings = Settings.query.first()
    if settings is None:
        settings = Settings(board_title=DEFAULT_BOARD_TITLE)
        db.session.add(settings)
        db.session.commit()
    return settings


@app.context_processor
def inject_settings():
    return {
        "site_settings": get_settings(),
        "asset_version": ASSET_VERSION,
        "ocr_available": PYTESSERACT_AVAILABLE and PYMUPDF_AVAILABLE,
    }


class Notice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(20), nullable=False, default="notice")
    display_type = db.Column(db.String(20), nullable=False, default="text")
    text_content = db.Column(db.Text, nullable=True)
    images = db.Column(db.Text, nullable=True)  # JSON list of filenames
    video_filename = db.Column(db.String(255), nullable=True)
    image_fit = db.Column(db.String(10), nullable=False, default=DEFAULT_IMAGE_FIT)  # cover | contain
    is_active = db.Column(db.Boolean, default=True)
    priority = db.Column(db.Integer, default=0)  # higher shows first
    duration_seconds = db.Column(db.Integer, default=10)  # how long to show in the normal rotation
    popup_duration_seconds = db.Column(db.Integer, default=15)  # how long an "important" full-screen popup stays up (separate from rotation duration)
    show_caption = db.Column(db.Boolean, default=True)  # if False, photo notices show pure full-bleed image — no badge/title/date bar at all
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_by = db.relationship("User", backref="notices")
    # Always taken from the server's own clock at the moment the row is
    # inserted/updated — the admin never types a date/time in, so what's
    # stored always matches when the post was actually made. Uses local
    # server time (not UTC) so the displayed date/day lines up with the
    # timezone the Raspberry Pi/display is actually running in.
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    expires_at = db.Column(db.Date, nullable=True)

    # ---- OCR metadata (optional) ----
    ocr_used = db.Column(db.Boolean, default=False)
    ocr_source_filename = db.Column(db.String(255), nullable=True)
    ocr_extracted_at = db.Column(db.DateTime, nullable=True)

    def image_list(self):
        if not self.images:
            return []
        try:
            return json.loads(self.images)
        except (ValueError, TypeError):
            return []

    def is_expired(self):
        if self.expires_at is None:
            return False
        return self.expires_at < now_local().date()

    def is_live(self):
        return self.is_active and not self.is_expired()

    def category_label(self):
        return CATEGORIES.get(self.category, {}).get("label", self.category)

    def category_color(self):
        return CATEGORIES.get(self.category, {}).get("color", "#888888")

    def to_display_dict(self):
        image_objs = [
            {"url": url_for("static", filename=f"uploads/{name}")}
            for name in self.image_list()
        ]
        video_obj = None
        if self.video_filename:
            video_obj = {"url": url_for("static", filename=f"uploads/{self.video_filename}")}

        # "Posted" always reflects when the notice was first created, even if
        # it's since been edited — edits are called out separately so the
        # display never shows a misleading/updated date as the post date.
        posted_at = self.created_at or self.updated_at
        was_edited = bool(
            self.updated_at and posted_at and (self.updated_at - posted_at).total_seconds() > 60
        )
        title = (self.title or "").strip()
        return {
            "id": self.id,
            "title": title,
            "has_title": bool(title),
            "category": self.category,
            "category_label": self.category_label(),
            "category_color": self.category_color(),
            "display_type": self.display_type,
            "text_content": self.text_content or "",
            "images": image_objs,
            "video": video_obj,
            "image_fit": self.image_fit or DEFAULT_IMAGE_FIT,
            "duration_seconds": self.duration_seconds or 10,
            "popup_duration_seconds": self.popup_duration_seconds or DEFAULT_POPUP_DURATION,
            "show_caption": self.show_caption if self.show_caption is not None else True,
            "author": self.created_by.name if self.created_by else "",
            "posted_at": posted_at.strftime("%d %b %Y, %I:%M %p") if posted_at else "",
            "updated_at": self.updated_at.strftime("%d %b %Y, %I:%M %p") if self.updated_at else "",
            "was_edited": was_edited,
            "expires_at": self.expires_at.strftime("%d %b %Y") if self.expires_at else "",
        }


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# --------------------------------------------------------------------------
# Timetable models
# --------------------------------------------------------------------------
class Timetable(db.Model):
    """Legacy fixed-column timetable model (1st/2nd/3rd/4th year faculty per
    slot). No longer editable from the admin UI — kept only so existing rows
    aren't lost. See TimetableEntry for the current, flexible model."""
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.String(10), nullable=False)          # "Monday".."Saturday"
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    year1_faculty = db.Column(db.String(120), nullable=False)
    year2_faculty = db.Column(db.String(120), nullable=False)
    year3_faculty = db.Column(db.String(120), nullable=False)
    year4_faculty = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    def time_range_label(self):
        return f"{self.start_time.strftime('%I:%M %p')} – {self.end_time.strftime('%I:%M %p')}"


class TimetableEntry(db.Model):
    """One class row. Uploaded/manual semester schedules are kept separately.
    Only one semester per year is published at a time, so the public board
    never repeats a year."""
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.String(10), nullable=False, index=True)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    year = db.Column(db.String(20), nullable=False)
    semester = db.Column(db.String(20), nullable=False)
    subject_code = db.Column(db.String(40), nullable=True)
    subject_title = db.Column(db.String(200), nullable=True)
    teacher_name = db.Column(db.String(120), nullable=True)
    published = db.Column(db.Boolean, default=True, nullable=False, index=True)
    source_filename = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    def time_range_label(self):
        return f"{self.start_time.strftime('%I:%M %p')} – {self.end_time.strftime('%I:%M %p')}"

    def is_current(self, now=None):
        now = now or now_local()
        return now.strftime("%A") == self.day and self.start_time <= now.time() < self.end_time

    def has_teacher(self):
        return bool((self.teacher_name or "").strip())


# --------------------------------------------------------------------------
# Helpers — files
# --------------------------------------------------------------------------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in IMAGE_EXTENSIONS


def allowed_video_file(filename, mimetype=None):
    if "." not in filename:
        return False
    ext_ok = filename.rsplit(".", 1)[1].lower() in VIDEO_EXTENSIONS
    if not ext_ok:
        return False
    # MIME check is best-effort — browsers/OSes don't always send a precise
    # video/* content type, so we don't hard-fail on an unusual one as long
    # as the extension is one we accept. We only reject if the browser sent
    # a MIME type that's clearly NOT a video.
    guessed = mimetypes.guess_type(filename)[0] or ""
    if mimetype and mimetype not in ("application/octet-stream", ""):
        if not mimetype.startswith(VIDEO_MIME_PREFIXES) and not guessed.startswith("video/"):
            return False
    return True


def unique_filename(original_filename):
    safe_name = secure_filename(original_filename) or "file"
    return f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{uuid.uuid4().hex[:8]}_{safe_name}"


def delete_notice_image(image_filename):
    """Remove a stored notice photo/video, if present."""
    path = os.path.join(app.config["UPLOAD_FOLDER"], image_filename)
    if os.path.exists(path):
        os.remove(path)


def save_uploaded_images(files):
    saved = []
    for f in files:
        if f and f.filename and allowed_file(f.filename):
            unique_name = unique_filename(f.filename)
            f.save(os.path.join(app.config["UPLOAD_FOLDER"], unique_name))
            saved.append(unique_name)
    return saved


def save_single_image(f, folder=None):
    """Save one uploaded file and return its stored filename, or None."""
    if not (f and f.filename and allowed_file(f.filename)):
        return None
    unique_name = unique_filename(f.filename)
    target_folder = folder or app.config["UPLOAD_FOLDER"]
    f.save(os.path.join(target_folder, unique_name))
    return unique_name


def save_single_video(f):
    """Save one uploaded video and return its stored filename, or None."""
    if not (f and f.filename):
        return None
    if not allowed_video_file(f.filename, getattr(f, "mimetype", None)):
        return None
    unique_name = unique_filename(f.filename)
    f.save(os.path.join(app.config["UPLOAD_FOLDER"], unique_name))
    return unique_name


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------
# Helpers — OCR
# --------------------------------------------------------------------------
class OcrError(Exception):
    pass


def ocr_extract_from_image_bytes(data):
    if not PYTESSERACT_AVAILABLE:
        raise OcrError("OCR is not available on this server (Tesseract/pytesseract not installed).")
    try:
        image = Image.open(io.BytesIO(data))
        text = pytesseract.image_to_string(image, lang=OCR_LANGUAGES or "eng")
    except pytesseract.TesseractNotFoundError:
        raise OcrError("OCR could not process this file (Tesseract executable not found — check TESSERACT_CMD).")
    except Exception:
        raise OcrError("OCR could not process this file.")
    return text.strip()


def ocr_extract_from_pdf_bytes(data):
    if not PYMUPDF_AVAILABLE:
        raise OcrError("OCR is not available on this server (PyMuPDF not installed).")
    if len(data) > OCR_MAX_PDF_BYTES:
        raise OcrError(f"PDF is too large for OCR (max {OCR_MAX_PDF_BYTES // (1024 * 1024)} MB).")

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise OcrError("OCR could not process this file (corrupted or unsupported PDF).")

    try:
        if doc.is_encrypted:
            raise OcrError("This PDF is encrypted/password-protected and can't be processed.")
        if doc.page_count > OCR_MAX_PDF_PAGES:
            raise OcrError(f"PDF has too many pages for OCR (max {OCR_MAX_PDF_PAGES} pages).")

        # 1. Try embedded/selectable text first — fast and perfectly accurate.
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text().strip())
        combined = "\n".join(p for p in text_parts if p).strip()
        if combined:
            return combined

        # 2. No usable embedded text — render pages as images and OCR them.
        if not PYTESSERACT_AVAILABLE:
            raise OcrError("No selectable text was found, and OCR is not available on this server.")
        ocr_parts = []
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            img_bytes = pix.tobytes("png")
            try:
                image = Image.open(io.BytesIO(img_bytes))
                ocr_parts.append(pytesseract.image_to_string(image, lang=OCR_LANGUAGES or "eng").strip())
            except pytesseract.TesseractNotFoundError:
                raise OcrError("OCR could not process this file (Tesseract executable not found — check TESSERACT_CMD).")
        return "\n".join(p for p in ocr_parts if p).strip()
    finally:
        doc.close()


def run_ocr(file_storage):
    """Runs OCR on an uploaded file (image or PDF) fully in memory, and
    returns the extracted text (may be an empty string if nothing readable
    was found). No temp files are written to disk for images; PDFs are
    processed in-memory via PyMuPDF as well."""
    filename = file_storage.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in OCR_EXTENSIONS:
        raise OcrError("Unsupported file type for OCR. Use JPG, PNG, WEBP, or PDF.")

    data = file_storage.read()
    if not data:
        raise OcrError("The uploaded file is empty.")

    if ext == "pdf":
        return ocr_extract_from_pdf_bytes(data)
    return ocr_extract_from_image_bytes(data)


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    return redirect(url_for("display"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            next_page = request.args.get("next")
            return redirect(next_page or url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# Dashboard routes
# --------------------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    category_filter = request.args.get("category", "all")
    status_filter = request.args.get("status", "all")

    query = Notice.query
    if category_filter != "all":
        query = query.filter_by(category=category_filter)
    if status_filter == "active":
        query = query.filter_by(is_active=True)
    elif status_filter == "inactive":
        query = query.filter_by(is_active=False)

    notices = query.order_by(Notice.priority.desc(), Notice.created_at.desc()).all()

    counts = {
        "total": Notice.query.count(),
        "notice": Notice.query.filter_by(category="notice").count(),
        "achievement": Notice.query.filter_by(category="achievement").count(),
        "important": Notice.query.filter_by(category="important").count(),
        "event": Notice.query.filter_by(category="event").count(),
        "active": Notice.query.filter_by(is_active=True).count(),
    }

    return render_template(
        "dashboard.html",
        notices=notices,
        categories=CATEGORIES,
        counts=counts,
        category_filter=category_filter,
        status_filter=status_filter,
        type_labels=DISPLAY_TYPE_LABELS,
    )


@app.route("/notice/add", methods=["GET", "POST"])
@login_required
def add_notice():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "notice")
        display_type = request.form.get("display_type", "text")
        text_content = request.form.get("text_content", "").strip()
        priority = request.form.get("priority", 0, type=int)
        duration_seconds = request.form.get("duration_seconds", 10, type=int)
        popup_duration_seconds = request.form.get("popup_duration_seconds", DEFAULT_POPUP_DURATION, type=int)
        show_caption = request.form.get("show_caption") == "1"
        expires_at_raw = request.form.get("expires_at", "").strip()
        image_fit = request.form.get("image_fit", DEFAULT_IMAGE_FIT)
        if image_fit not in IMAGE_FIT_OPTIONS:
            image_fit = DEFAULT_IMAGE_FIT

        ocr_used = request.form.get("ocr_used") == "1"
        ocr_source_filename = request.form.get("ocr_source_filename", "").strip() or None
        use_extracted_as_text = False

        # Title is optional — a notice can be posted with just a photo/text
        # and no headline. It's only required if the admin wants one shown.
        if category not in CATEGORIES:
            category = "notice"
        if display_type not in DISPLAY_TYPES:
            display_type = "text"
        # OCR is deliberately a text-post feature only. Image/grid/video posts
        # never invoke OCR or create companion text slides.
        if display_type != "text":
            ocr_used = False
            ocr_source_filename = None
            use_extracted_as_text = False

        # An OCR'd PDF is always shown as a reviewed text slide — never the
        # raw PDF on the TV.
        if ocr_used and ocr_source_filename and ocr_source_filename.lower().endswith(".pdf"):
            display_type = "text"
        saved_media = {"image": [], "grid": [], "video": []}

        if display_type == "grid":
            saved = []
            for slot in grid_slot_names():
                filename = save_single_image(request.files.get(slot))
                if not filename:
                    for already_saved in saved:
                        delete_notice_image(already_saved)
                    flash("Grid mode needs exactly 2 photos — please upload a photo for both slots.", "error")
                    return redirect(url_for("add_notice"))
                saved.append(filename)
            saved_media["grid"] = saved
        elif display_type == "image":
            files = request.files.getlist("images")
            saved = save_uploaded_images(files)
            if not saved:
                flash("Please upload at least one image for this display type.", "error")
                return redirect(url_for("add_notice"))
            saved_media["image"] = saved
        elif display_type == "video":
            video_file = request.files.get("video")
            filename = save_single_video(video_file)
            if not filename:
                if video_file and video_file.filename:
                    flash("Unsupported video file. Please upload an MP4, WEBM, or MOV file.", "error")
                else:
                    flash("Please upload a video file for this display type.", "error")
                return redirect(url_for("add_notice"))
            saved_media["video"] = filename
        elif display_type == "text" and not text_content:
            flash("Please enter text content.", "error")
            return redirect(url_for("add_notice"))

        expires_at = None
        if expires_at_raw:
            try:
                expires_at = datetime.strptime(expires_at_raw, "%Y-%m-%d").date()
            except ValueError:
                expires_at = None

        legacy_images = saved_media["image"] or saved_media["grid"]
        notice = Notice(
            title=title,
            category=category,
            display_type=display_type,
            text_content=text_content or None,
            images=json.dumps(legacy_images) if legacy_images else None,
            video_filename=saved_media["video"] or None,
            image_fit=image_fit,
            priority=priority,
            duration_seconds=max(3, duration_seconds or 10),
            popup_duration_seconds=max(5, popup_duration_seconds or DEFAULT_POPUP_DURATION),
            show_caption=show_caption,
            created_by_id=current_user.id,
            expires_at=expires_at,
            ocr_used=ocr_used,
            ocr_source_filename=ocr_source_filename,
            ocr_extracted_at=datetime.now() if ocr_used else None,
        )
        db.session.add(notice)
        db.session.commit()

        flash("Notice published successfully.", "success")
        return redirect(url_for("dashboard"))

    return render_template(
        "add_notice.html", categories=CATEGORIES, notice=None,
        type_labels=DISPLAY_TYPE_LABELS, fit_options=IMAGE_FIT_OPTIONS,
        grid_photo_count=GRID_PHOTO_COUNT,
    )


@app.route("/notice/edit/<int:notice_id>", methods=["GET", "POST"])
@login_required
def edit_notice(notice_id):
    notice = Notice.query.get_or_404(notice_id)
    if not current_user.is_admin and notice.created_by_id != current_user.id:
        abort(403)

    if request.method == "POST":
        notice.title = request.form.get("title", notice.title).strip()
        category = request.form.get("category", notice.category)
        if category in CATEGORIES:
            notice.category = category
        display_type = request.form.get("display_type", notice.display_type)
        if display_type in DISPLAY_TYPES:
            notice.display_type = display_type
        notice.text_content = request.form.get("text_content", "").strip() or None
        notice.priority = request.form.get("priority", notice.priority, type=int)
        notice.duration_seconds = max(3, request.form.get("duration_seconds", notice.duration_seconds, type=int) or 10)
        notice.popup_duration_seconds = max(
            5, request.form.get("popup_duration_seconds", notice.popup_duration_seconds, type=int) or DEFAULT_POPUP_DURATION
        )
        notice.show_caption = request.form.get("show_caption") == "1"
        image_fit = request.form.get("image_fit", notice.image_fit)
        if image_fit in IMAGE_FIT_OPTIONS:
            notice.image_fit = image_fit

        ocr_used = request.form.get("ocr_used") == "1" and notice.display_type == "text"
        if ocr_used:
            notice.ocr_used = True
            notice.ocr_source_filename = request.form.get("ocr_source_filename", "").strip() or notice.ocr_source_filename
            notice.ocr_extracted_at = datetime.now()
        elif notice.display_type != "text":
            notice.ocr_used = False
            notice.ocr_source_filename = None
            notice.ocr_extracted_at = None

        expires_at_raw = request.form.get("expires_at", "").strip()
        if expires_at_raw:
            try:
                notice.expires_at = datetime.strptime(expires_at_raw, "%Y-%m-%d").date()
            except ValueError:
                pass
        else:
            notice.expires_at = None

        if notice.display_type == "grid":
            existing = {name: True for name in notice.image_list()}
            updated = []
            newly_saved = []
            missing_slot = None
            for idx, slot in enumerate(grid_slot_names(), start=1):
                new_file = request.files.get(slot)
                existing_name = request.form.get(f"existing_{slot}", "").strip()
                saved_name = save_single_image(new_file)
                if saved_name:
                    newly_saved.append(saved_name)
                    updated.append(saved_name)
                elif existing_name:
                    updated.append(existing_name)
                else:
                    missing_slot = idx
                    break

            if missing_slot is not None:
                for f in newly_saved:
                    delete_notice_image(f)
                flash(f"Photo {missing_slot} needs an image — a grid needs both photos.", "error")
                return redirect(url_for("edit_notice", notice_id=notice.id))

            removed_files = [name for name in existing if name not in updated]
            for name in removed_files:
                delete_notice_image(name)
            notice.images = json.dumps(updated)

        elif notice.display_type == "image":
            files = request.files.getlist("images")
            saved = save_uploaded_images(files)
            if saved:
                for name in notice.image_list():
                    delete_notice_image(name)
                notice.images = json.dumps(saved)
            elif not notice.images:
                flash("Please upload at least one image for this display type.", "error")
                return redirect(url_for("edit_notice", notice_id=notice.id))

        elif notice.display_type == "video":
            video_file = request.files.get("video")
            if video_file and video_file.filename:
                filename = save_single_video(video_file)
                if not filename:
                    flash("Unsupported video file. Please upload an MP4, WEBM, or MOV file.", "error")
                    return redirect(url_for("edit_notice", notice_id=notice.id))
                if notice.video_filename:
                    delete_notice_image(notice.video_filename)
                notice.video_filename = filename
            elif not notice.video_filename:
                flash("Please upload a video for this display type.", "error")
                return redirect(url_for("edit_notice", notice_id=notice.id))

        notice.updated_at = datetime.now()
        db.session.commit()
        flash("Notice updated successfully.", "success")
        return redirect(url_for("dashboard"))

    return render_template(
        "add_notice.html", categories=CATEGORIES, notice=notice,
        type_labels=DISPLAY_TYPE_LABELS, fit_options=IMAGE_FIT_OPTIONS,
        grid_photo_count=GRID_PHOTO_COUNT,
    )


@app.route("/notice/toggle/<int:notice_id>", methods=["POST"])
@login_required
def toggle_notice(notice_id):
    notice = Notice.query.get_or_404(notice_id)
    if not current_user.is_admin and notice.created_by_id != current_user.id:
        abort(403)
    notice.is_active = not notice.is_active
    db.session.commit()
    return redirect(url_for("dashboard"))


@app.route("/notice/delete/<int:notice_id>", methods=["POST"])
@login_required
def delete_notice(notice_id):
    notice = Notice.query.get_or_404(notice_id)
    if not current_user.is_admin and notice.created_by_id != current_user.id:
        abort(403)
    for img in notice.image_list():
        delete_notice_image(img)
    if notice.video_filename:
        delete_notice_image(notice.video_filename)
    db.session.delete(notice)
    db.session.commit()
    flash("Notice deleted.", "success")
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------------------
# OCR — Add Notice / Edit Notice helper endpoint (AJAX)
# --------------------------------------------------------------------------
@app.route("/notice/ocr/extract", methods=["POST"])
@login_required
def ocr_extract():
    if not (PYTESSERACT_AVAILABLE and PYMUPDF_AVAILABLE):
        return jsonify({
            "status": "error",
            "message": "OCR could not process this file (OCR packages are not installed on this server).",
        }), 200

    file_storage = request.files.get("ocr_file")
    if not file_storage or not file_storage.filename:
        return jsonify({"status": "error", "message": "Please choose an image or PDF file first."}), 200

    try:
        text = run_ocr(file_storage)
    except OcrError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 200
    except Exception:
        return jsonify({"status": "error", "message": "OCR could not process this file."}), 200

    if not text:
        return jsonify({"status": "empty", "message": "No readable text was found.", "text": ""}), 200

    too_long = len(text) > OCR_MAX_DISPLAY_CHARS
    return jsonify({
        "status": "success",
        "message": "Text extracted successfully. Please review it before publishing.",
        "text": text,
        "too_long": too_long,
        "max_chars": OCR_MAX_DISPLAY_CHARS,
        "source_filename": secure_filename(file_storage.filename),
    })


# --------------------------------------------------------------------------
# Public QR page + media QR image
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Timetable management — flexible year/semester class entries. The public
# display reads whichever entries match the current day/time and have a
# teacher name filled in (see /api/timetable/current further down).
# --------------------------------------------------------------------------
def parse_clock_time(raw):
    """Parse an HTML <input type="time"> value ('HH:MM', 24-hour) into a
    Python time object, or None if it isn't valid."""
    try:
        return datetime.strptime((raw or "").strip(), "%H:%M").time()
    except ValueError:
        return None


def validate_timetable_entry_form(form):
    """Shared validation for add/edit. Returns (fields_dict, error_message).
    error_message is None when everything is valid. Teacher name is allowed
    to be blank (entries without one just won't show on the public display)."""
    day = form.get("day", "").strip()
    start_time = parse_clock_time(form.get("start_time"))
    end_time = parse_clock_time(form.get("end_time"))
    year = form.get("year", "").strip()
    semester = form.get("semester", "").strip()
    subject_code = _normalize_code(form.get("subject_code", "").strip())
    subject_title = form.get("subject_title", "").strip()
    teacher_name = form.get("teacher_name", "").strip()

    if day not in WEEKDAYS:
        return None, "Please choose a valid day (Monday–Saturday)."
    if not start_time or not end_time:
        return None, "Please provide a valid start and end time."
    if end_time <= start_time:
        return None, "End time must be after the start time."
    if year not in YEAR_SEMESTER_MAP:
        return None, "Please choose a valid year (2nd, 3rd, or 4th year)."
    if semester not in YEAR_SEMESTER_MAP[year]:
        return None, f"{semester or 'That semester'} isn't valid for {year}. Choose one of: {', '.join(YEAR_SEMESTER_MAP[year])}."

    fields = {
        "day": day, "start_time": start_time, "end_time": end_time,
        "year": year, "semester": semester, "subject_code": subject_code,
        "subject_title": subject_title, "teacher_name": teacher_name,
    }
    return fields, None


def _normalize_code(value):
    raw = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    m = re.search(r"[A-Z]{2,6}\d{3,4}[A-Z]*", raw)
    return m.group(0) if m else ""


def _parse_time_value(value):
    if value is None:
        return None
    if hasattr(value, "hour") and hasattr(value, "minute"):
        return value.replace(second=0, microsecond=0)
    text = str(value).strip().upper().replace("–", "-").replace("—", "-")
    text = text.replace(".", ":")
    for fmt in ("%H:%M", "%I:%M %p", "%I %p", "%H%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    return None


def _parse_time_range(text):
    text = str(text or "").strip().upper().replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text)
    m = re.search(r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM)?)\s*-\s*(\d{1,2}(?::\d{2})?\s*(?:AM|PM)?)", text)
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    # If AM/PM appears only on the end, use it for both values.
    suffix = re.search(r"(AM|PM)$", b)
    if suffix and not re.search(r"(AM|PM)$", a):
        a = a + " " + suffix.group(1)
    return _parse_time_value(a), _parse_time_value(b)


def _find_code(text):
    return _normalize_code(text)


def _extract_subject_mapping(text):
    """Best-effort subject-code -> (title, faculty) mapping from the lower
    subject table commonly attached to college timetable PDFs/images."""
    mapping = {}
    lines = [re.sub(r"\s+", " ", x).strip() for x in (text or "").splitlines() if x.strip()]
    known = []
    for line in lines:
        code = _find_code(line)
        if code:
            known.append((code, line))
    for i, (code, line) in enumerate(known):
        remainder = re.sub(re.escape(code), " ", line, flags=re.I).strip(" -|:")
        faculty = ""
        fm = re.search(r"(?:Prof\.?|Dr\.?|Mr\.?|Ms\.?)\s*[A-Za-z][A-Za-z .'-]{2,}", line, flags=re.I)
        if fm:
            faculty = fm.group(0).strip()
        title = remainder
        # If the current OCR line only contains the code, inspect nearby lines.
        if not title or title.upper() == code:
            nearby = lines[i + 1:i + 4]
            for n in nearby:
                if not _find_code(n):
                    if re.search(r"(?:Prof\.?|Dr\.?)", n, re.I):
                        faculty = n.strip()
                    elif not title:
                        title = n.strip()
        if faculty:
            faculty = re.sub(r"^\s*[:|-]\s*", "", faculty)
        mapping[code] = {"title": title, "faculty": faculty}
    return mapping


def _match_code(token, known_codes):
    code = _normalize_code(token)
    if code in known_codes:
        return code
    if not code or not known_codes:
        return code
    match = difflib.get_close_matches(code, list(known_codes), n=1, cutoff=0.68)
    return match[0] if match else code


def _parse_excel_timetable(data, filename, year, semester):
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext not in {"csv"} and not OPENPYXL_AVAILABLE:
        raise OcrError("Excel import needs openpyxl. Install the requirements and try again.")
    if ext == "xls":
        raise OcrError("Legacy .xls files are not supported yet. Please save the Excel file as .xlsx and upload it again.")
    if ext == "csv":
        import csv
        text = data.decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
    else:
        import tempfile
    if ext != "csv":
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp.write(data)
            path = tmp.name
        try:
            wb = openpyxl.load_workbook(path, data_only=True)
            ws = wb.active
            rows = [[cell.value for cell in row] for row in ws.iter_rows()]
        finally:
            try: os.unlink(path)
            except OSError: pass

    # Direct tabular format: Day | Start | End | Subject | Faculty.
    direct = []
    for row in rows:
        vals = [str(v).strip() if v is not None else "" for v in row]
        if len(vals) < 4:
            continue
        low = [v.lower() for v in vals]
        if any(v == "day" for v in low) and any("start" in v for v in low) and any("end" in v for v in low):
            continue
        day = next((v for v in vals if v.title() in WEEKDAYS), "")
        times = [v for v in vals if _parse_time_range(v)]
        start = _parse_time_value(vals[1]) if len(vals) > 1 else None
        end = _parse_time_value(vals[2]) if len(vals) > 2 else None
        if day and start and end:
            code = _find_code(next((v for v in vals[3:] if _find_code(v)), ""))
            faculty = next((v for v in reversed(vals[3:]) if re.search(r"(?:Prof\.?|Dr\.?)", v, re.I)), "")
            title = vals[4] if len(vals) > 4 and vals[4] != faculty else ""
            direct.append({"day": day, "start_time": start, "end_time": end, "subject_code": code, "subject_title": title, "teacher_name": faculty})
    if direct:
        return direct

    # Matrix format: find the row containing several time ranges, then rows
    # below it containing weekday names. Subject/faculty mapping is read from
    # any lower table in the workbook.
    time_headers = []
    header_row_idx = None
    for ri, row in enumerate(rows):
        found = []
        for ci, value in enumerate(row):
            tr = _parse_time_range(value)
            if tr:
                found.append((ci, tr))
        if len(found) >= 2:
            header_row_idx = ri
            time_headers = found
            break
    if header_row_idx is None:
        raise OcrError("I could not find timetable time columns in the Excel file. Use the usual Day/Time timetable layout or the simple Day/Start/End/Subject/Faculty format.")

    mapping = {}
    for row in rows[header_row_idx + 1:]:
        vals = [str(v).strip() if v is not None else "" for v in row]
        code = next((_find_code(v) for v in vals if _find_code(v)), "")
        if code:
            faculty = next((v for v in reversed(vals) if re.search(r"(?:Prof\.?|Dr\.?)", v, re.I)), "")
            title = ""
            if code:
                code_idx = next((i for i,v in enumerate(vals) if _find_code(v)), 0)
                for v in vals[code_idx+1:]:
                    if v and v != faculty and not _find_code(v):
                        title = v
                        break
            mapping[code] = {"title": title, "faculty": faculty}

    entries = []
    for ri in range(header_row_idx + 1, len(rows)):
        vals = [str(v).strip() if v is not None else "" for v in rows[ri]]
        day = next((v.title() for v in vals[:3] if v.title() in WEEKDAYS), "")
        if not day:
            continue
        for ci, (start, end) in time_headers:
            if ci >= len(vals):
                continue
            cell = vals[ci]
            code = _match_code(cell, mapping.keys())
            if not code or cell.lower() in {"break", "lunch", "tutorial", "open elective", "no class"}:
                continue
            info = mapping.get(code, {})
            entries.append({"day": day, "start_time": start, "end_time": end, "subject_code": code, "subject_title": info.get("title", ""), "teacher_name": info.get("faculty", "")})
    return entries


def _parse_image_timetable(data, filename, year, semester):
    if not PYTESSERACT_AVAILABLE:
        raise OcrError("Timetable OCR is not available on this server.")
    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
        # Keep enough resolution for small table text while normalizing phone photos.
        gray = ImageOps.autocontrast(ImageOps.grayscale(image))
        scale = 2 if max(gray.size) < 2400 else 1
        if scale > 1:
            gray = gray.resize((gray.width * scale, gray.height * scale))
        text = pytesseract.image_to_string(gray, lang=OCR_LANGUAGES or "eng", config="--psm 11")
        mapping = _extract_subject_mapping(text)
        data_dict = pytesseract.image_to_data(gray, lang=OCR_LANGUAGES or "eng", config="--psm 11", output_type=TesseractOutput.DICT)
    except pytesseract.TesseractNotFoundError:
        raise OcrError("Tesseract executable not found. Set TESSERACT_CMD on the server.")
    except Exception as exc:
        raise OcrError(f"Could not read the timetable image: {exc}")

    tokens = []
    for i, raw in enumerate(data_dict.get("text", [])):
        t = str(raw or "").strip()
        try: conf = float(data_dict["conf"][i])
        except Exception: conf = 0
        if t and conf >= 15:
            tokens.append({"text": t, "x": data_dict["left"][i], "y": data_dict["top"][i], "w": data_dict["width"][i], "h": data_dict["height"][i], "conf": conf})

    # Find the timetable header by collecting time tokens in the upper half.
    time_points = []
    for tok in tokens:
        tr = _parse_time_range(tok["text"])
        if tr:
            time_points.append((tok["x"] + tok["w"] / 2, tok["y"], *tr))
    # OCR usually separates start/end values; pair nearby tokens on the same line.
    simple_times = []
    for tok in tokens:
        tm = _parse_time_value(tok["text"])
        if tm and tok["y"] < gray.height * 0.55:
            simple_times.append((tok["x"] + tok["w"] / 2, tok["y"], tm))
    simple_times.sort(key=lambda z: (z[1], z[0]))
    header_candidates = []
    for idx in range(len(simple_times)-1):
        a, b = simple_times[idx], simple_times[idx+1]
        if abs(a[1]-b[1]) < max(20, gray.height*0.025) and b[0] > a[0] + 10:
            header_candidates.append((a[0], b[0], a[2], b[2], (a[1]+b[1])/2))
    # Deduplicate by x and retain the most likely header row.
    slots = []
    for c in header_candidates:
        if not any(abs(c[0]-s[0]) < 25 for s in slots):
            slots.append(c)
    slots.sort(key=lambda z: z[0])
    if len(slots) < 2:
        raise OcrError("I could not detect the timetable time columns. Please upload a clearer PDF/photo or use Excel/manual entry.")

    day_tokens = []
    for tok in tokens:
        day = tok["text"].strip().title()
        if day in WEEKDAYS and tok["y"] > min(s[4] for s in slots):
            day_tokens.append((day, tok["x"], tok["y"], tok["h"]))
    day_tokens.sort(key=lambda z: z[2])
    if not day_tokens:
        raise OcrError("I could not detect Monday–Saturday rows in the timetable. Please upload a clearer file or use manual entry.")

    # Match subject-code-like OCR tokens to the nearest time column and day row.
    known_codes = set(mapping.keys())
    candidates = []
    for tok in tokens:
        code = _match_code(tok["text"], known_codes)
        if not code or not re.search(r"\d", code):
            continue
        cy = tok["y"] + tok["h"] / 2
        cx = tok["x"] + tok["w"] / 2
        if cy <= min(d[2] for d in day_tokens) - 5:
            continue
        day = min(day_tokens, key=lambda d: abs((d[2]+d[3]/2) - cy))[0]
        slot = min(slots, key=lambda s: abs(((s[0]+s[1])/2) - cx))
        # Avoid accidentally reading the bottom subject mapping as a class cell.
        if abs((day_tokens[[d[0] for d in day_tokens].index(day)][2]) - cy) > gray.height * 0.12:
            continue
        info = mapping.get(code, {})
        candidates.append({"day": day, "start_time": slot[2], "end_time": slot[3], "subject_code": code, "subject_title": info.get("title", ""), "teacher_name": info.get("faculty", "")})

    # Deduplicate same day/time/code entries.
    unique = {}
    for e in candidates:
        key = (e["day"], e["start_time"], e["end_time"], e["subject_code"])
        unique[key] = e
    entries = list(unique.values())
    if not entries:
        raise OcrError("OCR found the timetable structure but could not confidently extract class cells. Please use Excel/manual entry or a clearer image.")
    return entries, text


def _parse_timetable_upload(file_storage, year, semester):
    filename = secure_filename(file_storage.filename or "")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in TIMETABLE_UPLOAD_EXTENSIONS:
        raise OcrError("Unsupported timetable file. Upload Excel (.xlsx), PDF, JPG, PNG, or WEBP.")
    data = file_storage.read()
    if not data:
        raise OcrError("The uploaded timetable file is empty.")
    if ext in {"xlsx", "xls", "csv"}:
        return _parse_excel_timetable(data, filename, year, semester), False
    if ext == "pdf":
        if not PYMUPDF_AVAILABLE:
            raise OcrError("PDF timetable import needs PyMuPDF.")
        try:
            doc = fitz.open(stream=data, filetype="pdf")
            if doc.is_encrypted:
                raise OcrError("The PDF is encrypted/password-protected.")
            if doc.page_count > OCR_MAX_PDF_PAGES:
                raise OcrError(f"PDF has too many pages (max {OCR_MAX_PDF_PAGES}).")
            # Prefer embedded text for mapping, but render the page for spatial timetable extraction.
            page = doc[0]
            pix = page.get_pixmap(dpi=220)
            img_data = pix.tobytes("png")
            doc.close()
            entries, ocr_text = _parse_image_timetable(img_data, filename, year, semester)
            return entries, True
        except OcrError:
            raise
        except Exception as exc:
            raise OcrError(f"Could not read the PDF timetable: {exc}")
    entries, _ = _parse_image_timetable(data, filename, year, semester)
    return entries, True


def _publish_semester(year, semester):
    # Only one semester can be visible for each year, preventing repeated years
    # on the public display. Other semesters remain stored for later switching.
    TimetableEntry.query.filter_by(year=year).update({"published": False}, synchronize_session=False)
    TimetableEntry.query.filter_by(year=year, semester=semester).update({"published": True}, synchronize_session=False)


def _replace_semester_entries(year, semester, entries, source_filename=None):
    TimetableEntry.query.filter_by(year=year, semester=semester).delete(synchronize_session=False)
    for e in entries:
        db.session.add(TimetableEntry(
            year=year, semester=semester, day=e["day"], start_time=e["start_time"], end_time=e["end_time"],
            subject_code=e.get("subject_code") or None, subject_title=e.get("subject_title") or None,
            teacher_name=e.get("teacher_name") or None, published=True, source_filename=source_filename,
        ))
    _publish_semester(year, semester)


@app.route("/timetable")
@login_required
@admin_required
def timetable_list():
    # Manual semester matrix: only the selected year + semester is shown.
    # Entries remain saved until the admin edits/deletes them. The public
    # display separately uses the saved entries to determine the current class.
    selected_year = request.args.get("year", "").strip()
    if selected_year not in TIMETABLE_YEARS:
        selected_year = TIMETABLE_YEARS[0]
    selected_semester = request.args.get("semester", "").strip()
    if selected_semester not in YEAR_SEMESTER_MAP[selected_year]:
        selected_semester = YEAR_SEMESTER_MAP[selected_year][0]

    entries = TimetableEntry.query.all()
    entries.sort(key=lambda e: (TIMETABLE_YEARS.index(e.year) if e.year in TIMETABLE_YEARS else 99,
                                YEAR_SEMESTER_MAP.get(e.year, []).index(e.semester) if e.semester in YEAR_SEMESTER_MAP.get(e.year, []) else 99,
                                WEEKDAYS.index(e.day) if e.day in WEEKDAYS else 99, e.start_time))
    sem_entries = [e for e in entries if e.year == selected_year and e.semester == selected_semester]
    # Fixed timetable columns matching the department's uploaded timetable.
    default_slots = [
        (datetime.strptime("09:00", "%H:%M").time(), datetime.strptime("10:00", "%H:%M").time()),
        (datetime.strptime("10:00", "%H:%M").time(), datetime.strptime("11:00", "%H:%M").time()),
        (datetime.strptime("11:00", "%H:%M").time(), datetime.strptime("11:15", "%H:%M").time()),
        (datetime.strptime("11:15", "%H:%M").time(), datetime.strptime("12:15", "%H:%M").time()),
        (datetime.strptime("12:15", "%H:%M").time(), datetime.strptime("13:15", "%H:%M").time()),
        (datetime.strptime("13:15", "%H:%M").time(), datetime.strptime("14:00", "%H:%M").time()),
        (datetime.strptime("14:00", "%H:%M").time(), datetime.strptime("15:00", "%H:%M").time()),
        (datetime.strptime("15:00", "%H:%M").time(), datetime.strptime("16:00", "%H:%M").time()),
    ]
    time_slots = sorted(set(default_slots) | {(e.start_time, e.end_time) for e in sem_entries})
    matrix = []
    for day in WEEKDAYS:
        day_cells = []
        for start, end in time_slots:
            matches = [e for e in sem_entries if e.day == day and e.start_time == start and e.end_time == end]
            day_cells.append(((start, end), matches))
        matrix.append((day, day_cells))
    selected_matrix = {
        "year": selected_year, "semester": selected_semester, "entries": sem_entries,
        "time_slots": time_slots, "matrix": matrix,
        "published": any(e.published for e in sem_entries),
    }
    return render_template("timetable.html", entries=entries, weekdays=WEEKDAYS,
                           year_semester_map=YEAR_SEMESTER_MAP, years=TIMETABLE_YEARS,
                           selected_year=selected_year, selected_semester=selected_semester,
                           selected_matrix=selected_matrix, now=datetime.now())


@app.route("/timetable/save-matrix", methods=["POST"])
@login_required
@admin_required
def save_timetable_matrix():
    """Save the simple fixed-hour semester matrix in one operation."""
    year = request.form.get("year", "").strip()
    semester = request.form.get("semester", "").strip()
    raw = request.form.get("matrix_json", "")
    if year not in YEAR_SEMESTER_MAP or semester not in YEAR_SEMESTER_MAP[year]:
        flash("Please choose a valid year and semester.", "error")
        return redirect(url_for("timetable_list"))
    try:
        matrix = json.loads(raw)
        if not isinstance(matrix, list):
            raise ValueError("Invalid timetable matrix.")
        # Fixed 8-column timetable matching the department timetable.
        fixed_slots = [
            (datetime.strptime("09:00", "%H:%M").time(), datetime.strptime("10:00", "%H:%M").time()),
            (datetime.strptime("10:00", "%H:%M").time(), datetime.strptime("11:00", "%H:%M").time()),
            (datetime.strptime("11:00", "%H:%M").time(), datetime.strptime("11:15", "%H:%M").time()),
            (datetime.strptime("11:15", "%H:%M").time(), datetime.strptime("12:15", "%H:%M").time()),
            (datetime.strptime("12:15", "%H:%M").time(), datetime.strptime("13:15", "%H:%M").time()),
            (datetime.strptime("13:15", "%H:%M").time(), datetime.strptime("14:00", "%H:%M").time()),
            (datetime.strptime("14:00", "%H:%M").time(), datetime.strptime("15:00", "%H:%M").time()),
            (datetime.strptime("15:00", "%H:%M").time(), datetime.strptime("16:00", "%H:%M").time()),
        ]
        rows = []
        for r in matrix:
            if not isinstance(r, dict):
                continue
            day = str(r.get("day", "")).strip().title()
            if day not in WEEKDAYS:
                continue
            cells = r.get("cells", [])
            for idx, value in enumerate(cells[:len(fixed_slots)]):
                text = str(value or "").strip()
                if not text or text.lower() in {"-", "—", "no class", "none"}:
                    continue
                # Allow a whole cell such as "BEE701 - Prof. Rao" or
                # "BEE701\nProf. Rao". If no code is present, the whole cell
                # is treated as the faculty/class text.
                lines = [x.strip() for x in re.split(r"\\n|\n", text) if x.strip()]
                flat = " ".join(lines)
                code = _normalize_code(flat)
                faculty = flat
                if code:
                    faculty = re.sub(re.escape(code), "", flat, count=1, flags=re.I)
                    faculty = re.sub(r"^[\s:–—-]+|[\s:–—-]+$", "", faculty).strip()
                    faculty = faculty or "Faculty not entered"
                start, end = fixed_slots[idx]
                rows.append({"day": day, "start_time": start, "end_time": end,
                             "subject_code": code, "subject_title": "", "teacher_name": faculty})
        _replace_semester_entries(year, semester, rows)
        db.session.commit()
        flash(f"{year} — {semester} timetable saved. It will remain until you change or remove it.", "success")
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        db.session.rollback()
        flash(f"Could not save timetable: {exc}", "error")
    except Exception as exc:
        db.session.rollback()
        flash(f"Could not save timetable: {exc}", "error")
    return redirect(url_for("timetable_list", year=year, semester=semester))


@app.route("/timetable/reset", methods=["POST"])
@login_required
@admin_required
def reset_timetable_matrix():
    """Full reset: clear every saved timetable entry for every year and
    semester, so the timetable is completely empty and ready for a new
    intake. This does NOT touch the User/Settings tables, notices, uploads,
    or any other data — only rows in TimetableEntry are deleted."""
    year = request.form.get("year", "").strip()
    semester = request.form.get("semester", "").strip()
    if year not in YEAR_SEMESTER_MAP:
        year = TIMETABLE_YEARS[0]
    if semester not in YEAR_SEMESTER_MAP.get(year, []):
        semester = YEAR_SEMESTER_MAP[year][0]
    try:
        TimetableEntry.query.delete(synchronize_session=False)
        db.session.commit()
        flash("The timetable has been reset. Every year and semester is now empty and ready for a new intake.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Could not reset timetable: {exc}", "error")
    return redirect(url_for("timetable_list", year=year, semester=semester))


@app.route("/timetable/add", methods=["GET", "POST"])
@login_required
@admin_required
def add_timetable():
    if request.method == "POST":
        fields, error = validate_timetable_entry_form(request.form)
        if error:
            flash(error, "error")
            return redirect(url_for("add_timetable"))
        publish = request.form.get("publish") == "1"
        db.session.add(TimetableEntry(**fields, published=publish))
        if publish:
            _publish_semester(fields["year"], fields["semester"])
        db.session.commit()
        flash("Timetable entry added.", "success")
        return redirect(url_for("timetable_list"))
    prefill_year = request.args.get("year", "").strip()
    prefill_semester = request.args.get("semester", "").strip()
    prefill_day = request.args.get("day", "").strip()
    prefill_start = request.args.get("start_time", "").strip()
    prefill_end = request.args.get("end_time", "").strip()
    return render_template("add_timetable.html", entry=None, weekdays=WEEKDAYS,
                           year_semester_map=YEAR_SEMESTER_MAP, years=TIMETABLE_YEARS,
                           prefill_year=prefill_year, prefill_semester=prefill_semester,
                           prefill_day=prefill_day, prefill_start=prefill_start, prefill_end=prefill_end)


@app.route("/timetable/edit/<int:entry_id>", methods=["GET", "POST"])
@login_required
@admin_required
def edit_timetable(entry_id):
    entry = TimetableEntry.query.get_or_404(entry_id)
    if request.method == "POST":
        fields, error = validate_timetable_entry_form(request.form)
        if error:
            flash(error, "error")
            return redirect(url_for("edit_timetable", entry_id=entry.id))
        for key, value in fields.items():
            setattr(entry, key, value)
        entry.published = request.form.get("publish") == "1"
        if entry.published:
            _publish_semester(entry.year, entry.semester)
        db.session.commit()
        flash("Timetable entry updated.", "success")
        return redirect(url_for("timetable_list"))
    return render_template("add_timetable.html", entry=entry, weekdays=WEEKDAYS,
                           year_semester_map=YEAR_SEMESTER_MAP, years=TIMETABLE_YEARS)


@app.route("/timetable/publish/<year>/<semester>", methods=["POST"])
@login_required
@admin_required
def publish_timetable(year, semester):
    if year not in YEAR_SEMESTER_MAP or semester not in YEAR_SEMESTER_MAP[year]:
        abort(404)
    if not TimetableEntry.query.filter_by(year=year, semester=semester).first():
        flash("That semester has no timetable entries to publish.", "error")
        return redirect(url_for("timetable_list"))
    _publish_semester(year, semester)
    db.session.commit()
    flash(f"{year} — {semester} is now active on the noticeboard.", "success")
    return redirect(url_for("timetable_list"))


@app.route("/timetable/upload-preview", methods=["POST"])
@login_required
@admin_required
def upload_timetable_preview():
    """Parse an uploaded timetable into draft rows only. Nothing is written to
    the live timetable until the admin reviews the preview and clicks Publish."""
    year = request.form.get("year", "").strip()
    semester = request.form.get("semester", "").strip()
    file_storage = request.files.get("timetable_file")
    if year not in YEAR_SEMESTER_MAP or semester not in YEAR_SEMESTER_MAP[year]:
        flash("Please choose a valid year and semester.", "error")
        return redirect(url_for("timetable_list"))
    if not file_storage or not file_storage.filename:
        flash("Please choose an Excel, PDF, or timetable photo.", "error")
        return redirect(url_for("timetable_list"))
    try:
        entries, used_ocr = _parse_timetable_upload(file_storage, year, semester)
        cleaned = []
        for e in entries:
            if e["day"] not in WEEKDAYS or not e["start_time"] or not e["end_time"] or e["end_time"] <= e["start_time"]:
                continue
            cleaned.append(e)
        if not cleaned:
            raise OcrError("No valid class periods were extracted from the timetable.")
        preview_entries = []
        for e in cleaned:
            preview_entries.append({
                "day": e["day"],
                "start_time": e["start_time"].strftime("%H:%M"),
                "end_time": e["end_time"].strftime("%H:%M"),
                "subject_code": e.get("subject_code") or "",
                "subject_title": e.get("subject_title") or "",
                "teacher_name": e.get("teacher_name") or "",
            })
        return render_template(
            "timetable_preview.html",
            year=year, semester=semester, entries=preview_entries,
            source_filename=secure_filename(file_storage.filename),
            used_ocr=used_ocr, weekdays=WEEKDAYS,
        )
    except OcrError as exc:
        flash(str(exc), "error")
    except Exception as exc:
        flash(f"Timetable import failed: {exc}", "error")
    return redirect(url_for("timetable_list"))


@app.route("/timetable/publish-preview", methods=["POST"])
@login_required
@admin_required
def publish_timetable_preview():
    """Validate the reviewed preview rows, then replace and publish the
    selected semester atomically. The old timetable is untouched if validation
    fails."""
    year = request.form.get("year", "").strip()
    semester = request.form.get("semester", "").strip()
    source_filename = request.form.get("source_filename", "").strip() or None
    raw_entries = request.form.get("entries_json", "")
    if year not in YEAR_SEMESTER_MAP or semester not in YEAR_SEMESTER_MAP[year]:
        flash("Please choose a valid year and semester.", "error")
        return redirect(url_for("timetable_list"))
    try:
        entries = json.loads(raw_entries)
        if not isinstance(entries, list) or not entries:
            raise ValueError("No timetable rows were provided.")
        cleaned = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            start = _parse_time_value(e.get("start_time"))
            end = _parse_time_value(e.get("end_time"))
            day = str(e.get("day", "")).strip().title()
            if day not in WEEKDAYS or not start or not end or end <= start:
                continue
            cleaned.append({
                "day": day, "start_time": start, "end_time": end,
                "subject_code": str(e.get("subject_code", "")).strip(),
                "subject_title": str(e.get("subject_title", "")).strip(),
                "teacher_name": str(e.get("teacher_name", "")).strip(),
            })
        if not cleaned:
            raise ValueError("The preview contains no valid class periods.")
        _replace_semester_entries(year, semester, cleaned, source_filename)
        db.session.commit()
        source = "OCR" if request.form.get("used_ocr") == "1" else "Excel"
        flash(f"{year} — {semester} timetable published with {len(cleaned)} class periods ({source}). The previous timetable was replaced.", "success")
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        db.session.rollback()
        flash(f"Preview validation failed: {exc}", "error")
    except Exception as exc:
        db.session.rollback()
        flash(f"Could not publish timetable: {exc}", "error")
    return redirect(url_for("timetable_list"))


@app.route("/timetable/upload", methods=["POST"])
@login_required
@admin_required
def upload_timetable():
    """Backward-compatible alias: all uploads now go through the preview."""
    return upload_timetable_preview()


@app.route("/timetable/delete/<int:entry_id>", methods=["POST"])
@login_required
@admin_required
def delete_timetable(entry_id):
    entry = TimetableEntry.query.get_or_404(entry_id)
    db.session.delete(entry)
    db.session.commit()
    flash("Timetable entry deleted.", "success")
    return redirect(url_for("timetable_list"))


# --------------------------------------------------------------------------
# Board settings (admin only) — the board title shown on the admin portal and
# the public display screen
# --------------------------------------------------------------------------
@app.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def board_settings():
    settings = get_settings()

    if request.method == "POST":
        title = request.form.get("board_title", "").strip()
        if title:
            settings.board_title = title
        db.session.commit()
        flash("Board settings updated.", "success")
        return redirect(url_for("board_settings"))

    return render_template(
        "settings.html", settings=settings,
        tesseract_cmd=TESSERACT_CMD, ocr_languages=OCR_LANGUAGES,
    )


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not current_user.check_password(current_password):
            flash("Current password is incorrect.", "error")
        elif len(new_password) < 6:
            flash("New password must be at least 6 characters.", "error")
        elif new_password != confirm_password:
            flash("New passwords do not match.", "error")
        else:
            current_user.set_password(new_password)
            db.session.commit()
            flash("Password updated successfully.", "success")
            return redirect(url_for("dashboard"))

    return render_template("change_password.html")


# --------------------------------------------------------------------------
# Public display routes (for the HDMI screen — no login required)
# --------------------------------------------------------------------------
@app.route("/display")
def display():
    return render_template("display.html", categories=CATEGORIES)


def _notices_payload():
    """The live rotation/ticker data for the public display. This is the
    single source of truth used by /api/notices AND by the SSE change-
    detector below, so the two can never drift out of sync with each other.
    """
    notices = (
        Notice.query.filter_by(is_active=True)
        .order_by(Notice.priority.desc(), Notice.created_at.desc())
        .all()
    )
    live = [n for n in notices if not n.is_expired()]

    rotation = [n.to_display_dict() for n in live if n.category != "important"]
    ticker = [n.to_display_dict() for n in live if n.category == "important"]

    return {"rotation": rotation, "ticker": ticker}


@app.route("/api/notices")
def api_notices():
    payload = _notices_payload()
    payload["server_time"] = datetime.now().strftime("%A, %d %B %Y  |  %I:%M %p")
    return jsonify(payload)


def _timetable_payload():
    """Return one row per year. The admin publishes exactly one semester for
    each year; only the currently ongoing period is considered a lecture.
    Single source of truth for /api/timetable/current AND the SSE
    change-detector below.
    """
    now = now_local()
    today = now.strftime("%A")
    published = TimetableEntry.query.filter_by(day=today, published=True).all()
    current = [e for e in published if e.is_current(now)]

    # Choose the configured active semester for each year. Because publishing
    # is exclusive per year, there can never be duplicate year rows.
    by_year = {}
    for e in published:
        by_year.setdefault(e.year, e.semester)
    ordered_years = TIMETABLE_YEARS

    if not current:
        return {"active": False, "entries": [], "day": today}

    current_by_year = {e.year: e for e in current}
    time_ranges = sorted({e.time_range_label() for e in current})
    common_time = time_ranges[0] if time_ranges else ""
    # If all active classes use the same slot, use that common range. If a
    # timetable contains overlapping slots, show the earliest current slot.
    entries = []
    for year in ordered_years:
        sem = by_year.get(year)
        e = current_by_year.get(year)
        entries.append({
            "year": year,
            "semester": sem or "",
            "teacher_name": e.teacher_name if e and e.has_teacher() else "",
            "subject_code": e.subject_code if e else "",
            "subject_title": e.subject_title if e else "",
            "no_class": not bool(e and e.has_teacher()),
        })
    return {"active": True, "day": today, "time_range": common_time, "entries": entries}


@app.route("/api/timetable/current")
def api_timetable_current():
    return jsonify(_timetable_payload())


# --------------------------------------------------------------------------
# Live sync (Server-Sent Events) — one common channel for the whole public
# Display (notices AND timetable). The database is the only source of
# truth: every ~1.5s this endpoint re-reads the exact same data the
# /api/notices and /api/timetable/current routes serve, hashes it, and — the
# moment that hash changes (a notice posted/edited/deleted, a timetable
# added/edited/deleted/reset, or the current lecture period rolling over) —
# pushes a tiny "update" event down the already-open connection. The display
# page (display.js) reacts by re-fetching those same two endpoints
# immediately, no manual refresh/reopen/restart required. This deliberately
# does NOT hook into every individual admin route — it just watches the
# database — so posting/deleting/editing notices or timetable entries from
# any admin route (present or future) is picked up automatically.
# --------------------------------------------------------------------------
SSE_POLL_SECONDS = 3            # how often the stream re-checks the database (kept modest for low-resource hosting like Render's free tier)
SSE_HEARTBEAT_SECONDS = 15      # comment ping so idle proxies don't drop the connection
SSE_MAX_CONNECTION_SECONDS = 3600  # recycle the stream hourly; EventSource auto-reconnects


def _display_signature():
    """A short fingerprint of everything the public display currently shows.
    Changes if and only if the rendered display would change."""
    payload = {"notices": _notices_payload(), "timetable": _timetable_payload()}
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@app.route("/api/events")
def api_events():
    def event_stream():
        idle_seconds = 0.0
        started = time.monotonic()
        # Tell the browser's EventSource how long to wait before
        # auto-reconnecting if this stream ever drops.
        yield "retry: 3000\n\n"
        # Baseline the signature at connect time so a fresh/reconnected
        # stream doesn't immediately fire a spurious "update" for content
        # the client already has — only real changes push after this.
        try:
            last_sig = _display_signature()
        except Exception:
            last_sig = None
        finally:
            db.session.remove()
        while time.monotonic() - started < SSE_MAX_CONNECTION_SECONDS:
            try:
                sig = _display_signature()
            except Exception:
                sig = None
            finally:
                # Release the DB connection back to the pool between checks
                # instead of holding it for the whole lifetime of the stream.
                db.session.remove()

            if sig is not None and sig != last_sig:
                last_sig = sig
                yield f"event: update\ndata: {sig}\n\n"
                idle_seconds = 0.0
            else:
                idle_seconds += SSE_POLL_SECONDS
                if idle_seconds >= SSE_HEARTBEAT_SECONDS:
                    yield ": heartbeat\n\n"
                    idle_seconds = 0.0

            time.sleep(SSE_POLL_SECONDS)
        # Stream recycled — the client's EventSource reconnects automatically.

    response = Response(stream_with_context(event_stream()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Connection"] = "keep-alive"
    # Disable response buffering on proxies in front of the app (Render's
    # router included) so events are flushed to the client immediately
    # instead of being held until the buffer fills.
    response.headers["X-Accel-Buffering"] = "no"
    return response


# --------------------------------------------------------------------------
# CLI / bootstrap
# --------------------------------------------------------------------------
@app.cli.command("init-db")
def init_db():
    """Create tables and a default admin account."""
    db.create_all()
    migrate_schema()
    if not User.query.filter_by(username="admin").first():
        admin = User(username="admin", name="Administrator", role="admin")
        admin.set_password("admin123")
        db.session.add(admin)
        db.session.commit()
        print("Default admin created -> username: admin | password: admin123")
    else:
        print("Database already initialized.")
    get_settings()


def _add_column_if_missing(conn, table, column, ddl):
    existing_cols = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
    if column not in existing_cols:
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        conn.commit()


def migrate_schema():
    """Add any newly-introduced columns to an existing SQLite database,
    without touching existing data. Safe to run every time the app starts.
    """
    with db.engine.connect() as conn:
        _add_column_if_missing(conn, "notice", "popup_duration_seconds",
                                f"popup_duration_seconds INTEGER DEFAULT {DEFAULT_POPUP_DURATION}")
        _add_column_if_missing(conn, "notice", "show_caption", "show_caption BOOLEAN DEFAULT 1")
        _add_column_if_missing(conn, "notice", "ocr_used", "ocr_used BOOLEAN DEFAULT 0")
        _add_column_if_missing(conn, "notice", "ocr_source_filename", "ocr_source_filename VARCHAR(255)")
        _add_column_if_missing(conn, "notice", "ocr_extracted_at", "ocr_extracted_at DATETIME")
        _add_column_if_missing(conn, "notice", "video_filename", "video_filename VARCHAR(255)")
        _add_column_if_missing(conn, "timetable_entry", "subject_code", "subject_code VARCHAR(40)")
        _add_column_if_missing(conn, "timetable_entry", "subject_title", "subject_title VARCHAR(200)")
        _add_column_if_missing(conn, "timetable_entry", "published", "published BOOLEAN DEFAULT 1")
        _add_column_if_missing(conn, "timetable_entry", "source_filename", "source_filename VARCHAR(255)")
        conn.commit()

        # Normalize old data so only one semester per year can be active on
        # the public display. Keep the most recently updated semester active.
        for _year in TIMETABLE_YEARS:
            _rows = TimetableEntry.query.filter_by(year=_year, published=True).order_by(TimetableEntry.updated_at.desc()).all()
            if len({r.semester for r in _rows}) > 1:
                _keep = _rows[0].semester
                TimetableEntry.query.filter_by(year=_year).update({"published": False}, synchronize_session=False)
                TimetableEntry.query.filter_by(year=_year, semester=_keep).update({"published": True}, synchronize_session=False)
        db.session.commit()

        # If this database was previously running a version with a separate
        # notice_media table (per-photo/video rows), pull that data back
        # into the simpler `images` / `video_filename` columns so nothing
        # posted under that version is lost.
        table_names = {row[0] for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "notice_media" in table_names:
            media_rows = conn.exec_driver_sql(
                "SELECT notice_id, filename, media_type, display_order FROM notice_media "
                "ORDER BY notice_id, display_order"
            ).fetchall()
            by_notice = {}
            for notice_id, filename, media_type, display_order in media_rows:
                by_notice.setdefault(notice_id, {"image": [], "video": []})
                bucket = "image" if media_type != "video" else "video"
                by_notice[notice_id][bucket].append(filename)

            for notice_id, media in by_notice.items():
                notice = Notice.query.get(notice_id)
                if notice is None:
                    continue
                if media["image"] and not notice.images:
                    notice.images = json.dumps(media["image"])
                if media["video"] and not notice.video_filename:
                    notice.video_filename = media["video"][0]
            if by_notice:
                db.session.commit()

    # One-time cleanup for notices created before slideshow was removed and
    # before grid was capped at 2 photos, so old data keeps displaying
    # correctly under the simplified display types instead of breaking.
    legacy_slideshows = Notice.query.filter_by(display_type="slideshow").all()
    for notice in legacy_slideshows:
        notice.display_type = "image"
        images = notice.image_list()
        if len(images) > 1:
            notice.images = json.dumps(images[:1])  # keep just the first photo
    oversized_grids = [
        n for n in Notice.query.filter_by(display_type="grid").all()
        if len(n.image_list()) > GRID_PHOTO_COUNT
    ]
    for notice in oversized_grids:
        notice.images = json.dumps(notice.image_list()[:GRID_PHOTO_COUNT])
    if legacy_slideshows or oversized_grids:
        db.session.commit()


def bootstrap():
    """Ensure DB + default admin + default settings exist without running flask cli."""
    with app.app_context():
        db.create_all()
        migrate_schema()
        if not User.query.filter_by(username="admin").first():
            admin = User(username="admin", name="Administrator", role="admin")
            admin.set_password("admin123")
            db.session.add(admin)
            db.session.commit()
        get_settings()


if __name__ == "__main__":
    bootstrap()
    app.run(debug=True, host="0.0.0.0", port=5000)
