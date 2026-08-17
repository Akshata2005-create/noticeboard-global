# KLS VDIT EEE Department — Smart Notice Board

A Flask + SQLite web app for the department's digital notice board. The admin
logs in to the **Admin Control** portal to post notices, achievements,
important notices, and events — as plain text, a single photo, a photo
slideshow, or multiple photos tiled together. A separate public **display
page** is built to be opened full-screen on the HDMI-connected TV/monitor.

## Features

- **Login-protected admin portal** (Flask-Login, hashed password), single
  admin account — no separate staff logins to manage
- **Dashboard** with live counts, category filters, and status (live/hidden) toggle
- **Post/edit notices** as:
  - **Text** — typographic, full-screen
  - **Single image** — fills the entire display screen edge-to-edge
  - **Slideshow** — multiple photos, cycling one at a time, full-screen
  - **Grid** — always exactly **4 photos**, each chosen in its own slot
    (Photo 1 / Photo 2 / Photo 3 / Photo 4), shown together tiled 2×2 to fill
    the whole screen with no gaps. Editing a grid notice lets you replace a
    single slot without touching the other three.
  - Each notice also has a category, priority, on-screen duration, and
    optional expiry date
- **Manual crop tool** — after choosing any photo (a notice photo, a grid
  slot, or the department banner), hit **Crop** to open a drag-to-select,
  zoomable crop box (built on Cropper.js) and frame exactly the part of the
  photo you want, freeform, before publishing. Nothing is auto-cropped
  without the admin's say-so.
- **Photo fit control** — for any photo-based notice, pick **"Show the whole
  photo"** (nothing cropped, default) or **"Fill the screen"** (crops edges
  to remove borders). Publish, check the display screen, and switch this per
  notice until it looks right. Works together with manual cropping — crop
  first for framing, then choose fit for how it fills its space on screen.
- **Board settings page** — rename the board and upload a department banner
  photo (with the same manual crop tool). The banner blends into the
  display screen: when nothing is posted, it fills the entire screen so the
  board is never blank; the moment a notice or photo is posted, it shrinks
  smoothly into a slim strip across the top, leaving the rest of the screen
  entirely to the notice content with no overlap. It stays there
  permanently until an admin changes or removes it.
- **Change password page** for the admin account
- **Clean, non-mixed display**:
  - Notices / Achievements / Events rotate one at a time, full-screen, with a
    clear category badge and colour overlaid on the content so it's obvious
    what you're looking at
  - **Important** notices never get lost in the rotation — they scroll
    continuously in a dedicated ticker bar at the bottom of the screen instead
  - The display polls the server every 30 seconds and updates automatically —
    no need to refresh the screen or restart anything
- SQLite database (zero external services needed)

## Project structure

```
noticeboard/
├── app.py                  # Flask app, models, routes
├── requirements.txt
├── static/
│   ├── css/style.css       # Admin portal + crop modal styling
│   ├── css/display.css     # Display screen styling (incl. idle banner state)
│   ├── js/display.js       # Display rotation/ticker/grid/idle-state logic
│   ├── js/cropper-tool.js  # Shared manual crop tool (Cropper.js wrapper)
│   ├── uploads/            # Uploaded notice images
│   └── branding/           # Uploaded department banner photo
└── templates/
    ├── base.html, _sidebar.html
    ├── login.html, dashboard.html, add_notice.html
    ├── settings.html, change_password.html
    └── display.html
```

Cropper.js is loaded from a CDN (cdnjs) on the pages that need it, so the
admin machine needs internet access to load the admin portal's crop tool —
the display screen itself doesn't use it and needs no internet beyond
reaching the Flask server.

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

3. **Initialize the database** (creates `noticeboard.db` and a default admin account)
   ```bash
   flask --app app init-db
   ```
   This prints the default login:
   ```
   username: admin
   password: admin123
   ```
   **Change this password** by creating a new admin account from the Staff
   Accounts page and removing the default one, or by editing it directly —
   there's no self-service "change password" screen in this build, so add one
   if you need it.

4. **Run the app**
   ```bash
   python app.py
   ```
   Or with the Flask CLI:
   ```bash
   flask --app app run --host=0.0.0.0 --port=5000
   ```

   The app also auto-creates the database and default admin the first time
   you run `python app.py` directly, so step 3 is optional in that case.

## Using it

- **Admin portal:** go to `http://<server-ip>:5000/login`
- **Display screen:** on the machine connected to the HDMI screen/TV, open a
  browser to `http://<server-ip>:5000/display` and put the browser into
  full-screen / kiosk mode (F11 in Chrome/Firefox, or launch Chrome with
  `--kiosk http://<server-ip>:5000/display`)
- **Posting photos:** after choosing a photo (in Image, Slideshow, Grid, or
  the banner upload), a thumbnail appears with a **✂ Crop** button — click it
  to drag, zoom, and frame exactly the part of the photo you want, freeform.
  Then pick **Show the whole photo** or **Fill the screen** under "How
  should photos be shown?" for how the cropped result fills its space.
  Publish, check the display screen, and adjust either setting if it doesn't
  look right yet.
- **Grid mode** always uses exactly 4 photos, one per named slot (Photo 1–4)
  — pick each position separately so you control exactly what appears where.
  Editing a grid notice lets you replace just one slot; leave the others
  blank to keep their current photo.
- **Department banner:** from the admin sidebar, go to **Board name &
  banner** and upload (and crop) a wide photo of the department.
  - When **no notices are posted**, the banner fills the entire display
    screen — so the board is never a blank page.
  - The moment a notice is posted, the banner smoothly shrinks into a slim
    strip across the top, and the rest of the screen is handed entirely to
    the notice content — no overlap, no crowding.
  - It stays there permanently until you upload a new one or remove it.

Because the display page polls the server on its own, you can leave it open
indefinitely — new notices posted from the admin portal on any device will
show up on the screen within 30 seconds, without touching the display machine.

## Notes on deployment

This is set up for development (`debug=True`, Flask's built-in server). For a
real deployment (e.g. running continuously behind the HDMI screen), put it
behind a production WSGI server such as **gunicorn** or **waitress**, and turn
`debug` off in `app.py`. Also change `SECRET_KEY` to a random value via the
`SECRET_KEY` environment variable rather than the placeholder in the code.
