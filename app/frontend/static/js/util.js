// util.js — extracted verbatim from the old dashboard.js IIFE.

function truncate(str, len) {
  if (!str) return '';
  return str.length > len ? str.slice(0, Math.max(1, len - 1)) + '…' : str;
}

function screenshotSrc(b64) {
  return b64 ? 'data:image/jpeg;base64,' + b64 : '';
}

// ---------------------------------------------------------------------------
// Image downscaling
//
// A stored screenshot is a full phone frame — 1080x2400, which the browser decodes to a
// ~10MB bitmap no matter how small it is drawn. The board shows every screen at a couple
// of hundred pixels, so a 39-screen project was asking for ~390MB of bitmap and locking
// the tab solid before a single card appeared. (Measured, and reproducible on the old
// tabbed UI too — this is not new, it is just no longer survivable now that the board and
// the agent share one screen.)
//
// So every stored screenshot is decoded once, drawn down to something proportionate to how
// it is actually displayed, and cached by state hash. Full resolution is still what the
// detail modal and the lightbox open — those show one image at a time, which is affordable.
// Decodes are queued two at a time so the transient full-size bitmaps never pile up.
// ---------------------------------------------------------------------------
// One size, shared. A 360px-wide JPEG costs nothing to display in a 26px tile, and
// deriving a second, smaller copy would mean decoding the full-size original twice —
// the expensive half of the work, done again for no visible difference.
const CARD_MAX_W = 360;          // node cards: ~2x their on-screen width at normal zoom
const DECODE_CONCURRENCY = 2;

// The board canvas is zoomable, and a card's on-screen size at normal zoom is not its
// on-screen size once the user zooms in to actually read a screen — that is the whole
// point of zooming. A thumbnail sized for 1x stays 360px wide no matter how far the card
// is stretched, so the browser upscales it and it blurs exactly when someone leans in to
// look closely. These are the sizes the canvas card is allowed to ask for instead, picked
// coarse enough that panning/zooming continuously does not thrash the decoder — the same
// bucket keeps returning a cache hit until the card crosses into the next one.
const CARD_ZOOM_BUCKETS = [360, 640, 960, 1400, 2000];

/** The smallest bucket that comfortably covers a card rendered `displayPx` wide. */
function cardTargetWidth(displayPx) {
  for (const bucket of CARD_ZOOM_BUCKETS) {
    if (displayPx <= bucket) return bucket;
  }
  return CARD_ZOOM_BUCKETS[CARD_ZOOM_BUCKETS.length - 1];
}

const scaledCache = new Map();   // `${hash}@${maxW}` -> small data URL
// state_hash -> naturalWidth / naturalHeight of the *stored* screenshot. The downscale
// below preserves the ratio, so this is equally the ratio of the scaled copy — but it is
// the only place the number is ever known, since nothing else in the frontend decodes a
// screenshot. The card box is shaped from it (see render.js), which is what keeps the
// thumbnail from being cropped to fit a box of the wrong shape.
const shotAspect = new Map();
const scaleQueue = [];
let scaleWorkers = 0;

function decodeScaled(b64, maxW) {
  return new Promise((resolve) => {
    const src = screenshotSrc(b64);
    if (!src) { resolve(null); return; }
    const img = new Image();
    img.decoding = 'async';
    img.onload = () => {
      try {
        const scale = Math.min(1, maxW / (img.naturalWidth || maxW));
        const canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round((img.naturalWidth || maxW) * scale));
        canvas.height = Math.max(1, Math.round((img.naturalHeight || maxW) * scale));
        canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
        resolve({
          url: canvas.toDataURL('image/jpeg', 0.92),
          aspect: (img.naturalWidth && img.naturalHeight)
            ? img.naturalWidth / img.naturalHeight
            : null,
        });
      } catch {
        resolve(null);   // tainted or malformed — the caller keeps its placeholder
      } finally {
        img.src = '';    // drop the full-size bitmap now, not at the next GC
      }
    };
    img.onerror = () => { img.src = ''; resolve(null); };
    img.src = src;
  });
}

function pumpScaleQueue() {
  while (scaleWorkers < DECODE_CONCURRENCY && scaleQueue.length) {
    const job = scaleQueue.shift();
    scaleWorkers += 1;
    decodeScaled(job.b64, job.maxW).then((res) => {
      if (res) {
        if (res.aspect) shotAspect.set(job.hash, res.aspect);
        scaledCache.set(job.key, res.url);
        job.apply(res.url, job.hash);
      }
      scaleWorkers -= 1;
      pumpScaleQueue();
    });
  }
}

/** Hand `apply` a scaled data URL for this screenshot, now if cached or later if not. */
function requestScaled(hash, b64, maxW, apply) {
  if (!b64) return null;
  const key = `${hash}@${maxW}`;
  const hit = scaledCache.get(key);
  if (hit) { apply(hit, hash); return hit; }
  if (!scaleQueue.some((j) => j.key === key)) {
    scaleQueue.push({ key, hash, b64, maxW, apply });
    pumpScaleQueue();
  }
  return null;
}


export { CARD_MAX_W, CARD_ZOOM_BUCKETS, DECODE_CONCURRENCY, cardTargetWidth, decodeScaled, pumpScaleQueue, requestScaled, scaleQueue, scaleWorkers, scaledCache, screenshotSrc, shotAspect, truncate };
