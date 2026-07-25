$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Managed Python environment is missing; run scripts\bootstrap.ps1 first'
}
& $Python -m services.mcp_hub.cli generate | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Managed Qwen MCP view generation failed' }
$RuntimeHome = Join-Path $Root 'run\qwen-homes\qwen-platform'
$ExactMcpConfig = Join-Path $RuntimeHome 'settings.json'
if ($args.Count -gt 0 -and [string]$args[0] -eq 'mcp') {
    throw 'Use .venv\Scripts\python.exe -m services.mcp_hub.cli for managed MCP operations'
}
foreach ($argument in $args) {
    if ([string]$argument -match '^--(mcp-config|allowed-mcp-server-names)(=|$)') {
        throw "The project wrapper owns the managed MCP option: $argument"
    }
}
$previousQwenHome = $env:QWEN_HOME
$qwenExitCode = 1
try {
    $env:QWEN_HOME = $RuntimeHome
    & qwen --mcp-config $ExactMcpConfig --allowed-mcp-server-names local-diagnostics @args
    $qwenExitCode = $LASTEXITCODE
} finally {
    if ($null -eq $previousQwenHome) { Remove-Item Env:QWEN_HOME -ErrorAction SilentlyContinue }
    else { $env:QWEN_HOME = $previousQwenHome }
}
exit $qwenExitCode
