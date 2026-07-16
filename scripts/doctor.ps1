$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$script:Failures = 0
$script:Warnings = 0
$gatewayKeyPath = Join-Path $Root 'run\gateway-api-key.txt'
$script:GatewayHeaders = @{}
if (Test-Path -LiteralPath $gatewayKeyPath) {
    $script:GatewayHeaders.Authorization = 'Bearer ' + [IO.File]::ReadAllText($gatewayKeyPath).Trim()
}

function Check-Command([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { Write-Host "[PASS] command $Name - $($command.Source)" -ForegroundColor Green }
    else { Write-Host "[FAIL] command $Name is missing" -ForegroundColor Red; $script:Failures++ }
}

function Check-OptionalCommand([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { Write-Host "[PASS] optional command $Name - $($command.Source)" -ForegroundColor Green }
    else { Write-Host "[WARN] optional command $Name is missing" -ForegroundColor Yellow; $script:Warnings++ }
}

function Check-Url([string]$Name, [string]$Url) {
    try {
        $headers = if ($Url -match '127\.0\.0\.1:8787/v1/') { $script:GatewayHeaders } else { @{} }
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -Headers $headers -TimeoutSec 15
        if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) {
            Write-Host "[PASS] $Name - $Url" -ForegroundColor Green
        } else { throw "HTTP $($response.StatusCode)" }
    } catch {
        Write-Host "[FAIL] $Name - $Url - $($_.Exception.Message)" -ForegroundColor Red
        $script:Failures++
    }
}

function Invoke-KnowledgeCli([string[]]$KnowledgeArguments) {
    $output = & uv run --quiet python -m services.knowledge.cli @KnowledgeArguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Knowledge CLI exited with code $LASTEXITCODE`: $($output -join [Environment]::NewLine)"
    }
    try {
        return ($output -join [Environment]::NewLine) | ConvertFrom-Json
    } catch {
        throw "Knowledge CLI returned invalid JSON: $($output -join [Environment]::NewLine)"
    }
}

foreach ($command in @('docker','git','node','npm','python','uv','ollama','qwen','codex')) {
    Check-Command $command
}
foreach ($command in @('wsl','code')) { Check-OptionalCommand $command }

try {
    $gpu = nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
    if ($gpu) { Write-Host "[PASS] NVIDIA GPU - $gpu" -ForegroundColor Green }
    else { throw "No NVIDIA GPU was reported by nvidia-smi" }
} catch { Write-Host "[FAIL] GPU - $_" -ForegroundColor Red; $script:Failures++ }

try {
    $docker = docker info --format 'Server={{.ServerVersion}} CPUs={{.NCPU}} RAM={{.MemTotal}}' 2>$null
    Write-Host "[PASS] Docker - $docker" -ForegroundColor Green
} catch { Write-Host '[FAIL] Docker engine is not running' -ForegroundColor Red; $script:Failures++ }

try {
    $models = ollama list
    if (($models -match 'local-fast') -and ($models -match 'local-strong')) {
        $fastAlias = (ollama show local-fast --modelfile | Select-String '^FROM ').Line
        $fastBase = (ollama show qwen3.5:4b --modelfile | Select-String '^FROM ').Line
        $strongAlias = (ollama show local-strong --modelfile | Select-String '^FROM ').Line
        $strongBase = (ollama show qwen3.6:35b --modelfile | Select-String '^FROM ').Line
        $fastProfile = ollama show local-fast --modelfile | Out-String
        $strongProfile = ollama show local-strong --modelfile | Out-String
        if (($fastAlias -ne $fastBase) -or ($strongAlias -ne $strongBase) -or
            ($fastProfile -notmatch 'PARAMETER num_ctx 8192') -or
            ($fastProfile -notmatch 'PARAMETER num_gpu 0') -or
            ($strongProfile -notmatch 'PARAMETER num_ctx 32768')) {
            throw 'Ollama aliases do not match the expected base models/context sizes'
        }
        Write-Host '[PASS] Ollama profiles - qwen3.5 4B/8K + qwen3.6 35B/32K' -ForegroundColor Green
    } else { throw 'fast or strong Qwen model not found' }
} catch { Write-Host "[FAIL] Ollama model - $_" -ForegroundColor Red; $script:Failures++ }

try {
    $expectedOllamaEnvironment = @{
        OLLAMA_MAX_LOADED_MODELS = '2'
        OLLAMA_NUM_PARALLEL = '1'
        OLLAMA_KEEP_ALIVE = '30m'
        OLLAMA_NO_CLOUD = '1'
    }
    foreach ($entry in $expectedOllamaEnvironment.GetEnumerator()) {
        $actual = [Environment]::GetEnvironmentVariable($entry.Key, 'User')
        if ($actual -ne $entry.Value) { throw "$($entry.Key) expected $($entry.Value), received $actual" }
    }
    Write-Host '[PASS] Ollama server policy - serialized requests, 30m keep-alive, cloud disabled' -ForegroundColor Green
} catch { Write-Host "[FAIL] Ollama scheduler - $_" -ForegroundColor Red; $script:Failures++ }

Check-Url 'Ollama API' 'http://127.0.0.1:11434/api/tags'
Check-Url 'Fast Ollama CPU API' 'http://127.0.0.1:11435/api/tags'
Check-Url 'Gateway' 'http://127.0.0.1:8787/health'
Check-Url 'Gateway liveness' 'http://127.0.0.1:8787/health/live'
Check-Url 'Gateway readiness' 'http://127.0.0.1:8787/health/ready'
Check-Url 'Gateway OpenAI models' 'http://127.0.0.1:8787/v1/models'
Check-Url 'Voice module' 'http://127.0.0.1:8788/health'
Check-Url 'Open WebUI' 'http://127.0.0.1:3737/health'
Check-Url 'n8n' 'http://127.0.0.1:5678/healthz'

try {
    $gatewayHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8787/health' -TimeoutSec 15
    if (($gatewayHealth.status -eq 'ok') -and $gatewayHealth.fast_model_present -and $gatewayHealth.strong_model_present -and
        ($gatewayHealth.health.schema_version -eq '1.0') -and $gatewayHealth.health.live -and $gatewayHealth.health.ready) {
        Write-Host "[PASS] Gateway contract health - ready, canonical status=$($gatewayHealth.health.status)" -ForegroundColor Green
    } else { throw ($gatewayHealth | ConvertTo-Json -Depth 5) }
} catch { Write-Host "[FAIL] Gateway contract health - $_" -ForegroundColor Red; $script:Failures++ }

try {
    $routeChecks = @{
        fast_chat = 'What is dependency injection?'
        strong_chat = 'Perform a deep analysis of this business strategy and trade-offs'
        local_code = 'Project: C:\tmp\app; create hello.py'
        codex = 'Project: C:\tmp\app; perform a security review before merge'
        auxiliary = 'Generate a concise title for this chat: generate image'
    }
    foreach ($expected in $routeChecks.Keys) {
        $encoded = [Uri]::EscapeDataString($routeChecks[$expected])
        $actual = (Invoke-RestMethod -Uri "http://127.0.0.1:8787/v1/route?text=$encoded" -Headers $script:GatewayHeaders -TimeoutSec 15).route
        if ($actual -ne $expected) { throw "expected $expected, received $actual" }
    }
    Write-Host '[PASS] Gateway automatic routing tiers' -ForegroundColor Green
} catch { Write-Host "[FAIL] Gateway routing - $_" -ForegroundColor Red; $script:Failures++ }

$comfyPython = 'modules\ComfyUI_windows_portable\python_embeded\python.exe'
$imageModel = 'modules\ComfyUI_windows_portable\ComfyUI\models\checkpoints\sd_xl_turbo_1.0_fp16.safetensors'
if ((Test-Path $comfyPython) -and (Test-Path $imageModel)) {
    Write-Host '[PASS] ComfyUI portable and SDXL Turbo model' -ForegroundColor Green
} else {
    Write-Host '[FAIL] ComfyUI portable or SDXL Turbo model is missing' -ForegroundColor Red
    $script:Failures++
}

try {
    $previousQwenHome = $env:QWEN_HOME
    try {
        $env:QWEN_HOME = Join-Path $Root 'config\qwen'
        $mcp = qwen mcp list 2>&1
    } finally {
        if ($null -eq $previousQwenHome) { Remove-Item Env:QWEN_HOME -ErrorAction SilentlyContinue }
        else { $env:QWEN_HOME = $previousQwenHome }
    }
    if (($mcp -match 'context7') -and ($mcp -match 'playwright')) {
        Write-Host '[PASS] Platform Qwen Code config - Ollama, Context7 and Playwright' -ForegroundColor Green
    } else { throw ($mcp -join "`n") }
} catch { Write-Host "[FAIL] Qwen Code MCP - $_" -ForegroundColor Red; $script:Failures++ }

try {
    $browser = npm run --silent browser:health 2>&1
    if ($browser -match 'PLAYWRIGHT_OK') { Write-Host '[PASS] Playwright Chromium' -ForegroundColor Green }
    else { throw ($browser -join "`n") }
} catch { Write-Host "[FAIL] Playwright - $_" -ForegroundColor Red; $script:Failures++ }

try { Write-Host "[PASS] Qwen Code - $(qwen --version)" -ForegroundColor Green }
catch { Write-Host '[FAIL] Qwen Code version check' -ForegroundColor Red; $script:Failures++ }

try { Write-Host "[PASS] Codex - $(codex --version)" -ForegroundColor Green }
catch { Write-Host '[FAIL] Codex version check' -ForegroundColor Red; $script:Failures++ }

try {
    $codexLogin = cmd.exe /d /c "codex login status 2>&1" | Out-String
    if ($codexLogin -match 'Logged in') { Write-Host "[PASS] Codex cloud authentication - $($codexLogin.Trim())" -ForegroundColor Green }
    else { throw $codexLogin.Trim() }
} catch { Write-Host "[FAIL] Codex cloud authentication - $_" -ForegroundColor Red; $script:Failures++ }

try {
    & python (Join-Path $PSScriptRoot 'validate_foundation.py') --root $Root
    if ($LASTEXITCODE -ne 0) { throw "foundation validator exited with code $LASTEXITCODE" }
    Write-Host '[PASS] Governance and architecture foundation validator' -ForegroundColor Green
} catch {
    Write-Host "[FAIL] Governance and architecture foundation validator - $_" -ForegroundColor Red
    $script:Failures++
}

$knowledgeDoctorRoot = Join-Path ([IO.Path]::GetTempPath()) ("local-agent-knowledge-doctor-" + [Guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $knowledgeDoctorRoot -Force | Out-Null
    $knowledgeDatabase = Join-Path $knowledgeDoctorRoot 'knowledge.sqlite3'
    $memoryDatabase = Join-Path $knowledgeDoctorRoot 'memory.sqlite3'
    $knowledgeStatus = Invoke-KnowledgeCli @(
        '--database', $knowledgeDatabase,
        '--memory-database', $memoryDatabase,
        'status'
    )
    if (($knowledgeStatus.schema_version -ne 1) -or
        ($knowledgeStatus.application_id -ne 1279347019) -or
        ($knowledgeStatus.integrity_check -ne 'ok') -or
        (-not $knowledgeStatus.fts5) -or
        ($knowledgeStatus.counts.knowledge_projects -ne 0) -or
        ($knowledgeStatus.counts.knowledge_sources -ne 0)) {
        throw ($knowledgeStatus | ConvertTo-Json -Depth 6)
    }
    Write-Host '[PASS] Knowledge Engine migration, application identity, FTS5, and integrity' -ForegroundColor Green
} catch {
    Write-Host "[FAIL] Knowledge Engine migration/status - $_" -ForegroundColor Red
    $script:Failures++
} finally {
    Remove-Item -LiteralPath $knowledgeDoctorRoot -Recurse -Force -ErrorAction SilentlyContinue
}

if ($script:Failures -eq 0) {
    Write-Host 'DOCTOR_OK' -ForegroundColor Green
    exit 0
}
Write-Host "DOCTOR_FAILED failures=$script:Failures warnings=$script:Warnings" -ForegroundColor Red
exit 1
