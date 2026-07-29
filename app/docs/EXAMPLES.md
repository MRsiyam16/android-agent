# Examples & Workflows

## Example 1: Quick Survey of a Calculator App

### Goal
Map the basic flows of the Samsung Calculator app in under 2 minutes.

### Setup
```bash
# Terminal 1: Start server
python server.py

# Terminal 2: Run exploration with low step limit
python run_agent.py --package com.sec.android.app.popupcalculator --steps 10
```

### What Happens
1. Server starts on http://localhost:8000
2. Agent connects to device, checks screen/lock
3. Launches Calculator app
4. Discovers states: Main screen → numbers → operator clicks → result
5. Stops after 10 steps

### Expected Output
```
2026-07-22 15:08:30 run_agent INFO Exploration finished: 10 steps, 6 unique states, 10 edges
```

### Dashboard
Open http://localhost:8000 → see flow graph with:
- 6 boxes (states): Main calculator, "1 pressed", "2 pressed", "+", "result"
- 10 arrows (transitions): "click 1" → "1 pressed", "click +" → "operator ready", etc.
- Screenshots of each state

### Interpretation
- **6 unique states**: Calculator correctly deduplicates input variations (entering "123" and "456" = same state layout)
- **10 edges**: User took 10 actions; some revisit states (e.g., clicked "1" twice → same state twice)
- **Flow**: Linear calculator flow with no branching → simple app, low complexity

---

## Example 2: Deep Exploration of a Notes App

### Goal
Fully map the Samsung Notes app to find all reachable UI states and edge cases.

### Setup
```bash
# Terminal 1: Server (already running from Example 1)

# Terminal 2: Deeper exploration
python run_agent.py --package com.samsung.android.app.notes --steps 100
```

### Configuration Tuning
If the app is slow to respond, edit `config.py`:
```python
ACTION_SETTLE_SECONDS = 1.5  # Was 0.9, give app more time
SCREENSHOT_QUALITY = 70      # Lower quality for faster captures
```

### What Happens
1. Agent launches Notes app
2. Discovers main screen, create note, edit, search
3. Explores search variations (typing different characters)
4. Hits dead ends (no more unexplored actions) → backtracks
5. Restarts app after 6 backtracks → continues exploring other flows
6. Stops after 100 steps or exhausts all paths

### Expected Output
```
2026-07-22 15:10:45 run_agent INFO Exploration finished: 100 steps, 47 unique states, 100 edges
```

### Dashboard Insights
- **47 states**: Rich app with many distinct screens (main, list, create, edit, search results, settings, etc.)
- **100 edges**: Comprehensive coverage; multiple paths between screens
- **Branching patterns**: 
  - Main → create note → edit → save/cancel
  - Main → search → results → open note
  - Settings → various toggles → back to main

### Annotating Findings

While dashboard is live:
1. Click **text tool** (dock, left side)
2. Click on a state card → enter annotation (e.g., "Bug: title field accepts 1000 chars")
3. Click **comment pin** tool → click on screenshot → add note (e.g., "Verify: search crashes with emoji")
4. Click **Save** (top right) → download `flow-graph.json` with your annotations

### Re-importing Later
```bash
# Next day, pick up where you left off
python server.py
# Open http://localhost:8000
# Click Import → select previous flow-graph.json
# Now you can view/edit your findings without re-running
```

---

## Example 3: Comparative Testing (Before/After)

### Scenario
You have two versions of an app (v1.0 and v2.0). Find what UI changed.

### Step 1: Map v1.0
```bash
# Save to file
python run_agent.py --package com.app.v1 --steps 50
# Dashboard → Save → flow-v1.json
```

### Step 2: Map v2.0
```bash
# Install v2.0
adb install app-v2.0.apk

# Re-run exploration
python run_agent.py --package com.app.v2 --steps 50
# Dashboard → Save → flow-v2.json
```

### Step 3: Compare
```bash
# Pseudo-code (manual analysis)
v1_states = load_json(flow-v1.json)['nodes']
v2_states = load_json(flow-v2.json)['nodes']

v1_hashes = set(n['id'] for n in v1_states)
v2_hashes = set(n['id'] for n in v2_states)

new_states = v2_hashes - v1_hashes      # v2 only
removed_states = v1_hashes - v2_hashes  # v1 only

print(f"New UI: {len(new_states)} states")
print(f"Removed UI: {len(removed_states)} states")
```

### Outcome
- **New states**: v2 added a new feature (e.g., dark mode toggle)
- **Removed states**: v2 simplified a flow (e.g., removed "advanced settings")
- **Same states**: Core functionality unchanged

---

## Example 4: Targeted Flow Testing

### Scenario
Test a specific user journey: "Create a note, add tags, save, view in list, delete"

### Manual Intervention
1. Start exploration: `python run_agent.py --package com.app.notes --steps 50`
2. Watch dashboard live
3. When agent reaches "Notes list" screen, manually intervene:
   - Click "+" button (or let agent discover it)
   - Type "Test note"
   - Add tags manually on device (user action)
   - Click "Save"
4. Continue watching agent explore

### Capture This Flow
1. After exploration, open dashboard
2. Use **text tool** to label key states: "Create note" → "Add tags" → "Save" → "List view"
3. Use **comment pin** to mark bugs (e.g., "Tags input accepts 100+ chars, should limit to 20")
4. **Save** the annotated graph

### Export for CI/CD
```bash
# Download JSON, parse in your test pipeline
python -c "
import json
with open('flow-graph.json') as f:
    graph = json.load(f)
    
# Verify the 'Create note' → 'Save' path exists
edges = [e for e in graph['edges'] if 'Create note' in e['from'] and 'Save' in e['to']]
assert len(edges) > 0, 'Create/Save flow missing!'
print('✓ Critical flow exists')
"
```

---

## Example 5: Automated Regression Test

### Scenario
Run regression tests on every build. If the flow graph changes, fail the build.

### Script
```bash
#!/bin/bash
# ci-test.sh

set -e

# Build previous baseline (assumed in git)
git show main:flow-graph-baseline.json > /tmp/baseline.json

# Run exploration on new build
python run_agent.py --package com.myapp --steps 50 > /tmp/new.json

# Compare
python ci_compare.py /tmp/baseline.json /tmp/new.json

# If no diff, pass. If diff, fail CI.
# Output can trigger alerts, create issues, etc.
```

### `ci_compare.py`
```python
import json
import sys

def load_graph(path):
    with open(path) as f:
        return json.load(f)

baseline = load_graph(sys.argv[1])
current = load_graph(sys.argv[2])

base_ids = set(n['id'] for n in baseline['nodes'])
curr_ids = set(n['id'] for n in current['nodes'])

added = curr_ids - base_ids
removed = base_ids - curr_ids

if added or removed:
    print(f"❌ REGRESSION: {len(added)} states added, {len(removed)} removed")
    print(f"Added: {added}")
    print(f"Removed: {removed}")
    sys.exit(1)
else:
    print("✓ No regression: same number of states")
    sys.exit(0)
```

### CI Integration
```yaml
# .github/workflows/regression.yml
name: Regression Tests

on: [push]

jobs:
  regression:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Run exploration
        run: python run_agent.py --package com.myapp --steps 50 > /tmp/new.json
      - name: Compare against baseline
        run: python ci_compare.py flow-graph-baseline.json /tmp/new.json
```

---

## Example 6: Debugging a Flaky State

### Scenario
The app's "search results" screen sometimes shows up, sometimes doesn't. Need to understand why.

### Investigation
```bash
# Run multiple times with verbose logging
for i in {1..5}; do
    python run_agent.py --package com.app.search --steps 20 2>&1 | tee run-$i.log
    sleep 5  # Wait between runs
done
```

### Analyze Logs
```bash
# Check if "search results" state appears in all runs
for i in {1..5}; do
    if grep -q "search_results_state_hash" run-$i.log; then
        echo "Run $i: ✓ search results found"
    else
        echo "Run $i: ✗ search results NOT found"
    fi
done
```

### Findings
If some runs miss "search results", the state is **flaky**. Possible causes:
- Network latency (search takes >1s, agent clicks too fast)
- App crashes silently, restarts
- Search API returns empty results sometimes

### Fix
1. Increase `ACTION_SETTLE_SECONDS` to allow more time for search results to appear
2. Add retry logic to the agent: if search doesn't yield results, try again
3. Check device logs: `adb logcat | grep com.app.search`

---

## Example 7: Performance Profiling

### Goal
Understand which screens take longest to load.

### Modified run_agent.py
Add timing:
```python
import time

while step < MAX_STEPS:
    t0 = time.time()
    screenshot = device.screenshot()
    xml = device.get_ui_dump()
    t_capture = time.time() - t0
    
    t0 = time.time()
    state_hash = compute_state_hash(package, activity, xml)
    t_hash = time.time() - t0
    
    t0 = time.time()
    actions = extract_actions(xml, width, height)
    t_extract = time.time() - t0
    
    print(f"[Step {step}] State {state_hash[:8]}: "
          f"capture={t_capture:.2f}s hash={t_hash:.3f}s extract={t_extract:.3f}s")
    
    # ... rest of loop
```

### Output
```
[Step 1] State c25453e3: capture=1.23s hash=0.002s extract=0.015s
[Step 2] State 9fdbdcdb: capture=0.95s hash=0.001s extract=0.022s
[Step 3] State e4a3c7d9: capture=3.45s hash=0.003s extract=0.045s  ← SLOW
[Step 4] State f13f7981: capture=1.10s hash=0.002s extract=0.018s
```

### Analysis
State e4a3c7d9 takes 3.45s to capture (vs. ~1s typical). Likely causes:
- Complex UI with lots of elements
- Animation playing
- Network request loading
- Device is busy

### Action
- If it's a slow screen, increase `ACTION_SETTLE_SECONDS`
- If it's a loading screen, the agent may need to wait for content
- If it's a known heavy screen, skip it in exploration

---

## Example 8: Testing Accessibility (TalkBack)

### Scenario
Verify the app is navigable using Android's TalkBack (screen reader).

### Manual Setup
On device:
1. Settings → Accessibility → TalkBack → Enable
2. TalkBack reads labels aloud

### Exploration with TalkBack
The agent doesn't need special config—it works unchanged. But TalkBack's presence might change the UI:
- Additional voice output UI elements might appear
- Some elements might be hidden or repositioned

### Capturing Differences
```bash
# Run without TalkBack
adb shell settings put secure enabled_accessibility_services ""
python run_agent.py --package com.app --steps 30 > /tmp/no-talkback.json

# Run with TalkBack
adb shell settings put secure enabled_accessibility_services "com.google.android.marvin.talkback/com.google.android.marvin.talkback.TalkBackService"
python run_agent.py --package com.app --steps 30 > /tmp/with-talkback.json

# Compare states
python ci_compare.py /tmp/no-talkback.json /tmp/with-talkback.json
```

### Outcome
If the flow graphs differ, TalkBack changes the app's UI tree. Document these changes for a11y testing.

---

## Example 9: Testing on Multiple Devices

### Scenario
Verify the app works the same on 3 different devices (pixel, samsung, motorola).

### Setup
```bash
# List connected devices
adb devices

# Run exploration on each
for serial in $(adb devices | grep -v "^List" | awk '{print $1}'); do
    python run_agent.py --package com.app --serial "$serial" --steps 30 > /tmp/flow-$serial.json
    echo "Explored on $serial"
done
```

### Compare
```bash
# Are the flow graphs identical?
device1=$(ls /tmp/flow-*.json | head -1)
device2=$(ls /tmp/flow-*.json | tail -1)

python ci_compare.py "$device1" "$device2"
# If diff, the app behaves differently on these devices → investigate!
```

---

## Example 10: AI Agent Integration

### Scenario
Use this tool within another AI agent's workflow.

### Integration Code
```python
# ai_helper.py

import subprocess
import json
import time

def explore_app(package, steps=50, device_serial=None):
    """Run exploration and return flow graph."""
    
    cmd = ["python", "run_agent.py", "--package", package, "--steps", str(steps)]
    if device_serial:
        cmd.extend(["--serial", device_serial])
    
    result = subprocess.run(cmd, cwd=".", capture_output=True, text=True)
    
    if result.returncode != 0:
        raise RuntimeError(f"Exploration failed: {result.stderr}")
    
    # Parse output: "Exploration finished: 30 steps, 15 unique states, 30 edges"
    import re
    match = re.search(r"(\d+) steps, (\d+) unique states, (\d+) edges", result.stdout)
    if match:
        steps, states, edges = match.groups()
        return {
            "success": True,
            "steps": int(steps),
            "states": int(states),
            "edges": int(edges),
            "log": result.stdout
        }
    else:
        raise RuntimeError(f"Could not parse output: {result.stdout}")

def get_flow_graph():
    """Download the flow graph JSON from the dashboard."""
    import requests
    # Note: This endpoint doesn't exist yet; would require adding to server.py
    # response = requests.get("http://localhost:8000/api/graph")
    # return response.json()
    pass

def analyze_coverage(flow_graph):
    """Analyze how many UI states were discovered."""
    nodes = flow_graph.get("nodes", [])
    edges = flow_graph.get("edges", [])
    
    return {
        "total_states": len(nodes),
        "total_transitions": len(edges),
        "avg_branching": len(edges) / len(nodes) if nodes else 0,
    }

# Usage
if __name__ == "__main__":
    result = explore_app("com.example.app", steps=50)
    print(f"✓ Explored {result['states']} states in {result['steps']} steps")
    
    # Decide next action based on coverage
    if result['states'] < 10:
        print("⚠ Low coverage; recommend running with more steps or checking for crashes")
    elif result['states'] > 100:
        print("✓ High coverage; app is complex but well-explored")
    else:
        print("✓ Good coverage")
```

---

**For more help, check the main README.md or ARCHITECTURE.md!**
