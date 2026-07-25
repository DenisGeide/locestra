$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. (Join-Path $PSScriptRoot 'process-ownership.ps1')

$gatewayKeyPath = Join-Path $Root 'run\gateway-api-key.txt'
$env:GATEWAY_API_KEY = if (Test-Path -LiteralPath $gatewayKeyPath) {
    [IO.File]::ReadAllText($gatewayKeyPath).Trim()
} else {
    'stop-only-placeholder'
}

$failures = 0
$mcpPython = Join-Path $Root '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $mcpPython) {
    & $mcpPython -m services.mcp_hub.cli stop | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning 'Managed MCP Hub refused to stop an unverified owner record'
        $failures++
    }
}
$ownedWorkers = @(
    @{ Name='gateway'; Port=8787; Fragments=@('uvicorn','services.gateway.app:app','--port','8787'); RequireRoot=$true },
    @{ Name='voice'; Port=8788; Fragments=@('uvicorn','services.voice.app:app','--port','8788'); RequireRoot=$true },
    @{ Name='telegram'; Port=0; Fragments=@('python.exe','-m services.telegram.bot'); RequireRoot=$true },
    @{ Name='ollama-fast'; Port=11435; Fragments=@('ollama.exe','serve'); RequireRoot=$false },
    @{ Name='comfyui'; Port=8388; Fragments=@('ComfyUI\main.py','--port','8388'); RequireRoot=$true }
)

foreach ($worker in $ownedWorkers) {
    try {
        $stopped = Stop-OwnedProcess `
            -Root $Root -Name $worker.Name -Port $worker.Port -Fragments $worker.Fragments `
            -RequireRootIdentity $worker.RequireRoot -Seconds 20 -AllowLegacy
        if ((-not $stopped) -and ($worker.Port -gt 0) -and (@(Get-ListenerProcessIds $worker.Port).Count -gt 0)) {
            Write-Warning "Listener on port $($worker.Port) remains because verified ownership was unavailable"
            $failures++
        }
    } catch {
        Write-Warning "Failed to stop owned worker $($worker.Name): $($_.Exception.Message)"
        $failures++
    }
}

docker compose down
if ($LASTEXITCODE -ne 0) {
    Write-Warning "docker compose down exited with code $LASTEXITCODE"
    $failures++
}

if ($failures -gt 0) {
    Write-Host 'LOCAL_AGENT_STOP_FAILED'
    exit 1
}
Write-Host 'LOCAL_AGENT_STOPPED'
