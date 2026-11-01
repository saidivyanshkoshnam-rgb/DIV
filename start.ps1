$project = $PSScriptRoot
$workspace = Split-Path -Parent $project
$pythonRoot = Join-Path $workspace 'work\uv-python'
$cache = Join-Path $workspace 'work\uv-cache'
$temp = Join-Path $workspace 'work\tmp'
New-Item -ItemType Directory -Path $temp -Force | Out-Null
$pythonExe = Join-Path $pythonRoot 'cpython-3.12.13-windows-x86_64-none\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'The local Python runtime is missing.' }
$env:UV_PYTHON_INSTALL_DIR = $pythonRoot
$env:UV_CACHE_DIR = $cache
$env:TEMP = $temp
$env:TMP = $temp
$env:UV_LINK_MODE = 'copy'
$env:STUDY_DAY_PORT = '8767'
uv run --offline --python $pythonExe --with sarvamai python (Join-Path $project 'app.py')
