# 对拍①：golden 标注 vs 硬判分一致率（表格 + 落盘 runs\<ts>_agreement_*\）
# 用法（PowerShell，项目根目录）：
#   powershell -File scripts\run_agreement.ps1
#   $env:GOLDEN="<路径>"; $env:BATCH="<批次名>"; powershell -File scripts\run_agreement.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$batch = if ($env:BATCH) { $env:BATCH } else { "20260903_105240_strong_skilldriven_baseline_full" }

# 自动找最新 golden 产出
$golden = $env:GOLDEN
if (-not $golden) {
  $golden = Get-ChildItem "runs\*_golden\golden_output.jsonl" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
  if (-not $golden) {
    $golden = Get-ChildItem "sut\runs\golden\*\golden_output.jsonl" -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
  }
}
if (-not $golden) {
  Write-Error "找不到 golden 产出，请用 `$env:GOLDEN 指定"
  exit 1
}
Write-Host "golden: $golden"
Write-Host "批次:   runs\$batch"

python eval\reward_agreement.py $golden "runs\$batch"
