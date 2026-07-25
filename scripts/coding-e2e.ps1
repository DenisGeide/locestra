[CmdletBinding()]
param(
    [switch]$IncludeCodex,
    [string]$QwenModel = "",
    [string]$CodexModel = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$CodexApproval = "I_APPROVE_CODEX_PUBLIC_SYNTHETIC_FIXTURE"
$ScopedEnvironmentNames = @(
    "LOCAL_AGENT_RUN_LIVE_CODING",
    "LOCAL_AGENT_RUN_LIVE_CODEX",
    "LOCAL_AGENT_CODEX_PUBLIC_FIXTURE_APPROVAL",
    "LOCAL_AGENT_LIVE_TEMP_PARENT",
    "LOCAL_AGENT_LIVE_QWEN_MODEL",
    "LOCAL_AGENT_LIVE_CODEX_MODEL"
)
$PreviousEnvironment = @{}
foreach ($Name in $ScopedEnvironmentNames) {
    $PreviousEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
}

function Find-RequiredCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Names,
        [Parameter(Mandatory = $true)]
        [string]$Purpose
    )

    foreach ($Name in $Names) {
        $Command = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $Command) {
            return $Command
        }
    }
    throw "$Purpose is required. Tried: $($Names -join ', ')"
}

# Keep disposable fixture paths deliberately short. Native Windows Git still
# encounters MAX_PATH-sensitive ref/worktree internals on some installations.
$TempBase = Join-Path ([IO.Path]::GetTempPath()) "LAE2E"
$TempParent = Join-Path $TempBase ([Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $TempParent -Force | Out-Null
$TempParent = (Resolve-Path $TempParent).Path
if ($TempParent.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to create a live coding fixture inside the product repository."
}

try {
    $Git = Find-RequiredCommand -Names @("git.exe", "git") -Purpose "Git"
    $Qwen = Find-RequiredCommand -Names @("qwen.cmd", "qwen") -Purpose "Qwen Code CLI"
    $Node = Find-RequiredCommand -Names @("node.exe", "node") -Purpose "Node.js"
    $null = $Git
    $null = $Qwen

    Push-Location $Root
    try {
        & $Node.Source --input-type=module -e "await import('playwright');"
        if ($LASTEXITCODE -ne 0) {
            throw "The Playwright Node package is not available from the project root."
        }
    }
    finally {
        Pop-Location
    }

    $Platform = Get-Content (Join-Path $Root "config\platform.json") -Raw | ConvertFrom-Json
    $OllamaBase = [string]$Platform.settings.OLLAMA_BASE_URL
    try {
        $null = Invoke-RestMethod -Uri ($OllamaBase.TrimEnd("/") + "/api/tags") -TimeoutSec 10
    }
    catch {
        throw "The local Ollama endpoint required by Qwen is unavailable at $OllamaBase."
    }

    [Environment]::SetEnvironmentVariable("LOCAL_AGENT_RUN_LIVE_CODING", "1", "Process")
    [Environment]::SetEnvironmentVariable("LOCAL_AGENT_LIVE_TEMP_PARENT", $TempParent, "Process")
    if ($QwenModel) {
        [Environment]::SetEnvironmentVariable("LOCAL_AGENT_LIVE_QWEN_MODEL", $QwenModel, "Process")
    }

    if ($IncludeCodex) {
        $Codex = Find-RequiredCommand -Names @("codex.cmd", "codex") -Purpose "Codex CLI"
        $null = $Codex
        [Environment]::SetEnvironmentVariable("LOCAL_AGENT_RUN_LIVE_CODEX", "1", "Process")
        [Environment]::SetEnvironmentVariable(
            "LOCAL_AGENT_CODEX_PUBLIC_FIXTURE_APPROVAL",
            $CodexApproval,
            "Process"
        )
        if ($CodexModel) {
            [Environment]::SetEnvironmentVariable("LOCAL_AGENT_LIVE_CODEX_MODEL", $CodexModel, "Process")
        }
        Write-Warning "Codex cloud E2E is explicitly enabled for generated PUBLIC synthetic fixture data only."
    }
    else {
        [Environment]::SetEnvironmentVariable("LOCAL_AGENT_RUN_LIVE_CODEX", "0", "Process")
        [Environment]::SetEnvironmentVariable(
            "LOCAL_AGENT_CODEX_PUBLIC_FIXTURE_APPROVAL",
            $null,
            "Process"
        )
        [Environment]::SetEnvironmentVariable("LOCAL_AGENT_LIVE_CODEX_MODEL", $null, "Process")
    }

    Push-Location $Root
    try {
        $Uv = Get-Command "uv" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $Uv) {
            & $Uv.Source run --no-sync pytest -m required_e2e -vv -s
        }
        else {
            $Python = Find-RequiredCommand -Names @("python.exe", "python") -Purpose "Python"
            & $Python.Source -m pytest -m required_e2e -vv -s
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Live coding E2E failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    foreach ($Name in $ScopedEnvironmentNames) {
        [Environment]::SetEnvironmentVariable($Name, $PreviousEnvironment[$Name], "Process")
    }

    if (Test-Path -LiteralPath $TempParent) {
        $Remaining = @(Get-ChildItem -LiteralPath $TempParent -Force)
        if ($Remaining.Count -eq 0) {
            Remove-Item -LiteralPath $TempParent -Force
        }
        else {
            Write-Warning "A marked live fixture remains for diagnosis: $TempParent"
        }
    }
}
