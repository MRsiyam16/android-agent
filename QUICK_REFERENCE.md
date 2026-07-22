# Quick Reference

## 30-Second Setup
```bash
pip install -r requirements.txt
python server.py &          # Start server (Ctrl+C to stop)
python run_agent.py --package com.app.name --steps 50
# Open http://localhost:8000 in browser
```

## Common Commands

### Explore an App
```bash
python run_agent.py --package com.example.app --steps 50
```

### Specify Device
```bash
adb devices  # List serials
python run_agent.py --package com.example.app --serial ABC123DEF45 --steps 50
```

### Quick Survey (10 states)
```bash
python run_agent.py --package com.example.app --steps 30
```

### Deep Exploration (100+ states)
```bash
python run_agent.py --package com.example.app --steps 200
```

### Start Server Separately
```bash
python server.py  # Runs on http://localhost:8000
```

### Check Connected Devices
```bash
adb devices
```

## Configuration Tweaks

### File: `config.py`

| Setting | Default | Adjust When |
|---------|---------|-------------|
| `MAX_STEPS` | 200 | Limit exploration length |
| `ACTION_SETTLE_SECONDS` | 0.9 | App slow? Increase to 1.5–2.0 |
| `SCREENSHOT_QUALITY` | 90 | Need faster speed? Lower to 70 |
| `EXCLUDE_TOP_PCT` | 0.05 | Ignore top 5% (status bar) |
| `EXCLUDE_BOTTOM_PCT` | 0.08 | Ignore bottom 8% (nav bar) |
| `BLOCKED_PACKAGES` | {...} | Add system apps to skip |

### Example: Slow App
```python
# config.py
ACTION_SETTLE_SECONDS = 2.0  # Wait 2s after each tap
SCREENSHOT_QUALITY = 70      # Faster captures
```

## Dashboard Controls

| Control | Action |
|---------|--------|
| **Left-click & drag** | Pan graph |
| **Scroll** | Zoom in/out |
| **Click node** | Show state details |
| **Pan tool** (dock) | Switch to pan mode (default) |
| **Select tool** (dock) | Select & drag multiple nodes |
| **Text tool** (dock) | Add text labels |
| **Comment tool** (dock) | Add pins with notes |
| **Settings (gear)** | Toggles (tap markers, headings, grid, auto-fit) |
| **Live preview** (right dock) | Show current device screen |
| **Save** (top-right) | Download flow graph as JSON |
| **Import** (top-right) | Load previously saved graph |

## File Locations

| File | Purpose |
|------|---------|
| `run_agent.py` | Main exploration loop |
| `server.py` | FastAPI telemetry server |
| `config.py` | Configuration (edit here) |
| `extractor.py` | State hashing & action extraction |
| `adb_device.py` | Device control wrapper |
| `templates/dashboard.html` | Web UI |
| `static/dashboard.js` | Interactive graph logic |
| `static/dashboard.css` | Styling |

## Output Interpretation

### Console Output
```
2026-07-22 15:08:30 run_agent INFO [step 1] state a1b2c3d4 -> click 'Login' @ (540,300)
2026-07-22 15:08:37 run_agent INFO [step 2] state 5e6f7g8h -> click 'Remember Me' @ (200,400)
...
2026-07-22 15:08:45 run_agent INFO Exploration finished: 2 steps, 2 unique states, 2 edges
```

**Read as**:
- Step 1: Clicked "Login" button → discovered new state
- Step 2: Clicked "Remember Me" → another new state
- Total: 2 interactions, 2 distinct UI screens, 2 transitions

### Dashboard Graph
- **Boxes**: States (UI screens)
- **Arrows**: Transitions (user actions)
- **Labels on arrows**: Action names (e.g., "click 'Submit'")
- **Width**: Number of states
- **Depth**: Branching complexity

**Good flow**: Linear or tree-like (organized)  
**Complex flow**: Dense graph (many interconnections)

## Troubleshooting

### Device Not Found
```bash
adb devices
# If empty, check:
# - USB cable connected
# - Developer mode enabled
# - USB debugging enabled
# - RSA key approved on device
```

### App Crashes During Exploration
Check device logs:
```bash
adb logcat | grep com.app.package
```
Look for exceptions. Increase `ACTION_SETTLE_SECONDS` to give app time to recover.

### Exploration Stuck (Repeating Same State)
Add verbose logging to `run_agent.py`:
```python
print(f"DEBUG: Current state: {current_state}")
print(f"DEBUG: Available actions: {actions_to_try}")
```
If no new actions discovered, app is fully explored.

### Screenshots Blurry
In `config.py`:
```python
SCREENSHOT_QUALITY = 100  # Max quality (slower)
```

### Slow Exploration
In `config.py`:
```python
ACTION_SETTLE_SECONDS = 0.5  # Faster (risky)
SCREENSHOT_QUALITY = 60      # Lower quality
```

## Performance Targets

| Metric | Target |
|--------|--------|
| Steps/minute | ~6–8 steps/min |
| States discovered | 20–50 for typical app |
| Memory usage | <500 MB |
| Dashboard load time | <2 seconds |

## Tips for Fast, Effective Exploration

1. **Start small**: `--steps 30` for 5-minute survey
2. **Monitor dashboard**: Watch real-time progress, stop early if complete
3. **Target core flows**: Ignore rarely-used screens
4. **Increase settle time for slow apps**: `ACTION_SETTLE_SECONDS = 1.5`
5. **Save early**: Download JSON after each run
6. **Annotate**: Use dashboard tools to mark findings
7. **Compare runs**: Use baseline graphs to detect regressions

## API Endpoints (Server)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Serve dashboard HTML |
| `/telemetry` | POST | Receive state logs from agent |
| `/status` | POST | Receive status messages |
| `/command` | POST | Send remote tap/click commands |
| `/ws` | WebSocket | Real-time state stream to dashboard |

## Environment Variables

```bash
ADB_PATH=/path/to/adb           # Override ADB path
SERVER_HOST=0.0.0.0              # Server binding address
SERVER_PORT=8000                 # Server port
SERVER_URL=http://localhost:8000 # Full server URL
TARGET_PACKAGE=com.app           # Default target package
MAX_STEPS=200                    # Default step limit
ALLOWED_PACKAGES=com.app,com.lib # Whitelist packages
```

Example:
```bash
ADB_PATH=/usr/local/bin/adb MAX_STEPS=100 python run_agent.py --package com.app
```

## Quick AI Integration

```python
import subprocess
result = subprocess.run([
    "python", "run_agent.py",
    "--package", "com.example.app",
    "--steps", "50"
], cwd="android-agent", capture_output=True, text=True)

if "Exploration finished" in result.stdout:
    # Success! Parse the output
    import re
    m = re.search(r"(\d+) steps, (\d+) unique states", result.stdout)
    steps, states = m.groups()
    print(f"Found {states} states in {steps} steps")
else:
    print("Failed:", result.stderr)
```

---

**For detailed docs, see README.md, ARCHITECTURE.md, or EXAMPLES.md**
