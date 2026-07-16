$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. (Join-Path $PSScriptRoot 'process-ownership.ps1')

$env:OLLAMA_MAX_LOADED_MODELS = '2'
$env:OLLAMA_NUM_PARALLEL = '1'
$env:OLLAMA_KEEP_ALIVE = '30m'
$env:OLLAMA_NO_CLOUD = '1'

$FastOllamaFragments = @('ollama.exe', 'serve')
$GatewayFragments = @('uvicorn', 'services.gateway.app:app', '--port', '8787')
$VoiceFragments = @('uvicorn', 'services.voice.app:app', '--port', '8788')
$TelegramFragments = @('python.exe', '-m services.telegram.bot')

function Wait-Url([string]$Url, [int]$Seconds = 120) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 5
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) { return }
        } catch {}
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Timed out waiting for $Url"
}

function Wait-GatewayReady([int]$Seconds = 120) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8787/health' -TimeoutSec 5
            if (($health.status -eq 'ok') -and $health.fast_model_present -and $health.strong_model_present) { return }
        } catch {}
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw 'Gateway is reachable but its fast/strong model profiles are not ready'
}

function Test-GatewayAuthBoundary {
    try {
        # A current gateway must reject an otherwise valid unauthenticated
        # OpenAI request.  A legacy process returning 200 is restarted below.
        Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8787/v1/models' -TimeoutSec 5 | Out-Null
        return $false
    } catch {}
    try {
        $headers = @{ Authorization = 'Bearer ' + $env:GATEWAY_API_KEY }
        $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8787/v1/models' -Headers $headers -TimeoutSec 5
        return ($response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Wait-GatewayAuthBoundary([int]$Seconds = 30) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        if (Test-GatewayAuthBoundary) { return }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    throw 'Gateway OpenAI authentication boundary is not ready'
}

function Protect-CurrentUserFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'Cannot protect a missing local credential file'
    }
    $account = New-Object System.Security.Principal.NTAccount(
        [Security.Principal.WindowsIdentity]::GetCurrent().Name
    )
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetOwner($account)
    $acl.SetAccessRuleProtection($true, $false)
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $account,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    [void]$acl.AddAccessRule($rule)
    [IO.File]::SetAccessControl($Path, $acl)
    $observed = Get-Acl -LiteralPath $Path
    $rules = @($observed.Access)
    if (
        -not $observed.AreAccessRulesProtected -or
        $rules.Count -ne 1 -or
        $rules[0].IsInherited -or
        $rules[0].AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow -or
        $rules[0].IdentityReference.Value -ne $account.Value -or
        (($rules[0].FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -ne
            [System.Security.AccessControl.FileSystemRights]::FullControl)
    ) {
        throw 'Gateway credential ACL is not owner-only'
    }
}

if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env' }
New-Item -ItemType Directory -Force -Path 'logs','run','data','inbox','outputs' | Out-Null

$gatewayKeyPath = Join-Path $Root 'run\gateway-api-key.txt'
if (-not (Test-Path -LiteralPath $gatewayKeyPath)) {
    $gatewayKeyBytes = New-Object byte[] 32
    $gatewayKeyGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $gatewayKeyGenerator.GetBytes($gatewayKeyBytes)
    } finally {
        $gatewayKeyGenerator.Dispose()
    }
    $gatewayKey = ([BitConverter]::ToString($gatewayKeyBytes) -replace '-', '').ToLowerInvariant()
    [IO.File]::WriteAllText(
        $gatewayKeyPath,
        $gatewayKey,
        [Text.UTF8Encoding]::new($false)
    )
}
Protect-CurrentUserFile $gatewayKeyPath
Set-Item -Path 'Env:GATEWAY_API_KEY' -Value ([IO.File]::ReadAllText($gatewayKeyPath).Trim())
if ($env:GATEWAY_API_KEY.Length -lt 32) {
    throw 'Gateway API credential file is invalid; remove it and restart to regenerate'
}

uv sync --frozen --python 3.12 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "uv sync failed with exit code $LASTEXITCODE" }
$Python = Join-Path $Root '.venv\Scripts\python.exe'
& $Python -c 'from services.config import get_settings; get_settings()'
if ($LASTEXITCODE -ne 0) {
    throw 'Effective configuration is invalid or requires a coordinated lifecycle migration'
}

$dockerReady = $false
& docker info 2>$null | Out-Null
$dockerReady = ($LASTEXITCODE -eq 0)
if (-not $dockerReady) {
    $dockerDesktop = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
    if (Test-Path $dockerDesktop) { Start-Process $dockerDesktop -WindowStyle Hidden }
    $deadline = (Get-Date).AddSeconds(120)
    do {
        Start-Sleep -Seconds 2
        & docker info 2>$null | Out-Null
        $dockerReady = ($LASTEXITCODE -eq 0)
    } while (-not $dockerReady -and (Get-Date) -lt $deadline)
    if (-not $dockerReady) { throw 'Docker Desktop engine did not start' }
}

try { Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 | Out-Null } catch {
    $ollama = (Get-Command ollama).Source
    Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden
    Wait-Url 'http://127.0.0.1:11434/api/tags' 60
}

$fastOwnership = Resolve-ProcessOwnership `
    -Root $Root -Name 'ollama-fast' -Port 11435 -Fragments $FastOllamaFragments `
    -RequireRootIdentity $false -AllowLegacy
if (-not $fastOwnership) {
    $existingFastListeners = @(Get-ListenerProcessIds 11435)
    if ($existingFastListeners.Count -gt 0) {
        # Validate identity for a precise diagnostic, then fail closed: fast
        # Ollama is platform-owned and requires prior ownership evidence.
        [void](Get-VerifiedListenerProcessId $Root 11435 $FastOllamaFragments $false)
        throw 'Port 11435 has a matching but unowned Ollama listener; refusing an unmanageable lifecycle topology'
    } else {
        $ollama = (Get-Command ollama).Source
        $previousOllamaHost = $env:OLLAMA_HOST
        try {
            $env:OLLAMA_HOST = '127.0.0.1:11435'
            $fastOllama = Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput "$Root\logs\ollama-fast.out.log" `
                -RedirectStandardError "$Root\logs\ollama-fast.err.log"
            try {
                $fastListenerProcessId = Wait-VerifiedListenerProcessId `
                    -Root $Root -Port 11435 -Fragments $FastOllamaFragments -RequireRootIdentity $false `
                    -LauncherProcessId $fastOllama.Id -Seconds 60
                Write-ProcessOwnership `
                    -Root $Root -Name 'ollama-fast' -Port 11435 -TargetProcessId $fastListenerProcessId `
                    -Fragments $FastOllamaFragments -RequireRootIdentity $false | Out-Null
            } catch {
                try {
                    Stop-VerifiedLaunchTree `
                        -Root $Root -Fragments $FastOllamaFragments -LauncherProcessId $fastOllama.Id `
                        -RequireRootIdentity $false -Seconds 10
                } catch {}
                throw
            }
        } finally {
            if ($null -eq $previousOllamaHost) { Remove-Item Env:OLLAMA_HOST -ErrorAction SilentlyContinue }
            else { $env:OLLAMA_HOST = $previousOllamaHost }
        }
    }
}
Wait-Url 'http://127.0.0.1:11435/api/tags' 60

$gatewayOwnership = Resolve-ProcessOwnership `
    -Root $Root -Name 'gateway' -Port 8787 -Fragments $GatewayFragments `
    -RequireRootIdentity $true -AllowLegacy -AllowMatchingAdoption
if ($gatewayOwnership -and -not (Test-GatewayAuthBoundary)) {
    $stopped = Stop-OwnedProcess `
        -Root $Root -Name 'gateway' -Port 8787 -Fragments $GatewayFragments `
        -RequireRootIdentity $true -Seconds 20 -AllowLegacy
    if (-not $stopped) { throw 'Legacy gateway could not be safely restarted for authentication' }
    $gatewayOwnership = $null
}
if (-not $gatewayOwnership) {
    if (@(Get-ListenerProcessIds 8787).Count -gt 0) {
        throw 'Port 8787 is occupied but safe gateway ownership could not be established'
    }
    $gateway = Start-Process -FilePath $Python `
        -ArgumentList @('-m','uvicorn','services.gateway.app:app','--host','0.0.0.0','--port','8787') `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput "$Root\logs\gateway.out.log" `
        -RedirectStandardError "$Root\logs\gateway.err.log"
    try {
        $gatewayListenerProcessId = Wait-VerifiedListenerProcessId `
            -Root $Root -Port 8787 -Fragments $GatewayFragments -RequireRootIdentity $true `
            -LauncherProcessId $gateway.Id -Seconds 120
        Write-ProcessOwnership `
            -Root $Root -Name 'gateway' -Port 8787 -TargetProcessId $gatewayListenerProcessId `
            -Fragments $GatewayFragments -RequireRootIdentity $true | Out-Null
    } catch {
        try {
            Stop-VerifiedLaunchTree `
                -Root $Root -Fragments $GatewayFragments -LauncherProcessId $gateway.Id `
                -RequireRootIdentity $true -Seconds 10
        } catch {}
        throw
    }
}

Wait-GatewayAuthBoundary 30

$voiceOwnership = Resolve-ProcessOwnership `
    -Root $Root -Name 'voice' -Port 8788 -Fragments $VoiceFragments `
    -RequireRootIdentity $true -AllowLegacy -AllowMatchingAdoption
if (-not $voiceOwnership) {
    if (@(Get-ListenerProcessIds 8788).Count -gt 0) {
        throw 'Port 8788 is occupied but safe voice ownership could not be established'
    }
    $voice = Start-Process -FilePath $Python `
        -ArgumentList @('-m','uvicorn','services.voice.app:app','--host','0.0.0.0','--port','8788') `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput "$Root\logs\voice.out.log" `
        -RedirectStandardError "$Root\logs\voice.err.log"
    try {
        $voiceListenerProcessId = Wait-VerifiedListenerProcessId `
            -Root $Root -Port 8788 -Fragments $VoiceFragments -RequireRootIdentity $true `
            -LauncherProcessId $voice.Id -Seconds 120
        Write-ProcessOwnership `
            -Root $Root -Name 'voice' -Port 8788 -TargetProcessId $voiceListenerProcessId `
            -Fragments $VoiceFragments -RequireRootIdentity $true | Out-Null
    } catch {
        try {
            Stop-VerifiedLaunchTree `
                -Root $Root -Fragments $VoiceFragments -LauncherProcessId $voice.Id `
                -RequireRootIdentity $true -Seconds 10
        } catch {}
        throw
    }
}

Wait-GatewayReady 120
Wait-Url 'http://127.0.0.1:8788/health' 120

$telegramToken = ''
foreach ($line in Get-Content '.env') {
    if ($line -match '^TELEGRAM_BOT_TOKEN=(.+)$') { $telegramToken = $Matches[1].Trim() }
}
if ($telegramToken) {
    $telegramOwnership = Resolve-ProcessOwnership `
        -Root $Root -Name 'telegram' -Port 0 -Fragments $TelegramFragments `
        -RequireRootIdentity $true -AllowLegacy -AllowMatchingAdoption
    if (-not $telegramOwnership) {
        $telegram = Start-Process -FilePath $Python `
            -ArgumentList @('-m','services.telegram.bot') `
            -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput "$Root\logs\telegram.out.log" `
            -RedirectStandardError "$Root\logs\telegram.err.log"
        try {
            $telegramWorkerProcessId = Wait-VerifiedProcessId `
                -Root $Root -Fragments $TelegramFragments -LauncherProcessId $telegram.Id `
                -RequireRootIdentity $true -Seconds 30
            Write-ProcessOwnership `
                -Root $Root -Name 'telegram' -Port 0 -TargetProcessId $telegramWorkerProcessId `
                -Fragments $TelegramFragments -RequireRootIdentity $true | Out-Null
        } catch {
            try {
                Stop-VerifiedLaunchTree `
                    -Root $Root -Fragments $TelegramFragments -LauncherProcessId $telegram.Id `
                    -RequireRootIdentity $true -Seconds 10
            } catch {}
            throw
        }
    }
}

docker compose up -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed with exit code $LASTEXITCODE" }
Wait-Url 'http://127.0.0.1:3737/health' 300
Wait-Url 'http://127.0.0.1:5678/healthz' 180

Write-Host 'LOCAL_AGENT_STARTED'
Write-Host 'Open WebUI: http://127.0.0.1:3737'
Write-Host 'n8n:        http://127.0.0.1:5678'
Write-Host 'Gateway:    http://127.0.0.1:8787/health'
Write-Host 'Voice:      http://127.0.0.1:8788/health'
Write-Host 'Fast model: http://127.0.0.1:11435 (CPU)'
if ($telegramToken) { Write-Host 'Telegram:   enabled' } else { Write-Host 'Telegram:   waiting for TELEGRAM_BOT_TOKEN' }
