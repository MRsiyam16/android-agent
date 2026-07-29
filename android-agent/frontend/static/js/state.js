// state.js — the board's shared data, extracted from the old dashboard.js IIFE.
//
// The collections below are exported directly: they are mutated in place, never reassigned,
// so every module holding the import sees the same Map.
//
// The scalars are different. They *are* reassigned — by viewport.js, tools.js, feed.js,
// board.js and screens.js — and an ES module may not assign to a name it imported. So they
// live as properties of `ui` and are read and written as `ui.currentTool`. That is the one
// mechanical difference between this file and the closure it came from; the alternative was
// a setter per field, which is the same indirection with more code.

const nodesData = new vis.DataSet([]);
const nodeMeta = new Map();      // state_hash -> { package, activity, elements, screenshot, action, section, _bakedFor }
const edgeMeta = new Map();      // edgeId -> { from, to, fullLabel }
const sections = new Map();      // sectionName -> [state_hash, ...] in first-seen order
const sectionOrder = [];         // section names in first-seen order
const cardElements = new Map();  // state_hash -> persistent DOM card element
const textNotes = new Map();     // id -> { id, cx, cy, text, fontSize }
const comments = new Map();      // id -> { id, hash, fracX, fracY, text }
// state_hash -> { level: 'fail' | 'warn', badge, summary }. Written by a reporting run
// (see scratch/test_yt_report.py) and persisted alongside the annotations, because it is a
// judgement about the run rather than something telemetry can derive on its own.
const nodeStatus = new Map();

const ui = {
  labelLength: 22,
  connectorPct: 100,
  observeMode: false,
  currentTool: 'pan',
  selectedIds: new Set(),
  lastElements: [],
  lastDims: { w: 1080, h: 1920 },
  currentSessionId: null,
  currentPackage: null,
  // Which project the screens currently on the board actually came from.
  //
  // This is deliberately *not* `currentPackage`. That one tracks "the app whose screen I am
  // looking at", and telemetry used to reassign it on every frame — so a deskclock run that
  // wandered into Chrome flipped it to Chrome, and the next autosave wrote the deskclock
  // board into Chrome's file. Four saved boards were destroyed that way. The board's owner
  // is set once, when a board is loaded or a run begins, and nothing on screen may change it.
  boardPackage: null,
  autoSaveTimer: null,
  showTapMarkers: true,
  showHeadings: true,
  showSectionNotes: true,
  autoFitOnNewState: false,

  // These five were declared beside the code that reads them and written from somewhere
  // else — chatPinned from three modules, statusHideTimer from two. In one closure that
  // was invisible; across modules it is an assignment to an imported binding, which the
  // browser refuses. Same category of state as the rest of this object, so same home.
  chatPinned: true,          // is the chat scrolled to the bottom (written by chat, transcript, main)
  pendingAttachments: [],    // images staged for the next message (written by compose)
  outcomeData: null,         // cached /outcomes response behind the four pills
  screenFilter: '',          // the screen-list search box
  statusHideTimer: null,     // pending auto-dismiss of the status banner

  // Two more of the same kind, and the reason this list is worth keeping honest.
  //
  // `transcriptRequest` was left as a `let` in outcomes.js and incremented from
  // transcript.js — `const token = ++transcriptRequest`. An imported binding is read-only,
  // so that line threw `TypeError: Assignment to constant variable.` on the *first* line of
  // loadTranscript, every time, and took the whole function with it: no chat history when
  // you clicked a module, no findings on the pills when a project opened, and no restored
  // "the agent is waiting for you" box after a reload — which is why answering a parked
  // question meant pressing Stop first. One misplaced `let`, three reported symptoms.
  transcriptRequest: 0,      // newest in-flight transcript load (transcript.js)
  outcomeRequest: 0,         // newest in-flight outcomes load (outcomes.js)
  // A token per endpoint, never one shared between two. Two fetches counting on the same
  // number invalidate each other's replies, which is the same stale-response bug the tokens
  // exist to prevent, only harder to see.
  noteRequest: 0,            // newest in-flight board-notes load (outcomes.js)
};

const PLACEHOLDER_IMG =
  'data:image/svg+xml;utf8,' + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="150" height="300">' +
    '<rect width="100%" height="100%" rx="14" fill="#1c1c1c"/>' +
    '<text x="50%" y="55%" fill="#5c5c5c" font-size="11" font-family="sans-serif" text-anchor="middle">loading…</text>' +
    '</svg>'
  );

export { PLACEHOLDER_IMG, cardElements, comments, edgeMeta, nodeMeta, nodeStatus, nodesData,
         sectionOrder, sections, textNotes, ui };
