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

  const POLL_MS = 30000;
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
    const parts = [];
    if (item.posted_at) parts.push(`Posted ${item.posted_at}`);
    if (item.author) parts.push(`by ${item.author}`);
    if (item.was_edited && item.updated_at) parts.push(`edited ${item.updated_at}`);
    return parts.join("  ·  ");
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
    const wrap = document.createElement("div");
    wrap.className = "caption-bar" + (item.has_title ? "" : " no-title");
    wrap.style.setProperty("--cat", item.category_color);

    const badge = document.createElement("div");
    badge.className = "slide-badge";
    badge.style.setProperty("--cat", item.category_color);
    badge.textContent = item.category_label;
    wrap.appendChild(badge);

    // Title is optional — only render it if the admin actually gave the
    // notice one. The meta line (posted date) always shows either way.
    if (item.has_title) {
      const title = document.createElement("h1");
      title.className = "slide-title";
      title.textContent = item.title;
      wrap.appendChild(title);
    }

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

    if (item.images && item.images.length) {
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
  // alongside the regular notice slides.
  function timetableSlideFrom(status) {
    return {
      id: `timetable-${status.id}`,
      display_type: "timetable",
      duration_seconds: TIMETABLE_SLIDE_SECONDS,
      day: status.day,
      time_range: status.time_range,
      year1_faculty: status.year1_faculty,
      year2_faculty: status.year2_faculty,
      year3_faculty: status.year3_faculty,
      year4_faculty: status.year4_faculty,
    };
  }

  // Interleaves the current-lecture-status slide with the regular notice
  // rotation — timetable slide, then a notice, then the timetable slide
  // again, and so on — so the two alternate continuously. If there are no
  // notices at all, the timetable slide plays on its own; if there's no
  // current lecture slot, the notice rotation plays exactly as before.
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
      combined.push(ttSlide);
      combined.push(notice);
    });
    return combined;
  }

  // A cheap "did anything change" fingerprint covering both the notice
  // rotation and the current timetable slot, used to decide whether the
  // stage needs to rebuild.
  function comboSignature(notices, timetableStatus) {
    const noticeIds = notices.map((n) => n.id).join(",");
    const ttKey = timetableStatus && timetableStatus.active ? `tt-${timetableStatus.id}` : "tt-none";
    return `${noticeIds}|${ttKey}`;
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

      const rows = document.createElement("div");
      rows.className = "timetable-rows";
      [
        ["1st Year", item.year1_faculty],
        ["2nd Year", item.year2_faculty],
        ["3rd Year", item.year3_faculty],
        ["4th Year", item.year4_faculty],
      ].forEach(([label, faculty]) => {
        const row = document.createElement("div");
        row.className = "timetable-row";
        const yearLabel = document.createElement("span");
        yearLabel.className = "year-label";
        yearLabel.textContent = label;
        const facultyName = document.createElement("span");
        facultyName.className = "faculty-name";
        facultyName.textContent = faculty;
        row.appendChild(yearLabel);
        row.appendChild(facultyName);
        rows.appendChild(row);
      });
      wrap.appendChild(rows);

      const slotTime = document.createElement("div");
      slotTime.className = "timetable-slot-time";
      slotTime.textContent = `${item.day}  ·  ${item.time_range}`;
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
        if (idx === 0) img.classList.add("shown");
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
    const isPhotoSlide = item.display_type === "image" || item.display_type === "grid";
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
    try {
      const [noticesRes, timetableRes] = await Promise.all([
        fetch("/api/notices", { cache: "no-store" }),
        fetch("/api/timetable/current", { cache: "no-store" }),
      ]);
      const data = await noticesRes.json();
      const timetableStatus = await timetableRes.json();

      const oldSignature = comboSignature(latestNotices, latestTimetableStatus);
      const newSignature = comboSignature(data.rotation, timetableStatus);

      renderTicker(data.ticker);
      handleImportantNotices(data.ticker);

      latestNotices = data.rotation;
      latestTimetableStatus = timetableStatus;

      const combined = buildCombinedRotation(data.rotation, timetableStatus);
      updateIdleState(combined.length > 0, data.ticker.length > 0);

      if (newSignature !== oldSignature) {
        rotation = combined;
        currentIndex = 0;
        renderStageForCurrent();
        scheduleNext();
      }
    } catch (err) {
      console.error("Failed to load notices/timetable", err);
    }
  }

  poll();
  setInterval(poll, POLL_MS);
})();
