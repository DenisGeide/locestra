$ErrorActionPreference = 'Stop'
$output = & "$PSScriptRoot\generate-image.ps1" -Prompt 'A small friendly robot writing code at a desk, clean digital illustration'
$line = $output | Where-Object { $_ -like 'IMAGE_PATH=*' } | Select-Object -Last 1
if (-not $line) { throw "Image module did not return a path: $output" }
$path = $line.Substring('IMAGE_PATH='.Length)
if (-not (Test-Path $path)) { throw "Generated image is missing: $path" }
$file = Get-Item $path
if ($file.Length -lt 10000) { throw "Generated image is unexpectedly small: $($file.Length) bytes" }
Write-Host "IMAGE_SMOKE_OK $path"
