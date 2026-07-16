$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command is missing: $Name"
    }
}

Write-Host '[1/7] Checking existing platform dependencies'
foreach ($command in @('git', 'docker', 'node', 'npm', 'uv', 'ollama', 'codex')) {
    Require-Command $command
}

if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
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
$previousQwenHome = $env:QWEN_HOME
try {
    $env:QWEN_HOME = Join-Path $Root 'config\qwen'
    $mcp = qwen mcp list 2>&1 | Out-String
    if (($mcp -notmatch 'context7') -or ($mcp -notmatch 'playwright')) {
        throw 'Platform Qwen config is missing Context7 or Playwright MCP'
    }
} finally {
    if ($null -eq $previousQwenHome) { Remove-Item Env:QWEN_HOME -ErrorAction SilentlyContinue }
    else { $env:QWEN_HOME = $previousQwenHome }
}

Write-Host '[3/7] Creating Python 3.12 environment'
uv python install 3.12
uv sync --python 3.12

Write-Host '[4/7] Installing Playwright module'
npm install
npx playwright install chromium

Write-Host '[5/7] Preparing fast and strong Ollama profiles'
$models = ollama list
if ($models -notmatch 'qwen3\.5:4b') {
    ollama pull qwen3.5:4b
}
if ($models -notmatch 'qwen3\.6:35b') {
    ollama pull qwen3.6:35b
}
ollama create local-fast -f models/fast.Modelfile
ollama create local-strong -f models/strong.Modelfile

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
