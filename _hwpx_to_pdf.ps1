# Convert HWPX → PDF using Hancom Office COM automation.
# This is the only way to get pixel-faithful layout out of HWPX:
# the real Hancom engine paints the page, we capture it as PDF.

param(
  [Parameter(Mandatory=$true)][string]$Src,
  [Parameter(Mandatory=$true)][string]$Dst
)

$ErrorActionPreference = 'Stop'
$inAbs  = [System.IO.Path]::GetFullPath($Src)
$outAbs = [System.IO.Path]::GetFullPath($Dst)
Write-Host "in : $inAbs"
Write-Host "out: $outAbs"

if (-not (Test-Path $inAbs)) { throw "Input not found: $inAbs" }

$hwp = New-Object -ComObject HWPFrame.HwpObject

# Bypass the file-path security dialog. The DLL is part of Hancom
# automation samples; falls back to silent allow if not present.
try {
  $hwp.RegisterModule('FilePathCheckDLL', 'FilePathChecker')
  Write-Host "RegisterModule OK"
} catch { Write-Host "RegisterModule skipped: $($_.Exception.Message.Split([Environment]::NewLine)[0])" }

# Hide window
try { $hwp.XHwpWindows.Item(0).Visible = $false } catch {}

Write-Host "Opening..."
# Hancom Open signature: Open(string filename, string Format, string arg)
$ext = [System.IO.Path]::GetExtension($inAbs).TrimStart('.').ToUpper()
if (-not $ext) { $ext = 'HWPX' }
$opened = $hwp.Open($inAbs, $ext, '')
Write-Host "Open returned: $opened (format=$ext)"

# Try SaveAs PDF — the simple path. If unsupported, fall back to
# the FileSaveAsPdf action.
$saved = $false
try {
  if ($hwp.SaveAs($outAbs, 'PDF')) { $saved = $true; Write-Host "SaveAs PDF: OK" }
} catch {
  Write-Host "SaveAs PDF failed: $($_.Exception.Message.Split([Environment]::NewLine)[0])"
}

if (-not $saved) {
  Write-Host "Trying FileSaveAsPdf action..."
  $action = $hwp.HAction
  $pset   = $hwp.HParameterSet.HFileOpenSave
  $action.GetDefault('FileSaveAsPdf', $pset.HSet)
  $pset.filename = $outAbs
  $pset.Format   = 'PDF'
  if ($action.Execute('FileSaveAsPdf', $pset.HSet)) { $saved = $true; Write-Host "FileSaveAsPdf: OK" }
  else { Write-Host "FileSaveAsPdf: failed" }
}

# Close cleanly
try { $hwp.Clear(1) } catch {}
try { $hwp.Quit() } catch {}
[System.Runtime.InteropServices.Marshal]::ReleaseComObject($hwp) | Out-Null
[System.GC]::Collect(); [System.GC]::WaitForPendingFinalizers()

if (Test-Path $outAbs) {
  $size = (Get-Item $outAbs).Length
  Write-Host "DONE: $outAbs ($size bytes)"
  exit 0
} else {
  Write-Host "FAILED: no PDF produced"
  exit 1
}
