# KLS VDIT EEE Department — Smart Notice Board

A Flask + SQLite web app for the department's digital notice board. The admin
logs in to the **Admin Control** portal to post notices, achievements,
important notices, and events — as plain text, a single photo, a 2-photo
grid, or a video. A separate public **display page** is built to be opened
full-screen on the HDMI-connected TV/monitor.

## Features

- **Login-protected admin portal** (Flask-Login, hashed password)
- **Dashboard** with live counts, category filters, and status (live/hidden) toggle
- **Post/edit notices** as:
  - **Text** — typographic, full-screen
  - **Single image** — fills the entire display screen edge-to-edge
  - **Grid** — exactly **2 photos**, side by side (50% / 50%)
  - **Video** — MP4/WEBM/MOV, autoplays full-screen, muted, looped, no
    controls, up to 200 MB
  - Each notice also has a category, priority, on-screen duration, and
    optional expiry date
- **Optional OCR** — upload a flyer photo or a PDF notice, click **Extract
  Text**, review/correct the result, then publish. Manual typing still works
  exactly as before; OCR is purely an optional shortcut.
- **Flexible timetable** — add individual class entries (day, time, year,
  semester, teacher) for 2nd/3rd/4th year. Gaps are fine, several classes can
  share the same time slot, and an entry only appears on the display once it
  has a teacher name.
- **Board settings page** — rename the board.
- **Change password page** for the admin account
- **Clean, non-mixed display**:
  - Notices / Achievements / Events rotate one at a time, full-screen
  - **Important** notices scroll continuously in a ticker bar and also pop
    up full-screen once
  - The display polls the server every 30 seconds — no manual refresh needed
- SQLite database (zero external services needed)

## Project structure

```
noticeboard/
├── app.py                  # Flask app, models, routes
├── requirements.txt
├── static/
│   ├── css/style.css       # Admin portal styling
│   ├── css/display.css     # Display screen styling (incl. video)
│   ├── js/display.js       # Display rotation/ticker/grid/video/timetable logic
│   ├── js/cropper-tool.js  # Shared manual crop tool (Cropper.js wrapper)
│   └── uploads/            # Uploaded notice images & videos
└── templates/
    ├── base.html, _sidebar.html
    ├── login.html, dashboard.html, add_notice.html
    ├── settings.html, change_password.html
    ├── timetable.html, add_timetable.html
    └── display.html
```

## Setup

1. **Create a virtual environment (recommended)**
   ```bash
   python3 -m venv venv
   source venv/bin/activate      # Windows: venv\Scripts\activate
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```
   This installs Flask, SQLAlchemy, Flask-Login, `Pillow`, `PyMuPDF`
   (PDF text/rendering), and `pytesseract` (OCR). The video and timetable
   features work with just this — **OCR additionally needs the Tesseract
   executable installed on the machine** (see below). If Tesseract isn't
   installed, every other feature still works normally; OCR just shows a
   clear "not available" message.

3. **Install Tesseract (only needed for OCR) — Windows**
   1. Download the installer from
      <https://github.com/UB-Mannheim/tesseract/wiki>.
   2. Run the installer, keeping note of the install path — typically
      `C:\Program Files\Tesseract-OCR\tesseract.exe`.
   3. Set the `TESSERACT_CMD` environment variable to that path (see below).

   On Linux: `sudo apt-get install tesseract-ocr`. On macOS: `brew install tesseract`.

4. **Set environment variables**

   ```bash
   # Optional — only needed if `tesseract` isn't already on your system PATH.
   TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
   OCR_LANGUAGES=eng

   # Recommended for any real deployment.
   SECRET_KEY=some-random-string
   ```

5. **Initialize / migrate the database** (creates `noticeboard.db`, a default
   admin account, and — if you're upgrading an existing install — safely adds
   any new columns without touching your existing data)
   ```bash
   flask --app app init-db
   ```
   This prints the default login the first time:
   ```
   username: admin
   password: admin123
   ```
   **Change this password** from the admin portal's "Change password" page
   immediately after first login.

6. **Run the app**
   ```bash
   python app.py
   ```
   Or with the Flask CLI:
   ```bash
   flask --app app run --host=0.0.0.0 --port=5000
   ```
   The app also auto-creates/migrates the database and default admin the
   first time you run `python app.py` directly, so step 5 is optional in
   that case.

## Using it

- **Admin portal:** `http://<server-ip>:5000/login`
- **Display screen:** on the machine connected to the HDMI screen/TV, open
  `http://<server-ip>:5000/display` in kiosk/full-screen mode
- **Video uploads:** MP4 with H.264 video is recommended for best
  compatibility across browsers/devices. WEBM and MOV are also accepted.
  Keep videos reasonably short/compressed — the upload limit is 200 MB.
- **OCR:** in Add/Edit Notice, use the optional "Extract text from
  image/PDF (OCR)" section — upload a flyer or PDF, click **Extract Text**,
  then review/correct the text before publishing. For a flyer image you can
  choose to keep just the image, just the extracted text, or both in
  rotation.
- **Timetable:** add class entries under **Timetable management** — pick a
  day, start/end time, year (this then filters which semesters are valid),
  and optionally a teacher name. An entry only shows on the display once a
  teacher name is filled in; entries with no matching class right now simply
  don't show a timetable slide, and the normal notice rotation continues.

Because the display page polls the server on its own, you can leave it open
indefinitely — new notices posted from the admin portal will show up on the
screen within 30 seconds.

## Database migration

Nothing about upgrading requires manual SQL. Running `flask --app app
init-db` (or just starting the app) automatically adds any new columns to
the existing `notice` table if they aren't already there, without touching
existing data. If you're upgrading from a version of this app that used a
separate per-photo/video table, that data is automatically pulled back into
the simpler notice fields on first run.

## Notes on deployment

This is set up for development (`debug=True`, Flask's built-in server). For a
real deployment (e.g. running continuously behind the HDMI screen), put it
behind a production WSGI server such as **gunicorn** or **waitress**, turn
`debug` off in `app.py`, and set `SECRET_KEY` via an environment variable
rather than relying on the default.

## Timetable upload workflow (updated)

- The timetable admin screen contains separate cards for **2nd Year, 3rd Year,
  and 4th Year**.
- Each year offers only its two semesters: **2nd = 3rd/4th, 3rd = 5th/6th,
  4th = 7th/8th**.
- Admin can upload **Excel (.xlsx), PDF, JPG, PNG, or WEBP** for each semester.
  Uploading a semester replaces the previous entries for that semester and
  automatically makes that semester the active semester for its year.
- Only one semester per year is active on the public board, so the public
  display never repeats a year.
- Excel imports support both a simple Day/Start/End/Subject/Faculty layout and
  a matrix timetable layout. PDF/photo imports use OCR and spatial timetable
  extraction. Faculty is matched from subject-code/faculty information when
  the uploaded document contains it; unmatched rows remain editable manually.
- Manual class entry remains available as a fallback, and an admin can switch
  which stored semester is active with **Show this semester**.
- The public display only renders the **currently ongoing period**. It never
  shows future or previous time slots. It shows one row each for 2nd, 3rd and
  4th year, with the active semester beside the year and **NO CLASS** when that
  year has no lecture in the current period.
- The timetable slide keeps the board's existing dark smart-notice-board look.
  The bottom shows only the active period's time range; it does not label the
  clock as "Current Time" and does not show future periods.
- OCR on **Post notice** is available only when the display type is **Text**.
  Image, Grid, and Video posts do not expose or process the notice OCR tool.
