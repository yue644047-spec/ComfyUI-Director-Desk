param(
  [Parameter(Mandatory=$true)][string]$Dest,
  [switch]$Mini
)
# ============================================================
#  Ollama offline transfer pack: program + model store
#  Mini mode keeps only qwen3:8b (storyboard split) and
#  bge-m3 (asset similarity) - about 6.5 GB instead of 14 GB.
# ============================================================
$ErrorActionPreference = 'Stop'

$prog = Join-Path $env:LOCALAPPDATA 'Programs\Ollama'
$models = if ($env:OLLAMA_MODELS) { $env:OLLAMA_MODELS } else { 'D:\model' }
$lib = 'registry.ollama.ai\library'
$keep = @('qwen3\8b', 'bge-m3\latest')

if (-not (Test-Path $prog)) { Write-Host "[ERROR] Ollama program not found: $prog"; exit 1 }
if (-not (Test-Path (Join-Path $models 'blobs'))) { Write-Host "[ERROR] model store not found: $models (set OLLAMA_MODELS)"; exit 1 }

New-Item -ItemType Directory -Force (Join-Path $Dest 'Programs\Ollama') | Out-Null
Write-Host "Copying Ollama program  $prog"
robocopy $prog (Join-Path $Dest 'Programs\Ollama') /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Host "[ERROR] robocopy program failed: $LASTEXITCODE"; exit 1 }

$blobs = Join-Path $Dest 'model\blobs'
New-Item -ItemType Directory -Force $blobs | Out-Null
if ($Mini) {
  $need = New-Object System.Collections.Generic.List[string]
  foreach ($m in $keep) {
    $src = Join-Path (Join-Path $models "manifests\$lib") $m
    if (-not (Test-Path $src)) { Write-Host "[WARN] manifest missing: $src"; continue }
    $j = Get-Content $src -Raw | ConvertFrom-Json
    foreach ($d in @($j.config.digest) + @($j.layers.digest)) { if ($d) { $need.Add($d.Replace(':', '-')) } }
    $dst = Join-Path (Join-Path $Dest "model\manifests\$lib") $m
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
    Copy-Item $src $dst
  }
  $need = $need | Sort-Object -Unique
  Write-Host ("Copying {0} blobs (mini pack)" -f $need.Count)
  foreach ($b in $need) { Copy-Item (Join-Path (Join-Path $models 'blobs') $b) $blobs }
} else {
  Write-Host "Copying model store  $models"
  robocopy $models (Join-Path $Dest 'model') /E /NFL /NDL /NJH /NJS /NP | Out-Null
  if ($LASTEXITCODE -ge 8) { Write-Host "[ERROR] robocopy models failed: $LASTEXITCODE"; exit 1 }
}

Copy-Item (Join-Path $PSScriptRoot '迁移_Ollama本地AI.md') $Dest -ErrorAction SilentlyContinue
$size = (Get-ChildItem $Dest -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ""
Write-Host ("[OK] pack ready: {0}   {1:N1} GB" -f $Dest, ($size / 1GB))
Write-Host "Offline machine: 1) Programs\Ollama -> %LOCALAPPDATA%\Programs\Ollama"
Write-Host "                 2) model -> D:\model      3) setx OLLAMA_MODELS D:\model"
Write-Host "                 4) start the launcher bat (it starts ollama serve when 11434 is free)"
