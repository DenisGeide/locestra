$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. (Join-Path $PSScriptRoot 'lib\platform-settings.ps1')
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

function Get-HttpErrorStatusCode($ErrorRecord) {
    try {
        return [int]$ErrorRecord.Exception.Response.StatusCode
    } catch {
        return 0
    }
}

function Check-OpenAiAuthBoundary([string]$Name, [string]$Url) {
    if (-not $script:GatewayHeaders.Authorization) {
        Write-Host "[FAIL] $Name - generated credential is unavailable" -ForegroundColor Red
        $script:Failures++
        return
    }
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 15 | Out-Null
        Write-Host "[FAIL] $Name - unauthenticated request was accepted" -ForegroundColor Red
        $script:Failures++
        return
    } catch {
        if ((Get-HttpErrorStatusCode $_) -ne 401) {
            Write-Host "[FAIL] $Name - unauthenticated request did not return HTTP 401" -ForegroundColor Red
            $script:Failures++
            return
        }
    }
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -Headers $script:GatewayHeaders -TimeoutSec 15
        $statusCode = [int]$response.StatusCode
    } catch {
        $statusCode = Get-HttpErrorStatusCode $_
    }
    if ($statusCode -eq 401 -or $statusCode -lt 200 -or $statusCode -ge 500) {
        Write-Host "[FAIL] $Name - authenticated request returned HTTP $statusCode" -ForegroundColor Red
        $script:Failures++
        return
    }
    Write-Host "[PASS] $Name - unauthenticated=401 authenticated=$statusCode" -ForegroundColor Green
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

function Invoke-CodingCli([string[]]$CodingArguments) {
    $output = & uv run --quiet python -m services.coding.cli @CodingArguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Coding CLI exited with code $LASTEXITCODE`: $($output -join [Environment]::NewLine)"
    }
    try {
        return ($output -join [Environment]::NewLine) | ConvertFrom-Json
    } catch {
        throw "Coding CLI returned invalid JSON: $($output -join [Environment]::NewLine)"
    }
}

$script:CodexExecutionRequired = $false
try {
    $script:CodexExecutionRequired = Get-PlatformBooleanSetting `
        -Root $Root `
        -Name 'ENABLE_CODEX_EXEC' `
        -Default $false
    $codexMode = if ($script:CodexExecutionRequired) { 'required' } else { 'optional/disabled' }
    Write-Host "[PASS] Codex execution policy - $codexMode" -ForegroundColor Green
} catch {
    Write-Host "[FAIL] Codex execution policy - $_" -ForegroundColor Red
    $script:Failures++
}

foreach ($command in @('docker','git','node','npm','python','uv','ollama','qwen')) {
    Check-Command $command
}
if ($script:CodexExecutionRequired) { Check-Command 'codex' }
else { Check-OptionalCommand 'codex' }
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
    $expectedQwenImage = 'ghcr.io/qwenlm/qwen-code:0.19.10@sha256:03456a270da8d1bf1f1d5e6bf5e340718b595355b68649e0f6940cb7ff8dbeda'
    $imageId = docker image inspect $expectedQwenImage --format '{{.Id}}' 2>$null
    if (($LASTEXITCODE -ne 0) -or ($imageId -ne 'sha256:03456a270da8d1bf1f1d5e6bf5e340718b595355b68649e0f6940cb7ff8dbeda')) {
        throw "Pinned Qwen sandbox image/digest is unavailable or mismatched: $imageId"
    }
    $expectedVerifierBase = 'python@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7'
    $verifierBaseId = docker image inspect $expectedVerifierBase --format '{{.Id}}' 2>$null
    if (($LASTEXITCODE -ne 0) -or ($verifierBaseId -ne 'sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7')) {
        throw "Pinned Python verifier base image/digest is unavailable or mismatched: $verifierBaseId"
    }
    $verifierImage = 'local-agent/verifier-python:3.12.11-pytest8.4.1-v2'
    $verifierId = docker image inspect $verifierImage --format '{{.Id}}' 2>$null
    $verifierLabelsRaw = docker image inspect $verifierImage --format '{{json .Config.Labels}}' 2>$null
    if (($LASTEXITCODE -ne 0) -or ($verifierId -notmatch '^sha256:[0-9a-f]{64}$')) {
        throw "Pinned Python verifier image is unavailable or invalid: $verifierId"
    }
    $verifierLabels = $verifierLabelsRaw | ConvertFrom-Json
    if (($verifierLabels.'local-agent.component' -ne 'coding-verifier-python') -or
        ($verifierLabels.'local-agent.recipe' -ne 'python-3.12.11-pytest-8.4.1-v2')) {
        throw 'Pinned Python verifier image labels do not match the trusted recipe.'
    }
    $staleContainers = @(docker ps --all --filter 'label=local-agent.owner=coding-engine' --format '{{.Names}}' 2>$null | Where-Object { $_ })
    $staleNetworks = @(docker network ls --filter 'label=local-agent.owner=coding-engine' --format '{{.Name}}' 2>$null | Where-Object { $_ })
    if (($LASTEXITCODE -ne 0) -or $staleContainers.Count -gt 0 -or $staleNetworks.Count -gt 0) {
        throw "Stale Qwen sandbox resources: containers=$($staleContainers -join ',') networks=$($staleNetworks -join ',')"
    }
    Write-Host '[PASS] Coding Docker sandboxes - pinned Qwen/Python runtimes and zero stale labeled resources' -ForegroundColor Green
} catch { Write-Host "[FAIL] Coding Docker sandboxes - $_" -ForegroundColor Red; $script:Failures++ }

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
    $codingPolicyPath = Join-Path $Root 'config\coding.json'
    $codingPolicy = [IO.File]::ReadAllText($codingPolicyPath) | ConvertFrom-Json
    if ($codingPolicy.policy_version -ne '2026-07-15.4') {
        throw "Unexpected coding policy version: $($codingPolicy.policy_version)"
    }
    $configuredSemanticExecutable = [string]$env:LOCESTRA_OLLAMA_EXECUTABLE
    if ([string]::IsNullOrWhiteSpace($configuredSemanticExecutable)) {
        $configuredSemanticExecutable = [string]$codingPolicy.local_semantic_expected_executable_path
    }
    if ($configuredSemanticExecutable -eq 'auto') {
        $ollamaCommand = Get-Command ollama -ErrorAction SilentlyContinue
        if ($ollamaCommand) {
            $configuredSemanticExecutable = [string]$ollamaCommand.Source
        } else {
            $ollamaCandidates = @(
                (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\Ollama\ollama.exe'),
                (Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'Ollama\ollama.exe')
            )
            $configuredSemanticExecutable = @(
                $ollamaCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }
            ) | Select-Object -First 1
        }
    }
    if ([string]::IsNullOrWhiteSpace($configuredSemanticExecutable)) {
        throw 'Ollama executable was not found through runtime override, PATH, or system locations'
    }
    $expectedSemanticExecutable = [IO.Path]::GetFullPath($configuredSemanticExecutable)
    $semanticExecutable = Get-Item -LiteralPath $expectedSemanticExecutable -Force -ErrorAction Stop
    if ($semanticExecutable.PSIsContainer -or
        (($semanticExecutable.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw 'Pinned semantic-review executable is not one regular non-reparse file'
    }
    $semanticExecutableHash = (Get-FileHash -LiteralPath $semanticExecutable.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    $configuredSemanticDigest = [string]$env:LOCESTRA_OLLAMA_EXECUTABLE_SHA256
    if ([string]::IsNullOrWhiteSpace($configuredSemanticDigest)) {
        $configuredSemanticDigest = [string]$codingPolicy.local_semantic_expected_executable_sha256
    }
    $semanticDigestMode = 'pinned'
    if ($configuredSemanticDigest -eq 'auto') {
        $configuredSemanticDigest = $semanticExecutableHash
        $semanticDigestMode = 'runtime-derived'
    } elseif ($configuredSemanticDigest -notmatch '^[0-9a-fA-F]{64}$') {
        throw 'Pinned Ollama executable digest is not a SHA-256 value'
    }
    if ($semanticExecutableHash -ne $configuredSemanticDigest.ToLowerInvariant()) {
        throw "Pinned Ollama executable digest mismatch: $semanticExecutableHash"
    }
    $semanticListeners = @(
        Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction Stop |
            Where-Object { $_.LocalAddress -eq '127.0.0.1' }
    )
    if ($semanticListeners.Count -ne 1 -or -not $semanticListeners[0].OwningProcess) {
        throw "Expected one exact 127.0.0.1:11434 listener, received $($semanticListeners.Count)"
    }
    $semanticProcess = Get-Process -Id $semanticListeners[0].OwningProcess -ErrorAction Stop
    $listenerExecutable = [IO.Path]::GetFullPath([string]$semanticProcess.Path)
    if (-not [string]::Equals(
        $listenerExecutable,
        $semanticExecutable.FullName,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Ollama listener executable mismatch: $listenerExecutable"
    }
    $semanticTags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 15
    $semanticAliases = @(
        $semanticTags.models |
            Where-Object {
                $_.name -in @(
                    [string]$codingPolicy.local_semantic_model,
                    "$($codingPolicy.local_semantic_model):latest"
                )
            }
    )
    if ($semanticAliases.Count -ne 1 -or
        $semanticAliases[0].digest -ne [string]$codingPolicy.local_semantic_expected_model_digest) {
        throw 'local-strong alias does not resolve to the policy-pinned model digest'
    }
    Write-Host "[PASS] Local semantic reviewer - exact loopback listener, $semanticDigestMode executable SHA, model alias and digest" -ForegroundColor Green
} catch { Write-Host "[FAIL] Local semantic reviewer identity - $_" -ForegroundColor Red; $script:Failures++ }

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
Check-OpenAiAuthBoundary 'Gateway bearer boundary' 'http://127.0.0.1:8787/v1/models'
Check-OpenAiAuthBoundary 'Voice bearer boundary' 'http://127.0.0.1:8788/v1/audio/transcriptions'
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
    $mcpOutput = & uv run --quiet python -m services.mcp_hub.cli doctor --live 2>&1
    if ($LASTEXITCODE -ne 0) { throw ($mcpOutput -join "`n") }
    $mcp = ($mcpOutput -join "`n") | ConvertFrom-Json
    if (($mcp.status -eq 'ok') -and
        ($mcp.failure_isolation -eq 'ok') -and
        ($mcp.lifecycle_cleanup -eq 'ok') -and
        (@($mcp.unexpected_owners).Count -eq 0) -and
        (@($mcp.unexpected_managed_processes).Count -eq 0) -and
        ($mcp.servers.context7.bounded_call -eq 'ok') -and
        ($mcp.servers.playwright.title_fixture -eq 'ok') -and
        ($mcp.servers.'local-diagnostics'.bounded_call -eq 'ok')) {
        Write-Host '[PASS] Managed MCP Hub - Context7, Playwright, local diagnostics and lifecycle' -ForegroundColor Green
    } else { throw ($mcp | ConvertTo-Json -Depth 8) }
} catch { Write-Host "[FAIL] Managed MCP Hub - $_" -ForegroundColor Red; $script:Failures++ }

try {
    $browser = npm run --silent browser:health 2>&1
    if ($browser -match 'PLAYWRIGHT_OK') { Write-Host '[PASS] Playwright Chromium' -ForegroundColor Green }
    else { throw ($browser -join "`n") }
} catch { Write-Host "[FAIL] Playwright - $_" -ForegroundColor Red; $script:Failures++ }

try { Write-Host "[PASS] Qwen Code - $(qwen --version)" -ForegroundColor Green }
catch { Write-Host '[FAIL] Qwen Code version check' -ForegroundColor Red; $script:Failures++ }

$codexCommand = Get-Command codex -ErrorAction SilentlyContinue
if ($codexCommand) {
    try {
        $codexVersion = codex --version 2>&1 | Out-String
        if (($LASTEXITCODE -ne 0) -or [string]::IsNullOrWhiteSpace($codexVersion)) {
            throw $codexVersion.Trim()
        }
        Write-Host "[PASS] Codex CLI - $($codexVersion.Trim())" -ForegroundColor Green
    } catch {
        if ($script:CodexExecutionRequired) {
            Write-Host "[FAIL] Required Codex version check - $_" -ForegroundColor Red
            $script:Failures++
        } else {
            Write-Host "[WARN] Optional Codex version check - $_" -ForegroundColor Yellow
            $script:Warnings++
        }
    }

    try {
        $codexLogin = cmd.exe /d /c "codex login status 2>&1" | Out-String
        if (($LASTEXITCODE -eq 0) -and
            ($codexLogin -match '(?im)^\s*Logged in(?:\s|$)')) {
            Write-Host "[PASS] Codex cloud authentication - $($codexLogin.Trim())" -ForegroundColor Green
        } else {
            throw $codexLogin.Trim()
        }
    } catch {
        if ($script:CodexExecutionRequired) {
            Write-Host "[FAIL] Required Codex cloud authentication - $_" -ForegroundColor Red
            $script:Failures++
        } else {
            Write-Host '[WARN] Optional Codex is not authenticated; local workflows remain available' -ForegroundColor Yellow
            $script:Warnings++
        }
    }
} elseif (-not $script:CodexExecutionRequired) {
    Write-Host '[INFO] Optional Codex unavailable; local workflows remain available' -ForegroundColor Cyan
}

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

try {
    $codingStatus = Invoke-CodingCli @('status')
    if (($codingStatus.schema_version -ne '1.0') -or
        ($codingStatus.command -ne 'status') -or
        (-not $codingStatus.healthy) -or
        ($codingStatus.database.schema_version -ne 1) -or
        ($codingStatus.database.application_id -ne 1279347011) -or
        ($codingStatus.database.integrity_check -ne 'ok') -or
        ($codingStatus.database.foreign_key_violations -ne 0) -or
        (-not $codingStatus.database.event_chain_consistent) -or
        (-not $codingStatus.ownership_registry.owned_root_marker_valid) -or
        ($codingStatus.ownership_registry.invalid_records -ne 0) -or
        ($codingStatus.ownership_registry.stale_active_records -ne 0) -or
        ($codingStatus.ownership_registry.mirror_missing -ne 0) -or
        ($codingStatus.ownership_registry.mirror_identity_mismatch -ne 0) -or
        ($codingStatus.ownership_registry.mirror_status_mismatch -ne 0)) {
        throw ($codingStatus | ConvertTo-Json -Depth 8)
    }
    Write-Host '[PASS] Coding Engine store, event chain, owned registry, and orphan health' -ForegroundColor Green
} catch {
    Write-Host "[FAIL] Coding Engine operational health - $_" -ForegroundColor Red
    $script:Failures++
}

if ($script:Failures -eq 0) {
    Write-Host 'DOCTOR_OK' -ForegroundColor Green
    exit 0
}
Write-Host "DOCTOR_FAILED failures=$script:Failures warnings=$script:Warnings" -ForegroundColor Red
exit 1
