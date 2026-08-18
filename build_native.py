import os
import shutil
import sys

try:
    import PyInstaller.__main__
except Exception:
    print("ERROR: PyInstaller is not installed for this Python interpreter.")
    print(f"Interpreter: {sys.executable}")
    print("Run: python -m pip install -r requirements.txt")
    sys.exit(1)

try:
    from PIL import Image
except Exception:
    Image = None

# Define base paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(BASE_DIR, "dist")
BUILD_DIR = os.path.join(BASE_DIR, "build")
DEPENDENCIES_DIR = os.path.join(BASE_DIR, ".dependencies")

# Clean previous builds
if os.path.exists(DIST_DIR):
    shutil.rmtree(DIST_DIR)
if os.path.exists(BUILD_DIR):
    shutil.rmtree(BUILD_DIR)

print(f"Building MotorBuster Native 2.0 from {BASE_DIR}")
print(f"Dependencies at {DEPENDENCIES_DIR}")

icon_path = os.path.join(BASE_DIR, "assets", "icon.ico")
icon_png_path = os.path.join(BASE_DIR, "assets", "icon.png")

if os.path.exists(icon_png_path) and Image is not None:
    try:
        with Image.open(icon_png_path) as image:
            image.save(icon_path, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
        print(f"Refreshed icon from PNG: {icon_png_path} -> {icon_path}")
    except Exception as e:
        print(f"Warning: Failed to refresh icon from PNG: {e}")

# Build Arguments
args = [
    os.path.join(BASE_DIR, 'native_app.py'), # Your main script
    '--name=MotorBuster',              # Executable name
    f'--icon={icon_path}', # Exe Icon
    f'--add-data={os.path.join(BASE_DIR, "assets")};assets',  # Bundle assets dir
    '--onedir',                  # Directory with exe and dependencies
    '--clean',                   # Clean cache
    '--windowed',                # No terminal window
    '--collect-all=dearpygui',   # Collect DPG assets
    '--collect-all=pysdl2_dll',  # Collect SDL2 dll wrapper if any
    '--collect-submodules=server', # Ensure server package is collected
    '--paths=.',                 # Add root to search path
    f'--paths={DEPENDENCIES_DIR}',  # Add .dependencies so PyInstaller can find dearpygui
    
    # Exclude unnecessary web/server stuff to save size
    '--exclude-module=uvicorn',
    '--exclude-module=fastapi',
    '--exclude-module=starlette',
    '--exclude-module=tkinter',
    '--hidden-import=server.ffb_engine', # Explicitly force import
    f'--distpath={DIST_DIR}',    # Force dist location
    f'--workpath={BUILD_DIR}',   # Force build temp location
    f'--specpath={BUILD_DIR}',   # Keep generated spec out of repo root
]

# Bundle SDL3 DLLs from server package
sdl3_dll = os.path.join(BASE_DIR, "server", "SDL3.dll")
if os.path.exists(sdl3_dll):
    print(f"Bundling SDL3.dll from: {sdl3_dll}")
    args.append(f'--add-binary={sdl3_dll};.')
else:
    print("WARNING: Could not find SDL3.dll in server/ directory!")

# Bundle other SDL3 libs if present (Image, Mixer, etc)
for fname in os.listdir(os.path.join(BASE_DIR, "server")):
    if fname.startswith("SDL3") and fname.endswith(".dll") and fname != "SDL3.dll":
        fpath = os.path.join(BASE_DIR, "server", fname)
        print(f"Bundling {fname}...")
        args.append(f'--add-binary={fpath};.')

# Bundle DirectInput provider DLL from project root if present
for di_name in ("DirectInputForceFeedback.dll",):
    di_path = os.path.join(BASE_DIR, di_name)
    if os.path.exists(di_path):
        print(f"Bundling {di_name} from: {di_path}")
        args.append(f'--add-binary={di_path};.')

# Force include the 'server' package dir
server_dir = os.path.join(BASE_DIR, "server")
if os.path.exists(server_dir):
     print(f"Bundling server package from: {server_dir}")
     args.append(f'--add-data={server_dir};server')

print(f"Running PyInstaller with: {args}")

PyInstaller.__main__.run(args)

# Post-Build Check
primary_exe_path = os.path.join(DIST_DIR, "MotorBuster", "MotorBuster.exe")

if os.path.exists(primary_exe_path):
    print(f"Build Complete: {primary_exe_path}")
else:
    print("Build Failed.")
