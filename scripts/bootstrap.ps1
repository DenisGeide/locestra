$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. (Join-Path $PSScriptRoot 'lib\platform-settings.ps1')

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command is missing: $Name"
    }
}

function Assert-OllamaModelDigest(
    [string]$Name,
    [string]$ExpectedDigest,
    [string]$Phase
) {
    if ($ExpectedDigest -notmatch '^[0-9a-fA-F]{64}$') {
        throw "Invalid pinned digest for Ollama model $Name"
    }
    $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 15
    $acceptedNames = @($Name)
    if ($Name -notmatch ':') {
        $acceptedNames += "${Name}:latest"
    }
    $matches = @(
        $tags.models | Where-Object { [string]$_.name -in $acceptedNames }
    )
    if ($matches.Count -ne 1) {
        throw "Expected one installed Ollama model for $Name during $Phase, received $($matches.Count)"
    }
    $actualDigest = ([string]$matches[0].digest).ToLowerInvariant()
    if ($actualDigest -ne $ExpectedDigest.ToLowerInvariant()) {
        throw "Ollama model drift for $Name during $Phase`: expected $ExpectedDigest, received $actualDigest"
    }
    Write-Host "[PASS] Ollama model identity - $Name $actualDigest ($Phase)" -ForegroundColor Green
}

if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
}

Write-Host '[1/7] Checking existing platform dependencies'
foreach ($command in @('git', 'docker', 'node', 'npm', 'uv', 'ollama')) {
    Require-Command $command
}
$codexExecutionRequired = Get-PlatformBooleanSetting `
    -Root $Root `
    -Name 'ENABLE_CODEX_EXEC' `
    -Default $false
if ($codexExecutionRequired) {
    Require-Command 'codex'
    $codexLogin = cmd.exe /d /c "codex login status 2>&1" | Out-String
    if (($LASTEXITCODE -ne 0) -or
        ($codexLogin -notmatch '(?im)^\s*Logged in(?:\s|$)')) {
        throw 'ENABLE_CODEX_EXEC=true requires an authenticated Codex CLI; run codex login and retry'
    }
    Write-Host '[PASS] Required Codex CLI is installed and authenticated' -ForegroundColor Green
} else {
    Write-Host '[INFO] Codex cloud execution is disabled; Codex CLI and login are optional' -ForegroundColor Cyan
}

$ollamaEnvironment = @{
    OLLAMA_MAX_LOADED_MODELS = '2'
    OLLAMA_NUM_PARALLEL = '1'
    OLLAMA_KEEP_ALIVE = '30m'
    OLLAMA_NO_CLOUD = '1'
}
foreach ($entry in $ollamaEnvironment.GetEnumerator()) {
    [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'User')
    Set-Item -Path "Env:$($entry.Key)" -Value $entry.Value
}

Write-Host '[2/7] Installing Qwen Code'
$qwenVersion = if (Get-Command qwen -ErrorAction SilentlyContinue) { (qwen --version 2>&1 | Out-String).Trim() } else { '' }
if ($qwenVersion -notmatch '0\.19\.10') {
    npm install -g '@qwen-code/qwen-code@0.19.10'
}

Write-Host '[3/7] Creating Python 3.12 environment'
uv python install 3.12
uv sync --python 3.12

Write-Host '[4/7] Installing Playwright module'
npm install
npx playwright install chromium
uv run python -m services.mcp_hub.cli validate
if ($LASTEXITCODE -ne 0) { throw 'Managed MCP Hub registry/source validation failed' }
uv run python -m services.mcp_hub.cli generate
if ($LASTEXITCODE -ne 0) { throw 'Managed MCP Hub view generation failed' }
uv run python -m services.mcp_hub.cli discover --server local-diagnostics --request-id bootstrap-local-mcp
if ($LASTEXITCODE -ne 0) { throw 'Required local MCP diagnostics failed' }

Write-Host '[5/7] Preparing fast and strong Ollama profiles'
$models = ollama list
if ($models -notmatch 'qwen3\.5:4b') {
    ollama pull qwen3.5:4b
}
if ($models -notmatch 'qwen3\.6:35b') {
    ollama pull qwen3.6:35b
}
$expectedStrongBaseDigest = '07d35212591fc27746f0a317c975a6d68754fb38e9053d82e25f06057af28522'
Assert-OllamaModelDigest `
    -Name 'qwen3.6:35b' `
    -ExpectedDigest $expectedStrongBaseDigest `
    -Phase 'after pull, before alias creation'

$codingPolicy = [IO.File]::ReadAllText((Join-Path $Root 'config\coding.json')) | ConvertFrom-Json
$expectedStrongAliasDigest = [string]$codingPolicy.local_semantic_expected_model_digest
ollama create local-fast -f models/fast.Modelfile
ollama create local-strong -f models/strong.Modelfile
Assert-OllamaModelDigest `
    -Name 'local-strong' `
    -ExpectedDigest $expectedStrongAliasDigest `
    -Phase 'after alias creation'

Write-Host '[6/7] Pulling UI and automation containers'
$gatewayKeyPath = Join-Path $Root 'run\gateway-api-key.txt'
$env:GATEWAY_API_KEY = if (Test-Path -LiteralPath $gatewayKeyPath) {
    [IO.File]::ReadAllText($gatewayKeyPath).Trim()
} else {
    'bootstrap-pull-only-placeholder'
}
docker compose pull

Write-Host '[7/7] Running static checks'
uv run pytest
if ($LASTEXITCODE -ne 0) { throw 'Python tests failed' }
npm run browser:health
if ($LASTEXITCODE -ne 0) { throw 'Playwright health test failed' }

Write-Host 'BOOTSTRAP_OK'
