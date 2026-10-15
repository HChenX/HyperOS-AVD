$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -LiteralPath $PSScriptRoot
if (-not $env:HYPEROS_AVD_WORKSPACE) {
    $env:HYPEROS_AVD_WORKSPACE = $PSScriptRoot
}
& python (Join-Path $PSScriptRoot "scripts\launch.py") @args
