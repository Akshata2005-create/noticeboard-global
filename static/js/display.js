(function () {
  const board = document.querySelector(".board");
  const stage = document.getElementById("stage");
  const stageEmpty = document.getElementById("stage-empty");
  const dotsWrap = document.getElementById("dots");
  const tickerWrap = document.getElementById("ticker-wrap");
  const tickerContent = document.getElementById("ticker-content");
  const clockEl = document.getElementById("clock");
  const importantBanner = document.getElementById("important-banner");
  const popupOverlay = document.getElementById("important-popup-overlay");
  const popupCard = document.getElementById("important-popup-card");

  let rotation = [];
  let currentIndex = 0;
  let slideTimer = null;

  // Important-notice popup/banner state
  const shownImportantIds = new Set();   // ids that have already had their popup shown
  const bannerItems = new Map();         // id -> item, currently pinned in the top banner
  let popupQueue = [];
  let popupShowing = false;
  let popupTimer = null;
  let currentPopupItem = null;
  let latestImportantIds = new Set();

  const POLL_MS = 30000; // slow safety-net poll; the SSE channel below is the fast path
  const EVENTS_URL = "/api/events";
  const DEFAULT_DURATION = 10;
  const DEFAULT_POPUP_DURATION = 15;
  const TIMETABLE_SLIDE_SECONDS = 10; // how long the "current lecture status" slide stays up

  // Latest known state from the two endpoints — used to detect real changes
  // (a new/removed notice, or the lecture slot rolling over) so the stage
  // only rebuilds when something actually changed, not on every 30s poll.
  let latestNotices = [];
  let latestTimetableStatus = { active: false };

  // Build the "Posted <date>" / "Posted <date> · edited <date>" meta line.
  // Always uses the notice's actual posted (created) date, never the
  // updated date alone, so the display never shows the wrong date.
  function metaLine(item) {
    // Image-post footer: show only the posting date. Keep the footer
    // deliberately minimal — no category, title, author, or time.
    return item.posted_at ? `Posted on ${item.posted_at.split(",")[0]}` : "";
  }

  function updateClock() {
    const now = new Date();
    const opts = { weekday: "long", year: "numeric", month: "long", day: "numeric" };
    const dateStr = now.toLocaleDateString(undefined, opts);
    const timeStr = now.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    clockEl.textContent = `${dateStr}  |  ${timeStr}`;
  }
  updateClock();
  setInterval(updateClock, 1000 * 30);

  // Grid mode is fixed at exactly 2 photos, laid out as one row of two
  // equal (50% / 50%) cells — simpler than the old variable-count tiling
  // logic since a grid notice can never have more than 2 images.
  function computeGridRows(n) {
    return n > 0 ? [Math.min(n, 2)] : [];
  }

  function buildCaption(item) {
    // For image posts, keep only a tiny date footer. The image remains the
    // main focus and the footer never becomes a large bottom bar.
    const wrap = document.createElement("div");
    wrap.className = "caption-bar minimal-date";

    const meta = document.createElement("div");
    meta.className = "slide-meta";
    meta.textContent = metaLine(item);
    wrap.appendChild(meta);

    return wrap;
  }

  // ---------- Important-notice popup + persistent top banner ----------

  function buildImportantPopupContent(item) {
    popupCard.innerHTML = "";

    const flag = document.createElement("div");
    flag.className = "important-popup-flag";
    const dot = document.createElement("span");
    dot.className = "siren";
    flag.appendChild(dot);
    const flagText = document.createElement("span");
    flagText.textContent = "Important Notice";
    flag.appendChild(flagText);
    popupCard.appendChild(flag);

    if (item.display_type === "video" && item.video) {
      const media = document.createElement("div");
      media.className = "important-popup-media";
      const video = document.createElement("video");
      video.src = item.video.url;
      video.autoplay = true;
      video.loop = true;
      video.muted = true;
      video.playsInline = true;
      video.controls = false;
      media.appendChild(video);
      popupCard.appendChild(media);
    } else if (item.images && item.images.length) {
      const media = document.createElement("div");
      media.className = "important-popup-media";
      const img = document.createElement("img");
      img.src = item.images[0].url;
      media.appendChild(img);
      popupCard.appendChild(media);
    }

    const body = document.createElement("div");
    body.className = "important-popup-body";

    // Title is optional here too — an important alert can be posted as
    // just a photo and/or message with no headline.
    if (item.has_title) {
      const title = document.createElement("h2");
      title.className = "important-popup-title";
      title.textContent = item.title;
      body.appendChild(title);
    }

    if (item.text_content) {
      const text = document.createElement("div");
      text.className = "important-popup-text";
      text.textContent = item.text_content;
      body.appendChild(text);
    }

    const meta = document.createElement("div");
    meta.className = "important-popup-meta";
    meta.textContent = metaLine(item);
    body.appendChild(meta);

    popupCard.appendChild(body);

    const timerbar = document.createElement("div");
    timerbar.className = "important-popup-timerbar";
    timerbar.style.animationDuration = `${item.popup_duration_seconds || DEFAULT_POPUP_DURATION}s`;
    popupCard.appendChild(timerbar);
  }

  function renderBanner() {
    importantBanner.innerHTML = "";
    bannerItems.forEach((item) => {
      const row = document.createElement("div");
      row.className = "important-banner-row";
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "IMPORTANT";
      row.appendChild(tag);
      const txt = document.createElement("span");
      txt.className = "txt";
      txt.textContent = bannerText(item);
      row.appendChild(txt);
      importantBanner.appendChild(row);
    });
    importantBanner.classList.toggle("show", bannerItems.size > 0);
  }

  // Ticker/banner one-liner — falls back gracefully when there's no title.
  function bannerText(item) {
    if (item.has_title) {
      return item.title + (item.text_content ? " — " + item.text_content : "");
    }
    return item.text_content || item.category_label;
  }

  function addToBanner(item) {
    bannerItems.set(item.id, item);
    renderBanner();
  }

  function removeFromBanner(id) {
    bannerItems.delete(id);
    renderBanner();
  }

  function showNextPopup() {
    if (popupShowing || popupQueue.length === 0) return;
    const item = popupQueue.shift();
    currentPopupItem = item;
    popupShowing = true;

    buildImportantPopupContent(item);
    requestAnimationFrame(() => popupOverlay.classList.add("active"));

    const duration = (item.popup_duration_seconds || DEFAULT_POPUP_DURATION) * 1000;
    clearTimeout(popupTimer);
    popupTimer = setTimeout(() => {
      popupOverlay.classList.remove("active");
      setTimeout(() => {
        popupShowing = false;
        if (latestImportantIds.has(item.id)) {
          addToBanner(item);
        }
        currentPopupItem = null;
        showNextPopup();
      }, 420);
    }, duration);
  }

  function handleImportantNotices(importantList) {
    const currentIds = new Set(importantList.map((n) => n.id));
    latestImportantIds = currentIds;

    importantList.forEach((item) => {
      const alreadyHandled =
        shownImportantIds.has(item.id) ||
        bannerItems.has(item.id) ||
        popupQueue.some((p) => p.id === item.id) ||
        (currentPopupItem && currentPopupItem.id === item.id);
      if (!alreadyHandled) {
        shownImportantIds.add(item.id);
        popupQueue.push(item);
      } else if (bannerItems.has(item.id)) {
        bannerItems.set(item.id, item); // keep banner text fresh if edited
      }
    });

    // A notice leaves the banner (and can pop up again if reposted) once an
    // admin deactivates, deletes, or lets it expire — it drops out of the
    // server's "important" list, which is what we're comparing against here.
    [...bannerItems.keys()].forEach((id) => {
      if (!currentIds.has(id)) {
        removeFromBanner(id);
        shownImportantIds.delete(id);
      }
    });
    popupQueue = popupQueue.filter((item) => currentIds.has(item.id));

    renderBanner();
    showNextPopup();
  }

  // ---------- Timetable ("current lecture status") integration ----------

  // Turns the /api/timetable/current response into a slide-shaped object
  // that buildSlide() knows how to render (display_type: "timetable"),
  // alongside the regular notice slides. `entries` is a list because
  // multiple classes (different years/sections) can share the same day
  // and time slot — there's no fixed year1..year4 layout any more.
  function timetableSlideFrom(status) {
    const entries = status.entries || [];
    return {
      id: `timetable-${status.day}-${status.time_range}-${entries.map((e) => e.year + e.semester + e.teacher_name).join("|")}`,
      display_type: "timetable",
      duration_seconds: TIMETABLE_SLIDE_SECONDS,
      day: status.day,
      time_range: status.time_range,
      entries: entries,
    };
  }

  // Inserts the current-lecture-status slide into the normal notice rotation.
  // A normal notice is shown first; the timetable follows it, then the next
  // notice, and so on. If there are no notices, the timetable plays alone.
  // This keeps the board feeling like a noticeboard while still surfacing the
  // live lecture status during the same rotation.
  function buildCombinedRotation(notices, timetableStatus) {
    if (!timetableStatus || !timetableStatus.active) {
      return notices.slice();
    }
    const ttSlide = timetableSlideFrom(timetableStatus);
    if (notices.length === 0) {
      return [ttSlide];
    }
    const combined = [];
    notices.forEach((notice) => {
      combined.push(notice);
      combined.push(ttSlide);
    });
    return combined;
  }

  // A cheap "did anything change" fingerprint covering both the notice
  // rotation and the current timetable slot, used to decide whether the
  // stage needs to rebuild.


  // A cheap "did anything change" fingerprint covering both the notice
  // rotation and the current timetable slot(s), used to decide whether the
  // stage needs to rebuild.
  function comboSignature(notices, timetableStatus) {
    // Include the visible notice content, not only its id. This means an
    // edited notice is refreshed on the display without requiring a page
    // reload.
    const noticeKey = notices.map((n) => [
      n.id,
      n.title,
      n.text_content,
      n.display_type,
      n.category,
      n.images,
      n.video,
      n.updated_at,
      n.duration_seconds
    ]).join("||");

    const entries = (timetableStatus && timetableStatus.entries) || [];
    const ttKey = timetableStatus && timetableStatus.active
      ? `tt-${timetableStatus.day}-${timetableStatus.time_range}-${entries.map((e) => e.year + e.semester + e.teacher_name).join("|")}`
      : "tt-none";
    return `${noticeKey}|${ttKey}`;
  }

  function buildSlide(item) {
    const el = document.createElement("div");
    el.className = "slide";
    el.dataset.id = item.id;

    if (item.display_type === "timetable") {
      const wrap = document.createElement("div");
      wrap.className = "slide-timetable-wrap";

      const heading = document.createElement("div");
      heading.className = "timetable-heading";
      heading.textContent = "EEE DEPARTMENT";
      wrap.appendChild(heading);

      const subheading = document.createElement("div");
      subheading.className = "timetable-subheading";
      subheading.textContent = "CURRENT LECTURE STATUS";
      wrap.appendChild(subheading);

      // No fixed year1..year4 columns any more — just one row per active
      // class entry, whatever years/semesters happen to have a class right
      // now. Entries without a teacher name are filtered out server-side
      // already, so every row here is guaranteed to be real and complete.
      const rows = document.createElement("div");
      rows.className = "timetable-rows";
      (item.entries || []).forEach((entry) => {
        const row = document.createElement("div");
        row.className = "timetable-row";
        const yearLabel = document.createElement("span");
        yearLabel.className = "year-label";
        yearLabel.textContent = entry.no_class ? `${entry.year}  (${entry.semester || ""})` : `${entry.year}  (${entry.semester || ""})`;
        const facultyName = document.createElement("span");
        facultyName.className = "faculty-name";
        facultyName.textContent = entry.no_class ? "NO CLASS" : (entry.teacher_name || "NO CLASS");
        row.appendChild(yearLabel);
        row.appendChild(facultyName);
        rows.appendChild(row);
      });
      wrap.appendChild(rows);

      const slotTime = document.createElement("div");
      slotTime.className = "timetable-slot-time";
      slotTime.textContent = item.time_range;
      wrap.appendChild(slotTime);

      el.appendChild(wrap);
      return el;
    }

    if (item.display_type === "text") {
      const wrap = document.createElement("div");
      wrap.className = "slide-text-wrap";
      wrap.style.setProperty("--cat", item.category_color);

      // No repeated board-title/department eyebrow here — the topbar
      // already shows "EEE SMART NOTICE BOARD" once, so a text post shows
      // only its own title (if any) and message content, nothing duplicated.
      if (item.has_title) {
        const title = document.createElement("h1");
        title.className = "slide-title";
        title.textContent = item.title;
        wrap.appendChild(title);
      }

      const p = document.createElement("div");
      p.className = "slide-text";
      p.textContent = item.text_content;
      wrap.appendChild(p);

      const meta = document.createElement("div");
      meta.className = "slide-meta";
      meta.textContent = metaLine(item);
      wrap.appendChild(meta);

      el.appendChild(wrap);
      return el;
    }

    if (item.display_type === "video") {
      const wrap = document.createElement("div");
      wrap.className = "slide-video-wrap";
      const video = document.createElement("video");
      if (item.video) video.src = item.video.url;
      video.autoplay = true;
      video.loop = true;
      video.muted = true;
      video.defaultMuted = true;
      video.playsInline = true;
      video.controls = false;
      // If the video can't load/play, fail gracefully and keep the
      // slideshow moving rather than getting stuck on a broken slide.
      video.addEventListener("error", () => advance());
      wrap.appendChild(video);
      el.appendChild(wrap);
      requestAnimationFrame(() => {
        const playPromise = video.play();
        if (playPromise && playPromise.catch) playPromise.catch(() => advance());
      });
      return el;
    }

    // Photo-based slides (image / grid). Build the photo(s)
    // first, then decide whether they get the whole stage to themselves
    // (pure image — maximum space, no badge/title/date bar at all) or share
    // it with a compact caption bar underneath (never overlapping the photo).
    let media;
    if (item.display_type === "grid") {
      media = document.createElement("div");
      media.className = "slide-grid";
      const rowCounts = computeGridRows(item.images.length);
      let imgIdx = 0;
      rowCounts.forEach((countInRow) => {
        const rowEl = document.createElement("div");
        rowEl.className = "grid-row";
        for (let i = 0; i < countInRow; i++) {
          const im = item.images[imgIdx++];
          const cell = document.createElement("div");
          cell.className = "slide-grid-cell";
          const img = document.createElement("img");
          img.src = im.url;
          // grid tiles always fill edge-to-edge (object-fit: cover in CSS) —
          // there's no room for letterboxing in a small tile, so image_fit
          // (which is for single-image only) doesn't apply here.
          cell.appendChild(img);
          rowEl.appendChild(cell);
        }
        media.appendChild(rowEl);
      });
    } else {
      media = document.createElement("div");
      media.className = "slide-media-full";
      item.images.forEach((im, idx) => {
        const img = document.createElement("img");
        img.src = im.url;
        img.style.objectFit = item.image_fit || "cover";
        if (idx === 0) {
          img.classList.add("shown");
        }
        media.appendChild(img);
      });
    }

    if (item.show_caption === false) {
      // Pure image — the photo(s) fill the entire stage, no bar at all.
      el.appendChild(media);
    } else {
      const column = document.createElement("div");
      column.className = "slide-media-column";
      const area = document.createElement("div");
      area.className = "slide-media-area";
      area.appendChild(media);
      column.appendChild(area);
      column.appendChild(buildCaption(item));
      el.appendChild(column);
    }

    return el;
  }

  function renderDots() {
    dotsWrap.innerHTML = "";
    rotation.forEach((_, idx) => {
      const d = document.createElement("div");
      d.className = "dot" + (idx === currentIndex ? " current" : "");
      dotsWrap.appendChild(d);
    });
  }

  function clearStage() {
    stage.querySelectorAll(".slide").forEach((el) => el.remove());
  }

  function renderStageForCurrent() {
    clearStage();

    if (rotation.length === 0) {
      stageEmpty.style.display = "";
      dotsWrap.innerHTML = "";
      board.classList.remove("photo-mode");
      return;
    }
    stageEmpty.style.display = "none";

    const item = rotation[currentIndex];
    const el = buildSlide(item);
    stage.appendChild(el);
    requestAnimationFrame(() => el.classList.add("active"));

    // Image/grid posts get almost the whole screen — the topbar shrinks to
    // a slim clock strip so the photo reads like a real signage poster.
    // Text posts and the timetable status slide keep the full topbar since
    // there's no photo to compete with for space.
    const isPhotoSlide = item.display_type === "image" || item.display_type === "grid" || item.display_type === "video";
    board.classList.toggle("photo-mode", isPhotoSlide);

    renderDots();
  }

  function advance() {
    if (rotation.length === 0) return;
    currentIndex = (currentIndex + 1) % rotation.length;
    renderStageForCurrent();
    scheduleNext();
  }

  function scheduleNext() {
    clearTimeout(slideTimer);
    if (rotation.length === 0) return;
    const duration = (rotation[currentIndex].duration_seconds || DEFAULT_DURATION) * 1000;
    slideTimer = setTimeout(advance, duration);
  }

  function renderTicker(items) {
    if (!items.length) {
      tickerWrap.style.display = "none";
      return;
    }
    tickerWrap.style.display = "flex";
    tickerContent.innerHTML = "";
    items.forEach((item) => {
      const span = document.createElement("span");
      span.className = "item";
      span.textContent = bannerText(item);
      tickerContent.appendChild(span);
    });
    // duplicate content for seamless loop
    tickerContent.innerHTML += tickerContent.innerHTML;
  }

  function updateIdleState(hasRotation, hasTicker) {
    const idle = !hasRotation && !hasTicker;
    board.classList.toggle("idle", idle);
    if (!hasRotation && hasTicker) {
      stageEmpty.querySelector(".no-signal").textContent = "IMPORTANT NOTICE BELOW";
      stageEmpty.querySelector("p").textContent = "See the ticker at the bottom of the screen.";
    } else {
      stageEmpty.querySelector(".no-signal").textContent = "NO ACTIVE NOTICES";
      stageEmpty.querySelector("p").textContent = "Posts published from the admin portal will appear here.";
    }
  }

  async function poll() {
    // Load notices independently from the timetable. A timetable/API error
    // must never prevent normal notices from reaching the display screen.
    try {
      const noticesRes = await fetch("/api/notices", {
        cache: "no-store",
        headers: { "Cache-Control": "no-cache" }
      });

      if (!noticesRes.ok) {
        throw new Error(`Notice API returned HTTP ${noticesRes.status}`);
      }

      const data = await noticesRes.json();
      const oldSignature = comboSignature(latestNotices, latestTimetableStatus);

      const safeRotation = Array.isArray(data.rotation) ? data.rotation : [];
      const safeTicker = Array.isArray(data.ticker) ? data.ticker : [];

      renderTicker(safeTicker);
      handleImportantNotices(safeTicker);

      latestNotices = safeRotation;

      // Keep the last known timetable state if that endpoint temporarily
      // fails. Notices can therefore continue rotating normally.
      let timetableStatus = latestTimetableStatus;
      try {
        const timetableRes = await fetch("/api/timetable/current", {
          cache: "no-store",
          headers: { "Cache-Control": "no-cache" }
        });
        if (timetableRes.ok) {
          const freshTimetable = await timetableRes.json();
          if (freshTimetable && typeof freshTimetable === "object") {
            timetableStatus = freshTimetable;
          }
        } else {
          console.warn(`Timetable API returned HTTP ${timetableRes.status}`);
        }
      } catch (timetableErr) {
        console.warn("Timetable API unavailable; continuing with notices.", timetableErr);
      }

      latestTimetableStatus = timetableStatus;

      const combined = buildCombinedRotation(safeRotation, timetableStatus);
      updateIdleState(combined.length > 0, safeTicker.length > 0);

      const newSignature = comboSignature(safeRotation, timetableStatus);

      if (newSignature !== oldSignature || rotation.length === 0) {
        rotation = combined;
        currentIndex = 0;
        renderStageForCurrent();
        scheduleNext();
      }
    } catch (err) {
      console.error("Failed to load notices", err);
    }
  }

  // ---------- Live sync (Server-Sent Events) ----------
  // One common channel for the whole display (notices + timetable). The
  // server pushes an "update" event the moment anything changes in the
  // database, and we simply re-run the same poll() used for the periodic
  // refresh — no full page reload, no reopening the Display URL. The
  // interval poll() above/below keeps running regardless, as a safety net
  // in case a proxy between here and the server ever blocks SSE.
  function connectLiveSync() {
    if (typeof EventSource === "undefined") {
      console.warn("EventSource not supported; relying on periodic polling only.");
      return;
    }
    let source;
    try {
      source = new EventSource(EVENTS_URL);
    } catch (err) {
      console.warn("Could not open live sync channel; relying on periodic polling only.", err);
      return;
    }
    source.addEventListener("update", () => {
      poll();
    });
    source.onerror = () => {
      // The browser's EventSource retries this automatically (see the
      // "retry:" hint sent by the server); nothing to do here except let
      // the fallback interval poll keep the display correct meanwhile.
    };
  }

  poll();
  connectLiveSync();
  setInterval(poll, POLL_MS);
})();
