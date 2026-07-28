# Documentation Index

**Android App Testing Agent — Complete Documentation**

Welcome! This is an autonomous Android UI exploration system for mapping app flows and discovering UI states. Start here to understand what this tool does and how to use it.

---

## 📖 Documentation Files

### For First-Time Users

1. **[SETUP.md](SETUP.md)** — Installation & Environment Setup
   - Step-by-step installation guide
   - Troubleshooting common setup issues
   - Verify everything works before starting

2. **[README.md](README.md)** — Main Documentation (Read This First!)
   - Overview of what the tool does
   - Quick 30-second setup
   - How to run the agent
   - How to use the dashboard
   - Configuration options
   - Best practices for fast & effective exploration

3. **[QUICK_REFERENCE.md](QUICK_REFERENCE.md)** — Cheat Sheet
   - Common commands
   - Configuration tweaks
   - Dashboard controls
   - Troubleshooting quick fixes
   - Performance targets

---

### For In-Depth Understanding

4. **[ARCHITECTURE.md](ARCHITECTURE.md)** — Technical Deep Dive
   - System architecture diagram
   - State representation & hashing algorithm
   - Exploration strategy explained
   - Action extraction logic
   - Performance characteristics
   - Extension points for customization

5. **[EXAMPLES.md](EXAMPLES.md)** — Real-World Workflows
   - 10 detailed examples:
     - Example 1: Quick survey (Calculator app)
     - Example 2: Deep exploration (Notes app)
     - Example 3: Comparative testing (v1.0 vs v2.0)
     - Example 4: Targeted flow testing
     - Example 5: Automated regression tests
     - Example 6: Debugging flaky states
     - Example 7: Performance profiling
     - Example 8: Testing with accessibility (TalkBack)
     - Example 9: Multiple device testing
     - Example 10: AI agent integration

---

## 🚀 Getting Started (5 Minutes)

### Step 1: Install
```bash
cd android-agent
pip install -r requirements.txt
```
→ See [SETUP.md](SETUP.md) for detailed instructions

### Step 2: Connect Device
```bash
adb devices  # Verify your device is listed
```

### Step 3: Start Server
```bash
python server.py
```

### Step 4: Run Exploration
```bash
# In another terminal
python run_agent.py --package com.example.app --steps 50
```

### Step 5: View Results
Open http://localhost:8000 in browser

---

## 📋 File Structure

```
android-agent/
│
├── Documentation (READ THESE FIRST)
│   ├── INDEX.md                     ← You are here
│   ├── SETUP.md                     ← Installation guide
│   ├── README.md                    ← Main docs
│   ├── QUICK_REFERENCE.md           ← Cheat sheet
│   ├── ARCHITECTURE.md              ← Technical details
│   └── EXAMPLES.md                  ← Real examples
│
├── Core Code
│   ├── run_agent.py                 ← Main exploration loop
│   ├── server.py                    ← FastAPI telemetry server
│   ├── config.py                    ← Configuration (EDIT THIS)
│   ├── extractor.py                 ← State hashing & actions
│   ├── adb_device.py                ← Device control
│   ├── graph.py                     ← Graph structures
│   └── telemetry.py                 ← HTTP client
│
├── Frontend
│   ├── templates/
│   │   └── dashboard.html           ← Web UI
│   └── static/
│       ├── dashboard.js             ← Graph interaction
│       └── dashboard.css            ← Styling
│
└── Config
    ├── requirements.txt             ← Python dependencies
    └── .gitignore                   ← Git config
```

---

## 🎯 Choose Your Path

### "I just want to explore an app quickly"
1. Read [SETUP.md](SETUP.md) (5 min install)
2. Follow [README.md](README.md) → "Running for a Target App" (5 min)
3. Open dashboard → See results

**Total: ~15 minutes** ✓

### "I want to understand how it works"
1. Read [README.md](README.md) → Overview & Architecture sections
2. Skim [ARCHITECTURE.md](ARCHITECTURE.md) → State Representation & Exploration Strategy
3. Walk through [EXAMPLES.md](EXAMPLES.md) → Example 1 & 2

**Total: ~30 minutes** ✓

### "I'm integrating this into a CI/CD pipeline"
1. Read [SETUP.md](SETUP.md) → Advanced Setup (Docker)
2. Study [EXAMPLES.md](EXAMPLES.md) → Example 5 (Regression Tests) & Example 10 (AI Integration)
3. Review [ARCHITECTURE.md](ARCHITECTURE.md) → Error Handling & Performance
4. Reference [QUICK_REFERENCE.md](QUICK_REFERENCE.md) → API Endpoints

**Total: ~1 hour** ✓

### "I need to customize the state hashing or exploration strategy"
1. Read [ARCHITECTURE.md](ARCHITECTURE.md) → State Hashing Deep Dive & Extension Points
2. Review source code comments in:
   - `extractor.py` → `compute_state_hash()`
   - `run_agent.py` → `pick_action()`
3. Modify & test in isolation

**Total: ~2 hours** ✓

---

## 🔧 Common Tasks

| Task | Docs |
|------|------|
| **Install for the first time** | [SETUP.md](SETUP.md) |
| **Run exploration on an app** | [README.md](README.md) → "Running for a Target App" |
| **Use the dashboard** | [README.md](README.md) → "Dashboard (Live Visualization)" |
| **Understand how states are identified** | [ARCHITECTURE.md](ARCHITECTURE.md) → "State Representation" |
| **Debug a flaky state** | [EXAMPLES.md](EXAMPLES.md) → Example 6 |
| **Add regression tests to CI** | [EXAMPLES.md](EXAMPLES.md) → Example 5 |
| **Change configuration** | [README.md](README.md) → "Configuration" + [QUICK_REFERENCE.md](QUICK_REFERENCE.md) |
| **Profile performance** | [EXAMPLES.md](EXAMPLES.md) → Example 7 |
| **Integrate with my AI agent** | [EXAMPLES.md](EXAMPLES.md) → Example 10 + [README.md](README.md) → "For AI Agents" |
| **Quick fix for a problem** | [QUICK_REFERENCE.md](QUICK_REFERENCE.md) → "Troubleshooting" |

---

## 💡 Key Concepts

### State
A unique UI screen identified by a **structural hash** of the UI tree. Input text is excluded (so "search 'a'" and "search 'ab'" = same state).

### Edge
A transition from one state to another, triggered by an action (tap, type, back). Labeled with the action name.

### Flow Graph
A directed graph where:
- **Nodes** = states (UI screens)
- **Edges** = transitions (user actions)
- **Visualization** = Interactive graph on the dashboard

### Exploration
Autonomous process: take screenshot → identify state → extract actions → pick unexplored action → repeat.

### Telemetry
Real-time HTTP posts from agent to server, which broadcasts via WebSocket to the browser dashboard.

---

## 🎯 Quick Answers

**Q: How long does exploration take?**  
A: ~7–10 seconds per step. 50 steps ≈ 6–8 minutes. Depends on app speed & device.

**Q: How many states can be discovered?**  
A: Typically 20–50 for apps like Notes or Calculator. Complex apps may reach 100+.

**Q: Can I run on multiple devices at once?**  
A: Yes, use `--serial` to target specific devices. Each needs its own server port (edit `config.py`).

**Q: What if the app crashes?**  
A: The agent logs it and restarts the app. It continues exploring from a safe state.

**Q: Can I save & reload results?**  
A: Yes! Click "Save" on the dashboard → `flow-graph.json`. Click "Import" to reload later.

**Q: How do I integrate this into CI/CD?**  
A: See [EXAMPLES.md](EXAMPLES.md) → Example 5. Run `run_agent.py` in your pipeline, save JSON, compare against baseline.

**Q: Can I annotate the results?**  
A: Yes! Use the dashboard's text tool, comment pins, and select tool to annotate states with findings.

---

## 📞 Support & Issues

### Before Asking
1. Check [QUICK_REFERENCE.md](QUICK_REFERENCE.md) → Troubleshooting
2. Search [EXAMPLES.md](EXAMPLES.md) for a similar scenario
3. Review [ARCHITECTURE.md](ARCHITECTURE.md) for technical details

### Report an Issue
- Check device logs: `adb logcat | grep <app-package>`
- Enable verbose logging in `run_agent.py`
- Open an issue on GitHub with:
  - Device model & Android version
  - App package name
  - Steps to reproduce
  - Console output & error logs

### Request a Feature
- Open an issue titled "[Feature Request] ..."
- Describe the use case
- Suggest an implementation approach

---

## 📚 Reading Order (Recommended)

1. **Absolute beginner?** Start here:
   - [SETUP.md](SETUP.md) (5 min)
   - [README.md](README.md) → Overview & Quick Start (10 min)
   - [EXAMPLES.md](EXAMPLES.md) → Example 1 (5 min)
   - Try it! (10 min)
   - **Total: 30 min** ✓

2. **Want to understand how it works?**
   - [README.md](README.md) (full, 20 min)
   - [ARCHITECTURE.md](ARCHITECTURE.md) → State Representation & Exploration Strategy (15 min)
   - [EXAMPLES.md](EXAMPLES.md) → Example 2 & 3 (10 min)
   - **Total: 45 min** ✓

3. **Advanced user (CI/CD, customization)?**
   - [README.md](README.md) → Best Practices & Configuration (15 min)
   - [ARCHITECTURE.md](ARCHITECTURE.md) → Extension Points (10 min)
   - [EXAMPLES.md](EXAMPLES.md) → Example 5, 7, 10 (15 min)
   - Modify & test (30 min+)
   - **Total: 1–2 hours** ✓

---

## 🚀 Next Steps

1. **Install**: Follow [SETUP.md](SETUP.md)
2. **Learn**: Read [README.md](README.md)
3. **Try**: Run an example from [EXAMPLES.md](EXAMPLES.md)
4. **Explore**: Pick your target app and run `run_agent.py`
5. **Save & Share**: Use the dashboard to save your findings

---

**Happy exploring! 🎉**

*For questions, see Support & Issues section above or check [QUICK_REFERENCE.md](QUICK_REFERENCE.md).*
