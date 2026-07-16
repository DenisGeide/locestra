$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'process-ownership.ps1')
$ComfyFragments = @('ComfyUI\main.py', '--port', '8388')
$failed = $false
try {
    $stopped = Stop-OwnedProcess `
        -Root $Root -Name 'comfyui' -Port 8388 -Fragments $ComfyFragments `
        -RequireRootIdentity $true -Seconds 20 -AllowLegacy
    if ((-not $stopped) -and (@(Get-ListenerProcessIds 8388).Count -gt 0)) {
        Write-Warning 'Listener on port 8388 remains because verified ComfyUI ownership was unavailable'
        $failed = $true
    }
} catch {
    Write-Warning "Failed to stop owned ComfyUI process: $($_.Exception.Message)"
    $failed = $true
}
if ($failed) {
    Write-Host 'COMFYUI_STOP_FAILED'
    exit 1
}
Write-Host 'COMFYUI_STOPPED'
