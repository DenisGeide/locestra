$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. (Join-Path $PSScriptRoot 'process-ownership.ps1')
$ComfyRoot = Join-Path $Root 'modules\ComfyUI_windows_portable'
$Python = Join-Path $ComfyRoot 'python_embeded\python.exe'
$Main = Join-Path $ComfyRoot 'ComfyUI\main.py'
$ComfyFragments = @('ComfyUI\main.py', '--port', '8388')
$ConfigPython = Join-Path $Root '.venv\Scripts\python.exe'

if (-not (Test-Path $Python)) { throw 'ComfyUI portable Python is missing' }
if (-not (Test-Path $Main)) { throw 'ComfyUI main.py is missing' }
if (-not (Test-Path $ConfigPython)) { throw 'Project Python environment is missing; run bootstrap first' }
& $ConfigPython -c 'from services.config import get_settings; get_settings()'
if ($LASTEXITCODE -ne 0) {
    throw 'Effective configuration is invalid or requires a coordinated lifecycle migration'
}

$existingOwnership = Resolve-ProcessOwnership `
    -Root $Root -Name 'comfyui' -Port 8388 -Fragments $ComfyFragments `
    -RequireRootIdentity $true -AllowLegacy -AllowMatchingAdoption
if ($existingOwnership) {
    $existingDeadline = (Get-Date).AddMinutes(5)
    do {
        try {
            $existingResponse = Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8388/system_stats' -TimeoutSec 5
            if ($existingResponse.StatusCode -eq 200) { Write-Host 'COMFYUI_ALREADY_RUNNING'; exit 0 }
        } catch {}
        if (-not (Get-ProcessSnapshot ([int]$existingOwnership.ProcessId))) {
            throw 'Owned ComfyUI process exited before readiness'
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $existingDeadline)
    throw 'Owned ComfyUI listener did not become ready within 5 minutes'
}
if (@(Get-ListenerProcessIds 8388).Count -gt 0) {
    throw 'Port 8388 is occupied but safe ComfyUI ownership could not be established'
}

$previousErrorPreference = $ErrorActionPreference
$ErrorActionPreference = 'SilentlyContinue'
foreach ($model in @('local-strong','qwen3.6:35b')) {
    & ollama stop $model *> $null
}
$ErrorActionPreference = $previousErrorPreference
Start-Sleep -Seconds 2

$process = $null
$ownershipRegistered = $false
try {
    $process = Start-Process -FilePath $Python `
        -ArgumentList @('-s', $Main, '--windows-standalone-build', '--listen', '127.0.0.1', '--port', '8388', '--disable-api-nodes', '--disable-auto-launch') `
        -WorkingDirectory (Join-Path $ComfyRoot 'ComfyUI') -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput "$Root\logs\comfyui.out.log" `
        -RedirectStandardError "$Root\logs\comfyui.err.log"
    $listenerProcessId = Wait-VerifiedListenerProcessId `
        -Root $Root -Port 8388 -Fragments $ComfyFragments -RequireRootIdentity $true `
        -LauncherProcessId $process.Id -Seconds 300
    Write-ProcessOwnership `
        -Root $Root -Name 'comfyui' -Port 8388 -TargetProcessId $listenerProcessId `
        -Fragments $ComfyFragments -RequireRootIdentity $true | Out-Null
    $ownershipRegistered = $true

    $deadline = (Get-Date).AddMinutes(5)
    do {
        try {
            $response = Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8388/system_stats' -TimeoutSec 5
            if ($response.StatusCode -eq 200) { Write-Host 'COMFYUI_STARTED'; exit 0 }
        } catch {}
        if (-not (Get-ProcessSnapshot $listenerProcessId)) {
            $errorLog = Get-Content -Raw -ErrorAction SilentlyContinue "$Root\logs\comfyui.err.log"
            throw "ComfyUI exited during startup: $errorLog"
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    throw 'ComfyUI startup timeout'
} catch {
    $startupError = $_
    if ($ownershipRegistered) {
        try {
            [void](Stop-OwnedProcess `
                -Root $Root -Name 'comfyui' -Port 8388 -Fragments $ComfyFragments `
                -RequireRootIdentity $true -Seconds 20)
        } catch {}
    } elseif ($process) {
        try {
            Stop-VerifiedLaunchTree `
                -Root $Root -Fragments $ComfyFragments -LauncherProcessId $process.Id `
                -RequireRootIdentity $true -Seconds 10
        } catch {}
    }
    Remove-ProcessOwnershipFiles $Root 'comfyui'
    throw $startupError
}
