$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location -LiteralPath $PSScriptRoot
& python (Join-Path $PSScriptRoot "scripts\manage.py") @args
