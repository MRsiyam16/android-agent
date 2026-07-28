# Setup & Installation Guide

## Prerequisites

### System Requirements
- **Python 3.8+** (check with `python --version`)
- **Windows 10+**, **macOS 10.15+**, or **Linux** (Ubuntu 18.04+)
- **4+ GB RAM** (2 GB minimum)
- **USB 2.0+ port** or network access to Android device/emulator

### Android Device/Emulator
- **Android 6.0+** (API level 23+)
- **USB Debugging enabled** (Settings → Developer Options → USB Debugging)
- **USB Connection** or **ADB over TCP**

### Development Tools
- **ADB** (Android Debug Bridge)
- **Git** (optional, for version control)

---

## Installation Steps

### 1. Install Python
**Windows**:
```bash
# Download from https://www.python.org/downloads/
# During install, check "Add Python to PATH"
python --version  # Verify installation
```

**macOS**:
```bash
brew install python3
python3 --version
```

**Linux (Ubuntu/Debian)**:
```bash
sudo apt-get update
sudo apt-get install python3 python3-pip
python3 --version
```

### 2. Install ADB

**Windows**:
- Option A: Install Android Studio → includes ADB
- Option B: Download platform-tools from Android SDK
- Option C: Set `ADB_PATH` environment variable to adb.exe location

**macOS**:
```bash
brew install android-platform-tools
adb version  # Verify
```

**Linux**:
```bash
sudo apt-get install android-tools-adb
adb version  # Verify
```

### 3. Clone or Download This Repository

**Via Git**:
```bash
git clone https://github.com/yourusername/android-agent.git
cd android-agent
```

**Via ZIP**:
- Download ZIP from GitHub
- Extract to a folder
- Navigate to `android-agent/` in terminal

### 4. Install Python Dependencies

```bash
# Navigate to project directory
cd android-agent

# Install dependencies
pip install -r requirements.txt

# Verify installations
python -c "import uiautomator2; print('✓ uiautomator2')"
python -c "import fastapi; print('✓ fastapi')"
```

### 5. Verify ADB Connection

```bash
# List connected devices
adb devices

# Expected output:
# List of attached devices
# ABC123DEF456    device
```

If no devices listed:
- Connect USB cable
- On device: approve RSA key when prompted
- Retry `adb devices`

### 6. Enable USB Debugging on Device

**On Android Device**:
1. Settings → About Phone
2. Tap "Build Number" 7 times
3. Settings → Developer Options
4. Enable "USB Debugging"
5. On computer, authorize RSA key when prompted

---

## Quick Start

### Terminal 1: Start Server
```bash
python server.py
```
Expected output:
```
INFO:     Started server process
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### Terminal 2: Run Exploration
```bash
python run_agent.py --package com.example.app --steps 50
```
Expected output:
```
[...device connection logs...]
[step 1] state abc123 -> click 'Button' @ (540,300)
...
Exploration finished: 50 steps, 25 unique states, 50 edges
```

### Browser: Open Dashboard
Navigate to: **http://localhost:8000**

You should see:
- Flow graph (empty initially)
- Live updates as agent explores
- Screenshots appearing in real-time

---

## Troubleshooting Setup

### "Command not found: python"
On some systems, Python 3 requires `python3`:
```bash
python3 -m pip install -r requirements.txt
python3 run_agent.py --package com.example.app
```

### "adb: command not found"
ADB is not in PATH. Solutions:

**Option 1**: Add to PATH
```bash
# Find adb location
which adb  # macOS/Linux
# Add to PATH in ~/.bash_profile or ~/.zshrc
export PATH="/path/to/android/platform-tools:$PATH"
```

**Option 2**: Set ADB_PATH in config.py
```python
# config.py
ADB_PATH = "/full/path/to/adb"
```

**Option 3**: Use full path
```bash
/usr/local/bin/adb devices
```

### "Device not authorized"
On device, you'll see a prompt to authorize the computer's RSA key.
- Tap **Always Allow**
- Try `adb devices` again

### "Device not found"
- Check USB cable
- Try different USB port
- Restart device
- Run `adb kill-server && adb start-server`

### "ImportError: No module named 'uiautomator2'"
Dependencies not installed:
```bash
pip install -r requirements.txt
# If that fails, install individually:
pip install uiautomator2
pip install fastapi
pip install uvicorn
pip install pydantic
```

### "Address already in use: ('0.0.0.0', 8000)"
Port 8000 is occupied. Solutions:

**Option 1**: Kill existing process
```bash
# Windows
netstat -ano | findstr :8000
taskkill /PID <PID> /F

# macOS/Linux
lsof -i :8000
kill -9 <PID>
```

**Option 2**: Use different port
```python
# config.py
SERVER_PORT = 8001
```

---

## Advanced Setup

### Using Emulator Instead of Device

**Android Studio Emulator**:
```bash
# Start emulator
/path/to/android/emulator/emulator -avd Pixel_API_30

# Wait for boot, then connect
adb devices  # Should see emulator-5554
```

**Genymotion**:
```bash
# Start Genymotion VM
genymotion-app  # GUI

# ADB will auto-detect
adb devices
```

### Remote Device (Over Network)

If device is on same network:
```bash
# On device (via USB first time):
adb shell ip addr show wlan0  # Get IP, e.g., 192.168.1.100

# On computer:
adb connect 192.168.1.100:5555
adb devices  # Should show device

# Now run exploration over network
python run_agent.py --package com.app --serial 192.168.1.100:5555
```

### Custom ADB Path (Windows)

If ADB is in `C:\Android\platform-tools\adb.exe`:

```python
# config.py
ADB_PATH = r"C:\Android\platform-tools\adb.exe"
```

Or environment variable:
```bash
set ADB_PATH=C:\Android\platform-tools\adb.exe
python run_agent.py --package com.app
```

### Docker Deployment

Create a `Dockerfile`:
```dockerfile
FROM python:3.9

# Install ADB
RUN apt-get update && apt-get install -y android-tools-adb

# Copy code
COPY . /app
WORKDIR /app

# Install Python deps
RUN pip install -r requirements.txt

# Run server
CMD ["python", "server.py"]
```

Build and run:
```bash
docker build -t android-agent .
docker run -p 8000:8000 -v ~/.android:/root/.android android-agent
```

---

## Verification Checklist

After installation, verify everything works:

- [ ] Python installed: `python --version` ✓
- [ ] ADB available: `adb devices` ✓
- [ ] Device connected: Appears in `adb devices` output ✓
- [ ] Dependencies installed: `pip list | grep uiautomator2` ✓
- [ ] Server starts: `python server.py` (no errors) ✓
- [ ] Dashboard loads: Open http://localhost:8000 in browser ✓
- [ ] Agent runs: `python run_agent.py --package com.android.calculator` (completes without error) ✓

If any step fails, refer to **Troubleshooting** above.

---

## Next Steps

1. **Read the Quick Start**: `../README.md` → Section "Running for a Target App"
2. **Try an Example**: `EXAMPLES.md` → Example 1 (Calculator App)
3. **Explore Your App**: Pick a target app package, run `run_agent.py`
4. **Check the Dashboard**: Open http://localhost:8000 and watch live
5. **Annotate Findings**: Use dashboard tools to document flows
6. **Save Results**: Click "Save" to download flow graph JSON

---

## Performance Tuning (Optional)

### For Slow Device
```python
# config.py
ACTION_SETTLE_SECONDS = 2.0   # Wait longer after taps
```

### For Fast Device
```python
# config.py
ACTION_SETTLE_SECONDS = 0.5   # Faster interactions
SCREENSHOT_QUALITY = 60       # Lower quality for speed
```

### For Network-Based ADB
```python
# config.py
ACTION_SETTLE_SECONDS = 1.5   # Network latency
```

---

## Getting Help

### Documentation
- **Main README**: `../README.md` — the full guide, including the command cheat sheet
  and dashboard controls that used to live in a separate `QUICK_REFERENCE.md`
- **Architecture**: `ARCHITECTURE.md`
- **Examples**: `EXAMPLES.md`

### Common Issues
See **Troubleshooting Setup** section above.

### Community
- Open an issue on GitHub
- Check existing issues for solutions
- Contact maintainers

---

**Ready? Start with `README.md` → "Running for a Target App" section!** 🚀
