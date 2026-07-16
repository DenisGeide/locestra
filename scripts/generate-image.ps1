param(
    [Parameter(Mandatory=$true)][string]$Prompt
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
& "$PSScriptRoot\start-images.ps1"

try {
    $workflow = Get-Content -Raw -Encoding UTF8 'services/images/workflow_sdxl_turbo.json' | ConvertFrom-Json
    $workflow.'6'.inputs.text = $Prompt
    $workflow.'3'.inputs.seed = Get-Random -Minimum 1 -Maximum 2147483647
    $body = @{ prompt = $workflow } | ConvertTo-Json -Depth 30 -Compress
    $queued = Invoke-RestMethod -Uri 'http://127.0.0.1:8388/prompt' -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 30
    $promptId = $queued.prompt_id
    if (-not $promptId) { throw "ComfyUI did not return prompt_id: $($queued | ConvertTo-Json -Depth 10)" }

    $deadline = (Get-Date).AddMinutes(10)
    do {
        Start-Sleep -Seconds 2
        $history = Invoke-RestMethod -Uri "http://127.0.0.1:8388/history/$promptId" -TimeoutSec 20
        $entry = $history.$promptId
        if ($entry) { break }
    } while ((Get-Date) -lt $deadline)
    if (-not $entry) { throw 'Image generation timeout' }

    $image = $entry.outputs.'9'.images | Select-Object -First 1
    if (-not $image.filename) { throw "No image in ComfyUI history: $($entry | ConvertTo-Json -Depth 20)" }
    $sourcePath = Join-Path $Root "modules\ComfyUI_windows_portable\ComfyUI\output\$($image.filename)"
    if (-not (Test-Path $sourcePath)) { throw "Generated image file not found: $sourcePath" }
    $publicName = "generated-$promptId.png"
    $path = Join-Path $Root "outputs\$publicName"
    Copy-Item -LiteralPath $sourcePath -Destination $path -Force
    Write-Output "IMAGE_PATH=$path"
    Write-Output "IMAGE_URL=http://127.0.0.1:8787/outputs/$publicName"
} finally {
    & "$PSScriptRoot\stop-images.ps1"
}
