# Architecture & Design

## System Overview

```
┌─────────────┐
│ Android App │
└──────┬──────┘
       │ (USB/TCP)
       ▼
┌──────────────────────┐
│  adb_device.py       │ uiautomator2 wrapper
│  • screenshot()      │ (UI dump, coordinates)
│  • click(x, y)       │
│  • type(text)        │
│  • press_back()      │
└──────────────────────┘
       │
       │ (XML dump, JPEG)
       ▼
┌──────────────────────┐
│  extractor.py        │ State & action extraction
│  • compute_state_hash│ (SHA-256 of structure)
│  • extract_actions   │ (clickable elements)
└──────────────────────┘
       │
       │ (state hash, actions, screenshot)
       ▼
┌──────────────────────┐
│  graph.py            │ State graph management
│  • add_state()       │
│  • add_edge()        │
│  • get_unexplored()  │
└──────────────────────┘
       │
       │ (state telemetry)
       ▼
┌──────────────────────┐
│  telemetry.py        │ HTTP client
│  • post_state()      │ (sends to server)
└──────────────────────┘
       │
       │ (HTTP POST)
       ▼
┌──────────────────────┐
│  server.py           │ FastAPI + WebSocket
│  • POST /telemetry   │ (receives state logs)
│  • WS /ws            │ (broadcasts to dashboard)
└──────────────────────┘
       │
       │ (WebSocket JSON)
       ▼
┌──────────────────────┐
│  dashboard.html      │ Browser UI
│  • vis.js network    │ (interactive graph)
│  • live updates      │ (real-time state stream)
└──────────────────────┘
```

---

## State Representation

### State Hash (Structural Fingerprint)

A state is uniquely identified by a SHA-256 hash computed from the UI structure:

```python
signature_parts = [
    package_name,
    activity_name,
    # For each element in the XML tree:
    f"{class}:{resource_id}:{clickable}:{text_if_static}:{content_desc_if_static}"
]
hash = SHA256("|".join(signature_parts))
```

**Key insight**: Input text on interactive elements (input fields, search boxes) is **excluded** from the signature. This prevents "search 'a'" and "search 'ab'" from being separate states when the UI layout is identical.

### Why This Matters
- **Deduplicates input variations**: Typing 10 characters doesn't spawn 10 unique states
- **Counts structural changes only**: New UI elements, visibility changes, layout shifts = new state
- **Stable across time**: Same screen captured 1 minute apart = same hash (ignores clocks, counters)

### Example
```
Package: com.samsung.android.app.notes
Activity: MainActivity

UI Elements:
  - EditText (resource: search_input, clickable: true, text: "a") → signature: "EditText:search_input:true::"
  - Button (resource: send_btn, clickable: true, content-desc: "Send") → signature: "Button:send_btn:true::Send"
  - TextView (resource: title, clickable: false, text: "Notes") → signature: "TextView:title:false:Notes:"

Hash = SHA256("com.samsung.android.app.notes|MainActivity|EditText:search_input:true::|Button:send_btn:true::Send|TextView:title:false:Notes:")
```

The same screen with text changed from "a" to "ab" produces the **same hash** because text on the interactive `EditText` is excluded.

---

## Exploration Strategy

### State Discovery Loop

```python
graph = Graph()  # empty state graph

for step in range(MAX_STEPS):
    # 1. Capture current screen
    screenshot_bytes = device.screenshot()
    xml_dump = device.get_ui_dump()
    
    # 2. Compute state hash
    current_state = compute_state_hash(package, activity, xml_dump)
    
    # 3. Check if new
    if current_state not in graph.nodes:
        # New state discovered
        graph.add_node(current_state, {
            "screenshot": screenshot_bytes,
            "xml": xml_dump,
            "timestamp": now()
        })
        
        # Extract clickable elements
        actions = extract_actions(xml_dump, width, height)
        
        # Prioritize: unexplored actions first
        unexplored = [a for a in actions if not graph.has_edge(current_state, a.target_state)]
        actions_to_try = unexplored + [a for a in actions if a in graph.outgoing_edges(current_state)]
    else:
        # Seen state, use cached actions
        actions_to_try = graph.outgoing_edges(current_state)
    
    # 4. Pick next action
    if actions_to_try:
        action = pick_action(actions_to_try)  # greedy: unexplored first
        target_state = device.click(action.x, action.y)
        graph.add_edge(current_state, target_state, label=action.label)
        consecutive_backtracks = 0
    else:
        # Dead end: backtrack
        device.press_back()
        consecutive_backtracks += 1
        if consecutive_backtracks >= MAX_CONSECUTIVE_BACKTRACKS:
            # Restart app, reset backtrack counter
            device.launch_app(TARGET_PACKAGE)
            consecutive_backtracks = 0
    
    # 5. Report progress
    telemetry.post_state(current_state, action.label, screenshot_bytes)

return graph  # Final: N unique states, M edges
```

### Backtrack Logic

When the agent reaches a dead end (no unexplored actions):
1. Press BACK
2. If BACK fails to move to a new state, try again (up to 6 times)
3. After 6 backtracks without progress, restart the app
4. Reset counter, continue exploration

This balances **depth-first** (follow chains deep) with **breadth-first** (eventually try all paths).

---

## Action Extraction

### Element Filtering

From the XML UI dump, extract clickable/focusable elements. Filters applied:

```python
for element in xml_root.iter():
    # Must be interactive
    if not (element.get("clickable") == "true" or element.get("focusable") == "true"):
        continue
    
    # Must be enabled
    if element.get("enabled") == "false":
        continue
    
    # Ignore system chrome packages
    pkg = element.get("package", "")
    if pkg in BLOCKED_PACKAGES:
        continue
    
    # Bounds check: within screen and outside status bar / nav bar
    bounds = parse_bounds(element.get("bounds"))
    cx, cy = center_of(bounds)
    if not (0 <= cx <= width and top_bound <= cy <= bottom_bound):
        continue
    
    # Dedup by coordinate (two overlapping elements = one action)
    if (cx, cy) in seen_coords:
        continue
    
    # Add to actions list
    actions.append({
        "x": cx,
        "y": cy,
        "label": extract_label(element),
        "bounds": bounds,
        "resource_id": element.get("resource-id"),
        "clickable": element.get("clickable") == "true"
    })
```

### Label Generation

For each action, generate a human-readable label:
```python
label = element.get("text") or element.get("content-desc") or element.get("resource-id").split("/")[-1] or element.get("class").split(".")[-1]
```

Examples:
- `EditText` with `content-desc="Search notes"` → label: "Search notes"
- `Button` with `resource-id="android:id/button1"` → label: "button1"
- `View` with no label → label: "View"

---

## State Hashing Deep Dive

### Dynamic Content Filtering

Patterns that look dynamic (and thus ignored in the hash):

```python
_DYNAMIC_PATTERNS = [
    r"^\d{1,2}:\d{2}(:\d{2})?\s*(AM|PM|am|pm)?$",  # times like "10:42 AM"
    r"^\d{1,3}\s?%$",                                # percentages like "87%"
    r"^\d+$",                                        # counters like "42"
    r"^(Mon|Tue|...)[a-z]*,?\s",                    # weekday-prefixed dates
    r"^\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}$",         # date formats
]
```

Text matching these patterns is excluded from the hash, even on static labels. This prevents a clock changing from fragmenting the state.

### Text Component Logic

```python
is_interactive = (clickable == "true") or (focusable == "true")

# Interactive elements: exclude text (button with different labels = same structure)
# Static labels: include text (label "Login" vs "Logout" = different meaning)
if is_interactive or _is_dynamic(text):
    text_component = ""
else:
    text_component = text
```

**Result**: 
- `Button(label="OK")` + `Button(label="Cancel")` on same screen → **same state** (interactive buttons change per context)
- `TextView(label="Login")` + `TextView(label="Logout")` on same screen → **different states** (static labels convey meaning)

---

## Telemetry Flow

### State Posting

When a new state is discovered:

```json
{
  "step": 5,
  "state_hash": "a1b2c3d4e5f6...",
  "package": "com.example.app",
  "activity": "MainActivity",
  "action_label": "click 'Submit button'",
  "action_coords": [540, 671],
  "screenshot": "data:image/jpeg;base64,/9j/4AAQSkZJRgABA...",
  "timestamp": "2026-07-22T15:08:30.123456Z",
  "num_actions": 12
}
```

### Server Reception

The FastAPI server receives the POST, stores it in memory, and broadcasts via WebSocket:

```json
{
  "type": "state",
  "data": { ... same as above ... }
}
```

The dashboard (connected via WebSocket) updates in real-time:
- Add new node to graph
- Add edge if transitioning from previous state
- Display screenshot
- Update step counter

---

## Performance Characteristics

### Memory
- **Per state**: ~100 KB (1–3 MB for 43 states with screenshots)
- **Per edge**: ~1 KB (negligible)
- **Server overhead**: ~50 MB base + screenshot storage

### Time
- **Screenshot + XML dump**: 1–2 seconds (device I/O)
- **State hash computation**: <1 ms (lightweight string ops)
- **Action extraction**: <1 ms
- **Network post**: <500 ms (local network)
- **Total per step**: ~7–10 seconds

### Optimization Opportunities
- **Batch screenshots**: Capture N screenshots, process off-device
- **Skip redundant hashing**: Cache hash for known states
- **Parallel exploration**: Run multiple agents on same device (risky, requires coordination)
- **Screenshot compression**: Lower JPEG quality (70 vs 90)

---

## Graph Algorithms

### Layout & Visualization

The dashboard uses **vis.js** to render the state graph with a physics-based layout:

```javascript
var nodes = graph.nodes.map(n => ({
  id: n.state_hash,
  label: n.activity + " " + n.step,
  image: n.screenshot_url,  // native DOM img
  shape: 'image',
  useImageSize: true,
  title: `State ${n.state_hash}`
}));

var edges = graph.edges.map(e => ({
  from: e.from_state,
  to: e.to_state,
  label: e.action_label,
  smooth: { type: 'cubic', forceDirection: 'horizontal' }
}));

var network = new vis.Network(container, { nodes, edges }, options);
```

### Section Grouping

States are organized by `activity` (e.g., "MainActivity" vs "SearchActivity"). Nodes in the same activity are positioned together via **section headers** and implicit clustering in the physics simulation.

### Self-Loops

If an action returns to the same state (e.g., "click button, dialog closes, back to original screen"), the edge loops back on itself with a curved path.

---

## Error Handling

### Device Communication Failures
If a click or screenshot fails:
1. Log the error
2. Retry once
3. If retry fails, attempt device reset (disconnect/reconnect ADB)
4. If reset fails, abort exploration and report failure

### XML Parsing Errors
If the UI dump is malformed:
1. Fall back to a raw string hash (still distinguishes states, but less reliable)
2. Log warning but continue exploration
3. Try to recover on the next screen

### Network Failures (Telemetry)
If POST to server fails:
1. Queue the state locally
2. Retry on the next screen
3. If queue grows too large, drop oldest entries
4. Log warning

---

## Extension Points

### Custom State Hashing
Subclass or patch `extractor.py::compute_state_hash()` to:
- Ignore certain element types
- Weight elements by importance
- Use machine learning for semantic equivalence

### Custom Action Picking
Modify the exploration strategy in `run_agent.py::pick_action()` to:
- Prioritize certain element types (e.g., always click "menu" first)
- Use heuristics to avoid known-bad paths
- Explore high-value flows first (e.g., "create note" before "edit settings")

### Custom Telemetry
Subclass `telemetry.py::TelemetryClient` to:
- Send to a different server
- Add custom metadata (e.g., performance metrics)
- Integrate with CI/CD pipelines

---

## Testing the System

### Unit Tests
```bash
pytest tests/
```

### Integration Test (Calculator)
```bash
python run_agent.py --package com.sec.android.app.popupcalculator --steps 10
# Expected: 8–10 steps, 4–6 unique states
```

### Integration Test (Notes)
```bash
python run_agent.py --package com.samsung.android.app.notes --steps 50
# Expected: 50 steps, 30–50 unique states
```

---

**For more details, see the inline code comments in each `.py` file.**
