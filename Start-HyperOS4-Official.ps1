$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -LiteralPath $PSScriptRoot
if (-not $env:HYPEROS_AVD_WORKSPACE) {
    $env:HYPEROS_AVD_WORKSPACE = Join-Path $PSScriptRoot "work\os4-official"
}
$runtimeFile = Join-Path $env:HYPEROS_AVD_WORKSPACE "local\runtime.json"
if (-not (Test-Path $runtimeFile)) {
    [Console]::Error.WriteLine("Install the official OS4 Release first: .\Setup.cmd --bundle path\to\manifest.json")
    exit 1
}
& python (Join-Path $PSScriptRoot "scripts\launch.py") @args
