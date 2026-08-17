import os
import json
from datetime import datetime, date
from functools import wraps

from flask import (
    Flask, render_template, redirect, url_for, request,
    flash, jsonify, abort
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
BRANDING_FOLDER = os.path.join(BASE_DIR, "static", "branding")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
DEFAULT_BOARD_TITLE = "KLS VDIT EEE DEPARTMENT SMART NOTICE BOARD"
GRID_PHOTO_COUNT = 2     # grid mode is fixed at exactly 2 photos, side-by-side 50/50
ASSET_VERSION = "9"  # bump this whenever display.css/display.js change, to bust the 1-year static cache

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "noticeboard.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB total per request (grid is capped at 2 photos)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000  # cache static files hard — filenames are unique per upload

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access the admin portal."
login_manager.login_message_category = "info"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(BRANDING_FOLDER, exist_ok=True)

CATEGORIES = {
    "notice": {"label": "Notice", "color": "#3E7CB1"},
    "achievement": {"label": "Achievement", "color": "#C99A2E"},
    "important": {"label": "Important", "color": "#C1443C"},
    "event": {"label": "Event", "color": "#2E8B72"},
}
DISPLAY_TYPES = ["text", "image", "grid"]
DISPLAY_TYPE_LABELS = {
    "text": "Text",
    "image": "Single image",
    "grid": "Grid — 2 photos, side by side (50% / 50%)",
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
    banner_filename = db.Column(db.String(255), nullable=True)


def get_settings():
    settings = Settings.query.first()
    if settings is None:
        settings = Settings(board_title=DEFAULT_BOARD_TITLE)
        db.session.add(settings)
        db.session.commit()
    return settings


@app.context_processor
def inject_settings():
    return {"site_settings": get_settings(), "asset_version": ASSET_VERSION}


class Notice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(20), nullable=False, default="notice")
    display_type = db.Column(db.String(20), nullable=False, default="text")
    text_content = db.Column(db.Text, nullable=True)
    images = db.Column(db.Text, nullable=True)  # JSON list of filenames
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
        return self.expires_at < date.today()

    def is_live(self):
        return self.is_active and not self.is_expired()

    def category_label(self):
        return CATEGORIES.get(self.category, {}).get("label", self.category)

    def category_color(self):
        return CATEGORIES.get(self.category, {}).get("color", "#888888")

    def to_display_dict(self):
        image_objs = [
            {"url": url_for("static", filename=f"uploads/{img}")}
            for img in self.image_list()
        ]
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
            "image_fit": self.image_fit or DEFAULT_IMAGE_FIT,
            "duration_seconds": self.duration_seconds or 10,
            "popup_duration_seconds": self.popup_duration_seconds or DEFAULT_POPUP_DURATION,
            "show_caption": self.show_caption if self.show_caption is not None else True,
            "author": self.created_by.name if self.created_by else "",
            "posted_at": posted_at.strftime("%d %b %Y, %I:%M %p") if posted_at else "",
            "updated_at": self.updated_at.strftime("%d %b %Y, %I:%M %p") if self.updated_at else "",
            "was_edited": was_edited,
        }


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# --------------------------------------------------------------------------
# Timetable model — one row per weekly lecture slot. The admin fills this in
# once for the whole week; nothing here needs day-to-day updates. This is a
# brand-new table — it doesn't touch Notice, User, or Settings in any way.
# --------------------------------------------------------------------------
class Timetable(db.Model):
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

    def is_current(self, now=None):
        """True if `now` (a datetime, defaults to this instant) falls inside
        this slot's day + time range."""
        now = now or datetime.now()
        return now.strftime("%A") == self.day and self.start_time <= now.time() < self.end_time

    def to_status_dict(self):
        """Shape consumed by the public display's 'current lecture status' slide."""
        return {
            "id": self.id,
            "day": self.day,
            "time_range": self.time_range_label(),
            "year1_faculty": self.year1_faculty,
            "year2_faculty": self.year2_faculty,
            "year3_faculty": self.year3_faculty,
            "year4_faculty": self.year4_faculty,
        }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def delete_notice_image(image_filename):
    """Remove a stored notice photo, if present."""
    path = os.path.join(app.config["UPLOAD_FOLDER"], image_filename)
    if os.path.exists(path):
        os.remove(path)


def save_uploaded_images(files):
    saved = []
    for f in files:
        if f and f.filename and allowed_file(f.filename):
            safe_name = secure_filename(f.filename)
            unique_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{safe_name}"
            f.save(os.path.join(app.config["UPLOAD_FOLDER"], unique_name))
            saved.append(unique_name)
    return saved


def save_single_image(f, folder=None):
    """Save one uploaded file and return its stored filename, or None."""
    if not (f and f.filename and allowed_file(f.filename)):
        return None
    safe_name = secure_filename(f.filename)
    unique_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{safe_name}"
    target_folder = folder or app.config["UPLOAD_FOLDER"]
    f.save(os.path.join(target_folder, unique_name))
    return unique_name


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


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

        # Title is optional — a notice can be posted with just a photo/text
        # and no headline. It's only required if the admin wants one shown.
        if category not in CATEGORIES:
            category = "notice"
        if display_type not in DISPLAY_TYPES:
            display_type = "text"

        images_json = None
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
            # Grid mode only ever exposes 2 upload slots (see grid_slot_names),
            # so a well-behaved browser can never submit more than 2 photos.
            # This is the validation for "no more than 2 images in grid mode".
            images_json = json.dumps(saved)
        elif display_type == "image":
            files = request.files.getlist("images")
            saved = save_uploaded_images(files)
            if not saved:
                flash("Please upload at least one image for this display type.", "error")
                return redirect(url_for("add_notice"))
            images_json = json.dumps(saved)
        elif display_type == "text" and not text_content:
            flash("Please enter text content.", "error")
            return redirect(url_for("add_notice"))

        expires_at = None
        if expires_at_raw:
            try:
                expires_at = datetime.strptime(expires_at_raw, "%Y-%m-%d").date()
            except ValueError:
                expires_at = None

        notice = Notice(
            title=title,
            category=category,
            display_type=display_type,
            text_content=text_content or None,
            images=images_json,
            image_fit=image_fit,
            priority=priority,
            duration_seconds=max(3, duration_seconds or 10),
            popup_duration_seconds=max(5, popup_duration_seconds or DEFAULT_POPUP_DURATION),
            show_caption=show_caption,
            created_by_id=current_user.id,
            expires_at=expires_at,
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

        expires_at_raw = request.form.get("expires_at", "").strip()
        if expires_at_raw:
            try:
                notice.expires_at = datetime.strptime(expires_at_raw, "%Y-%m-%d").date()
            except ValueError:
                pass
        else:
            notice.expires_at = None

        if notice.display_type == "grid":
            existing = notice.image_list()

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

            # anything from the old image list that isn't in the new one
            # (replaced, or left over from before the grid was capped at 2)
            # can be deleted
            removed_files = [f for f in existing if f not in updated]
            notice.images = json.dumps(updated)
            for old_file in removed_files:
                delete_notice_image(old_file)
        elif notice.display_type == "image":
            files = request.files.getlist("images")
            saved = save_uploaded_images(files)
            if saved:
                old_files = notice.image_list()
                notice.images = json.dumps(saved)
                for old_file in old_files:
                    delete_notice_image(old_file)
            elif not notice.images:
                flash("Please upload at least one image for this display type.", "error")
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
    db.session.delete(notice)
    db.session.commit()
    flash("Notice deleted.", "success")
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------------------
# Timetable management — the weekly "who's teaching which year, right now"
# schedule. Filled in once by the admin; the public display reads it
# automatically based on the current day/time (see /api/timetable/current
# and the display routes further down).
# --------------------------------------------------------------------------
def parse_clock_time(raw):
    """Parse an HTML <input type="time"> value ('HH:MM', 24-hour) into a
    Python time object, or None if it isn't valid."""
    try:
        return datetime.strptime((raw or "").strip(), "%H:%M").time()
    except ValueError:
        return None


def validate_timetable_form(form):
    """Shared validation for add/edit. Returns (fields_dict, error_message).
    error_message is None when everything is valid."""
    day = form.get("day", "").strip()
    start_time = parse_clock_time(form.get("start_time"))
    end_time = parse_clock_time(form.get("end_time"))
    year1 = form.get("year1_faculty", "").strip()
    year2 = form.get("year2_faculty", "").strip()
    year3 = form.get("year3_faculty", "").strip()
    year4 = form.get("year4_faculty", "").strip()

    if day not in WEEKDAYS:
        return None, "Please choose a valid day (Monday–Saturday)."
    if not start_time or not end_time:
        return None, "Please provide a valid start and end time."
    if end_time <= start_time:
        return None, "End time must be after the start time."
    if not all([year1, year2, year3, year4]):
        return None, "Please fill in the faculty name for all four years."

    fields = {
        "day": day, "start_time": start_time, "end_time": end_time,
        "year1_faculty": year1, "year2_faculty": year2,
        "year3_faculty": year3, "year4_faculty": year4,
    }
    return fields, None


@app.route("/timetable")
@login_required
def timetable_list():
    entries = Timetable.query.all()
    # Group/sort Monday -> Saturday, then by start time within each day —
    # friendlier to scan than insertion order.
    entries.sort(key=lambda e: (WEEKDAYS.index(e.day) if e.day in WEEKDAYS else 99, e.start_time))
    return render_template("timetable.html", entries=entries, weekdays=WEEKDAYS)


@app.route("/timetable/add", methods=["GET", "POST"])
@login_required
def add_timetable():
    if request.method == "POST":
        fields, error = validate_timetable_form(request.form)
        if error:
            flash(error, "error")
            return redirect(url_for("add_timetable"))
        db.session.add(Timetable(**fields))
        db.session.commit()
        flash("Timetable entry added.", "success")
        return redirect(url_for("timetable_list"))

    return render_template("add_timetable.html", entry=None, weekdays=WEEKDAYS)


@app.route("/timetable/edit/<int:entry_id>", methods=["GET", "POST"])
@login_required
def edit_timetable(entry_id):
    entry = Timetable.query.get_or_404(entry_id)

    if request.method == "POST":
        fields, error = validate_timetable_form(request.form)
        if error:
            flash(error, "error")
            return redirect(url_for("edit_timetable", entry_id=entry.id))
        for key, value in fields.items():
            setattr(entry, key, value)
        db.session.commit()
        flash("Timetable entry updated.", "success")
        return redirect(url_for("timetable_list"))

    return render_template("add_timetable.html", entry=entry, weekdays=WEEKDAYS)


@app.route("/timetable/delete/<int:entry_id>", methods=["POST"])
@login_required
def delete_timetable(entry_id):
    entry = Timetable.query.get_or_404(entry_id)
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

        banner_file = request.files.get("banner")
        if banner_file and banner_file.filename:
            if allowed_file(banner_file.filename):
                old_banner = settings.banner_filename
                new_filename = save_single_image(banner_file, folder=BRANDING_FOLDER)
                if new_filename:
                    settings.banner_filename = new_filename
                    if old_banner:
                        old_path = os.path.join(BRANDING_FOLDER, old_banner)
                        if os.path.exists(old_path):
                            os.remove(old_path)
            else:
                flash("Banner image must be a PNG, JPG, GIF, or WEBP file.", "error")
                return redirect(url_for("board_settings"))

        if request.form.get("remove_banner") == "1" and settings.banner_filename:
            old_path = os.path.join(BRANDING_FOLDER, settings.banner_filename)
            if os.path.exists(old_path):
                os.remove(old_path)
            settings.banner_filename = None

        db.session.commit()
        flash("Board settings updated.", "success")
        return redirect(url_for("board_settings"))

    return render_template("settings.html", settings=settings)


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


@app.route("/api/notices")
def api_notices():
    notices = (
        Notice.query.filter_by(is_active=True)
        .order_by(Notice.priority.desc(), Notice.created_at.desc())
        .all()
    )
    live = [n for n in notices if not n.is_expired()]

    rotation = [n.to_display_dict() for n in live if n.category != "important"]
    ticker = [n.to_display_dict() for n in live if n.category == "important"]

    return jsonify({
        "rotation": rotation,
        "ticker": ticker,
        "server_time": datetime.now().strftime("%A, %d %B %Y  |  %I:%M %p"),
    })


@app.route("/api/timetable/current")
def api_timetable_current():
    """Whichever timetable row's day + time range contains this exact
    moment, if any. The display polls this to build its 'current lecture
    status' slide — no manual/hourly updates needed on the admin side."""
    now = datetime.now()
    entry = next(
        (e for e in Timetable.query.filter_by(day=now.strftime("%A")).all() if e.is_current(now)),
        None,
    )
    if entry is None:
        return jsonify({"active": False})
    data = entry.to_status_dict()
    data["active"] = True
    return jsonify(data)


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


def migrate_schema():
    """Add any newly-introduced columns to an existing SQLite database.

    db.create_all() only creates tables that don't exist yet — it won't add
    a new column to a `notice` table that was created by an older version of
    this app. This does that one additive migration safely (no-op if the
    column is already there).
    """
    with db.engine.connect() as conn:
        existing_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(notice)")}
        if "popup_duration_seconds" not in existing_cols:
            conn.exec_driver_sql(
                f"ALTER TABLE notice ADD COLUMN popup_duration_seconds INTEGER DEFAULT {DEFAULT_POPUP_DURATION}"
            )
            conn.commit()
        if "show_caption" not in existing_cols:
            conn.exec_driver_sql("ALTER TABLE notice ADD COLUMN show_caption BOOLEAN DEFAULT 1")
            conn.commit()

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
