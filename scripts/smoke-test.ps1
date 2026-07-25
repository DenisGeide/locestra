$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$gatewayKeyPath = Join-Path $Root 'run\gateway-api-key.txt'
if (-not (Test-Path -LiteralPath $gatewayKeyPath)) { throw 'Gateway API credential is missing; run start.ps1 first' }
$GatewayHeaders = @{ Authorization = 'Bearer ' + [IO.File]::ReadAllText($gatewayKeyPath).Trim() }

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

Write-Host '[1/10] Python routing tests'
uv run pytest
if ($LASTEXITCODE -ne 0) { throw "pytest failed with exit code $LASTEXITCODE" }

Write-Host '[2/10] OpenAI-compatible automatic gateway'
$body = @{
    model = 'local-agent-auto'
    messages = @(@{ role='user'; content='Reply with exactly GATEWAY_OK' })
    stream = $false
    max_tokens = 512
} | ConvertTo-Json -Depth 8
$gatewayResponse = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8787/v1/chat/completions' -Headers $GatewayHeaders -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 300
$reply = $gatewayResponse.Content | ConvertFrom-Json
if ($reply.choices[0].message.content -notmatch 'GATEWAY_OK') { throw "Gateway semantic test failed: $($reply | ConvertTo-Json -Depth 8)" }
if ($gatewayResponse.Headers['X-Local-Agent-Route'] -ne 'fast_chat') { throw 'Easy request did not use fast_chat' }
if ($gatewayResponse.Headers['X-Local-Agent-Model'] -ne 'local-fast') { throw 'Easy request did not use local-fast' }
if (-not $gatewayResponse.Headers['X-Local-Agent-Request-ID']) { throw 'Gateway response is missing request correlation id' }
Write-Host '[PASS] Gateway semantic response'

$streamBody = @{
    model = 'local-agent-auto'
    messages = @(@{ role='user'; content='Say hello in one short sentence' })
    stream = $true
} | ConvertTo-Json -Depth 8
$streamReply = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8787/v1/chat/completions' -Headers $GatewayHeaders -Method Post -ContentType 'application/json' -Body $streamBody -TimeoutSec 300
if (($streamReply.StatusCode -ne 200) -or ($streamReply.Content -notmatch '\[DONE\]')) {
    throw 'Gateway streaming response was incomplete'
}
Write-Host '[PASS] Gateway streaming response'

Write-Host '[3/10] Automatic route matrix'
$routeChecks = @{
    fast_chat = 'What is dependency injection?'
    strong_chat = 'Perform a deep analysis of this business strategy and trade-offs'
    local_code = 'Project: C:\tmp\app; create hello.py'
    codex = 'Project: C:\tmp\app; perform a security review before merge'
    auxiliary = 'Generate a concise title for this chat: generate image'
}
foreach ($expected in $routeChecks.Keys) {
    $encoded = [Uri]::EscapeDataString($routeChecks[$expected])
    $decision = Invoke-RestMethod -Uri "http://127.0.0.1:8787/v1/route?text=$encoded" -Headers $GatewayHeaders -TimeoutSec 15
    if ($decision.route -ne $expected) { throw "Routing failed: expected $expected, received $($decision.route)" }
    if (($decision.schema_version -ne '1.0') -or (-not $decision.executor) -or (-not $decision.reason_codes)) {
        throw "Route decision contract is incomplete: $($decision | ConvertTo-Json -Depth 8)"
    }
}
Write-Host '[PASS] Fast, strong, local agent, Codex, and auxiliary routes'

Write-Host '[4/10] Gateway tool calling and strict SSE stream'
$toolBody = @{
    model = 'local-agent-auto'
    messages = @(@{role='user';content='Use the add tool to add 19 and 23. Do not calculate it yourself.'})
    tools = @(@{type='function';function=@{name='add';description='Add two integers';parameters=@{type='object';properties=@{a=@{type='integer'};b=@{type='integer'}};required=@('a','b')}}})
    stream = $false
} | ConvertTo-Json -Depth 12
$toolResponse = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8787/v1/chat/completions' -Headers $GatewayHeaders -Method Post -ContentType 'application/json' -Body $toolBody -TimeoutSec 300
$toolReply = $toolResponse.Content | ConvertFrom-Json
if (-not $toolReply.choices[0].message.tool_calls) { throw 'Ollama/Qwen did not produce a tool call' }
if ($toolResponse.Headers['X-Local-Agent-Route'] -ne 'fast_chat') { throw 'Gateway tool request was not routed through fast_chat' }

$toolStreamBody = @{
    model = 'local-agent-auto'
    messages = @(@{role='user';content='Use the add tool to add 19 and 23. Do not calculate it yourself.'})
    tools = @(@{type='function';function=@{name='add';description='Add two integers';parameters=@{type='object';properties=@{a=@{type='integer'};b=@{type='integer'}};required=@('a','b')}}})
    stream = $true
} | ConvertTo-Json -Depth 12
$toolStream = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8787/v1/chat/completions' -Headers $GatewayHeaders -Method Post -ContentType 'application/json' -Body $toolStreamBody -TimeoutSec 300
$events = @($toolStream.Content -split "`r?`n`r?`n" | Where-Object { $_ -match '^data: ' })
$jsonEvents = @($events | Where-Object { $_ -notmatch '\[DONE\]' } | ForEach-Object { ($_ -replace '^data: ', '') | ConvertFrom-Json })
$toolDelta = @($jsonEvents | Where-Object { $_.choices[0].delta.tool_calls })[0]
$finalToolEvent = @($jsonEvents | Where-Object { $_.choices[0].finish_reason -eq 'tool_calls' })
if ($null -eq $toolDelta -or $toolDelta.choices[0].delta.tool_calls[0].index -ne 0) {
    throw 'Gateway SSE tool call is missing index=0'
}
if ($finalToolEvent.Count -eq 0 -or $events[-1] -notmatch '\[DONE\]') {
    throw 'Gateway SSE tool stream did not finish with tool_calls and [DONE]'
}
Write-Host '[PASS] Gateway Qwen tool call, stream index, finish reason, and DONE marker'

Write-Host '[5/10] Playwright browser'
$browser = npm run --silent browser:health
if ($browser -notmatch 'PLAYWRIGHT_OK') { throw 'Playwright semantic test failed' }
Write-Host '[PASS] Playwright Chromium'

Write-Host '[6/10] Managed MCP Hub'
$mcpOutput = & uv run --quiet python -m services.mcp_hub.cli doctor --live 2>&1
if ($LASTEXITCODE -ne 0) { throw "Managed MCP Hub smoke failed: $($mcpOutput -join [Environment]::NewLine)" }
$mcp = ($mcpOutput -join [Environment]::NewLine) | ConvertFrom-Json
if (($mcp.status -ne 'ok') -or
    ($mcp.failure_isolation -ne 'ok') -or
    ($mcp.lifecycle_cleanup -ne 'ok') -or
    (@($mcp.unexpected_owners).Count -ne 0) -or
    (@($mcp.unexpected_managed_processes).Count -ne 0) -or
    ($mcp.servers.context7.bounded_call -ne 'ok') -or
    ($mcp.servers.playwright.title_fixture -ne 'ok') -or
    ($mcp.servers.'local-diagnostics'.bounded_call -ne 'ok')) {
    throw "Managed MCP Hub smoke contract failed: $($mcp | ConvertTo-Json -Depth 10)"
}
Write-Host '[PASS] Context7 documentation, Playwright title fixture and local diagnostics through managed lifecycle'

Write-Host '[7/10] Voice model load'
$voice = Invoke-RestMethod -Uri 'http://127.0.0.1:8788/health?load_model=true' -Headers $GatewayHeaders -TimeoutSec 900
if (-not $voice.loaded) { throw 'faster-whisper model did not load' }
Write-Host "[PASS] faster-whisper $($voice.model) loaded on $($voice.device)"

Write-Host '[8/10] Full gateway-to-Qwen Code edit cycle'
$SmokeFixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ("local-agent-coding-smoke-" + [Guid]::NewGuid().ToString('N'))
$Smoke = Join-Path $SmokeFixtureRoot 'source'
$SmokeRemote = Join-Path $SmokeFixtureRoot 'remote.git'
$SmokeCodingTaskId = $null
$SmokeCleanupComplete = $false
try {
New-Item -ItemType Directory -Path $Smoke -Force | Out-Null
Set-Location $Smoke
git init -q -b main
if ($LASTEXITCODE -ne 0) { throw 'Could not initialize the disposable Coding Engine source fixture' }
git config user.email 'local-agent@localhost'
git config user.name 'Local Agent Smoke Test'
[IO.File]::WriteAllText(
    (Join-Path $Smoke 'README.md'),
    "Create result.txt containing exactly QWEN_CODE_OK and no extra characters.`n",
    [Text.UTF8Encoding]::new($false)
)
$SmokeTests = Join-Path $Smoke 'tests'
New-Item -ItemType Directory -Path $SmokeTests -Force | Out-Null
$resultVerifier = @'
import unittest
from pathlib import Path


class ResultContractTests(unittest.TestCase):
    def test_exact_result_bytes(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        self.assertEqual((repository / "result.txt").read_bytes(), b"QWEN_CODE_OK")


if __name__ == "__main__":
    unittest.main()
'@
[IO.File]::WriteAllText(
    (Join-Path $SmokeTests 'test_result.py'),
    ($resultVerifier.TrimStart() -replace "`r`n", "`n"),
    [Text.UTF8Encoding]::new($false)
)
git add README.md tests/test_result.py
git commit -qm 'smoke baseline'
if ($LASTEXITCODE -ne 0) { throw 'Could not commit the disposable Coding Engine baseline' }
git init --bare -q $SmokeRemote
if ($LASTEXITCODE -ne 0) { throw 'Could not initialize the disposable Coding Engine remote' }
git remote add origin $SmokeRemote
git push -q -u origin main
if ($LASTEXITCODE -ne 0) { throw 'Could not publish the disposable baseline to its local remote' }
$env:QWEN_CODE_SUPPRESS_YOLO_WARNING = '1'
$baselineCommit = (git rev-parse HEAD).Trim()
$baselineReadmeHash = (Get-FileHash -LiteralPath (Join-Path $Smoke 'README.md') -Algorithm SHA256).Hash
$baselineRemoteConfiguration = @((git remote -v))
$baselineRemoteRefs = @((git --git-dir=$SmokeRemote for-each-ref '--format=%(refname)=%(objectname)'))
if ($LASTEXITCODE -ne 0 -or $baselineRemoteRefs.Count -eq 0) {
    throw 'Could not snapshot the disposable remote refs'
}
Set-Location $Root
$agentPrompt = @"
Project: $Smoke
Read README.md.
Create result.txt containing exactly QWEN_CODE_OK with no extra characters. Do not modify README.md and do not commit.
"@
$agentJson = @{
    model = 'local-agent-auto'
    messages = @(@{ role='user'; content=$agentPrompt })
    stream = $false
} | ConvertTo-Json -Depth 8
$agentBody = [Text.Encoding]::UTF8.GetBytes($agentJson)
$agentResponse = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8787/v1/chat/completions' -Headers $GatewayHeaders -Method Post -ContentType 'application/json; charset=utf-8' -Body $agentBody -TimeoutSec 1800
if ($agentResponse.Headers['X-Local-Agent-Route'] -ne 'local_code') {
    throw "Gateway did not use local_code: $($agentResponse.Headers['X-Local-Agent-Route']) $($agentResponse.Content)"
}
$requestId = [string]$agentResponse.Headers['X-Local-Agent-Request-ID']
if ([string]::IsNullOrWhiteSpace($requestId)) { throw 'Coding Engine response is missing its request correlation id' }
$SmokeCodingTaskId = $requestId
try {
    $agentPayload = $agentResponse.Content | ConvertFrom-Json
    $agentContent = [string]$agentPayload.choices[0].message.content
} catch {
    throw "Coding Engine returned invalid OpenAI-compatible JSON: $($agentResponse.Content)"
}
if ([string]::IsNullOrWhiteSpace($agentContent)) { throw 'Coding Engine response has no assistant content' }
$worktreeMatch = [regex]::Match($agentContent, '(?m)^Worktree:\s*(?<path>[^\r\n]+?)\s*$')
if (-not $worktreeMatch.Success) { throw "Coding Engine response did not return a worktree path: $agentContent" }
$reportedWorktree = $worktreeMatch.Groups['path'].Value.Trim()
if (-not [IO.Path]::IsPathRooted($reportedWorktree) -or
    $reportedWorktree -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+)') {
    throw "Coding Engine returned a non-absolute worktree path: $reportedWorktree"
}
if ($agentContent -notmatch '(?m)^Modified:\s*result\.txt\s*$' -or
    $agentContent -notmatch '(?m)^Verification:\s*passed\s*$' -or
    $agentContent -notmatch '(?m)^Review:\s*approved\s*$') {
    throw "Coding Engine response did not report the expected verified and independently reviewed change: $agentContent"
}

$ownedRootCandidate = if ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA 'LocalAgent\coding-worktrees'
} else {
    Join-Path ([IO.Path]::GetTempPath()) 'LocalAgent\coding-worktrees'
}
if (-not (Test-Path -LiteralPath $ownedRootCandidate -PathType Container)) {
    throw "Coding Engine owned root does not exist: $ownedRootCandidate"
}
if (-not (Test-Path -LiteralPath $reportedWorktree -PathType Container)) {
    throw "Coding Engine returned a missing worktree: $reportedWorktree"
}
$ownedRoot = (Get-Item -LiteralPath $ownedRootCandidate -Force).FullName
$ownedMarkerPath = Join-Path $ownedRoot '.local-agent-owned.json'
if (-not (Test-Path -LiteralPath $ownedMarkerPath -PathType Leaf)) {
    throw "Coding Engine owned root marker is missing: $ownedMarkerPath"
}
$ownedMarker = Get-Content -LiteralPath $ownedMarkerPath -Raw | ConvertFrom-Json
$worktree = (Get-Item -LiteralPath $reportedWorktree -Force).FullName
$ownedPrefix = $ownedRoot.TrimEnd([char[]]'\/') + [IO.Path]::DirectorySeparatorChar
if (-not $worktree.StartsWith($ownedPrefix, [StringComparison]::OrdinalIgnoreCase) -or
    $worktree.Equals($ownedRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Coding Engine returned a worktree outside its owned root: $worktree"
}
if (($ownedMarker.schema_version -ne '1.0') -or
    ($ownedMarker.purpose -ne 'local-agent-coding-worktrees') -or
    (-not ([string]$ownedMarker.canonical_root).Equals($ownedRoot, [StringComparison]::OrdinalIgnoreCase)) -or
    (-not ([string]$ownedMarker.platform_root).Equals((Get-Item -LiteralPath $Root).FullName, [StringComparison]::OrdinalIgnoreCase))) {
    throw "Coding Engine owned root marker is invalid: $($ownedMarker | ConvertTo-Json -Depth 4)"
}
if ($worktree.Equals((Get-Item -LiteralPath $Smoke).FullName, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Coding Engine edited the source repository instead of an owned worktree'
}
$worktreeTopLevel = (git -C $worktree rev-parse --show-toplevel).Trim()
if ($LASTEXITCODE -ne 0 -or
    -not (Get-Item -LiteralPath $worktreeTopLevel).FullName.Equals($worktree, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Returned path is not the root of the expected Git worktree: $worktree"
}

$resultPath = Join-Path $worktree 'result.txt'
if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) {
    throw "Gateway/Qwen Code did not create result.txt in the owned worktree. Response: $agentContent"
}
$expectedResultBytes = [Text.Encoding]::UTF8.GetBytes('QWEN_CODE_OK')
$actualResultBytes = [IO.File]::ReadAllBytes($resultPath)
if ($actualResultBytes.Length -ne $expectedResultBytes.Length -or
    [Convert]::ToBase64String($actualResultBytes) -ne [Convert]::ToBase64String($expectedResultBytes)) {
    throw "Qwen Code did not write the exact expected result.txt bytes in $worktree"
}
$worktreeChanges = @((git -C $worktree status --porcelain=v1 --untracked-files=all))
if ($LASTEXITCODE -ne 0 -or $worktreeChanges.Count -ne 1 -or $worktreeChanges[0] -ne '?? result.txt') {
    throw "Owned worktree contains unexpected changes: $($worktreeChanges -join ', ')"
}
if ((git -C $worktree rev-parse HEAD).Trim() -ne $baselineCommit) {
    throw 'Qwen Code created an unexpected commit in the owned worktree'
}

$sourceChanges = @((git -C $Smoke status --porcelain=v1 --untracked-files=all))
if ($LASTEXITCODE -ne 0 -or -not [string]::IsNullOrWhiteSpace(($sourceChanges -join "`n"))) {
    throw "Coding Engine changed the source repository: $($sourceChanges -join ', ')"
}
if ((git -C $Smoke rev-parse HEAD).Trim() -ne $baselineCommit) {
    throw 'Coding Engine changed the source repository HEAD'
}
if (Test-Path -LiteralPath (Join-Path $Smoke 'result.txt')) {
    throw 'Coding Engine leaked result.txt into the source repository'
}
if ((Get-FileHash -LiteralPath (Join-Path $Smoke 'README.md') -Algorithm SHA256).Hash -ne $baselineReadmeHash) {
    throw 'Coding Engine changed README.md in the source repository'
}
$remoteConfiguration = @((git -C $Smoke remote -v))
if (@(Compare-Object -ReferenceObject $baselineRemoteConfiguration -DifferenceObject $remoteConfiguration).Count -ne 0) {
    throw 'Coding Engine altered the source repository remote configuration'
}
$remoteRefs = @((git --git-dir=$SmokeRemote for-each-ref '--format=%(refname)=%(objectname)'))
if ($LASTEXITCODE -ne 0 -or
    @(Compare-Object -ReferenceObject $baselineRemoteRefs -DifferenceObject $remoteRefs).Count -ne 0) {
    throw 'Coding Engine changed the remote refs; an unexpected push may have occurred'
}
Remove-Item -LiteralPath $resultPath -Force
$postFixtureCleanupChanges = @((git -C $worktree status --porcelain=v1 --untracked-files=all))
if ($LASTEXITCODE -ne 0 -or -not [string]::IsNullOrWhiteSpace(($postFixtureCleanupChanges -join "`n"))) {
    throw "Owned worktree did not become clean after removing the exact verified smoke output: $($postFixtureCleanupChanges -join ', ')"
}
$cleanupPreview = Invoke-CodingCli @('cleanup', '--task-id', $SmokeCodingTaskId)
if (($cleanupPreview.status -ne 'preview') -or (-not $cleanupPreview.eligible) -or $cleanupPreview.applied) {
    throw "Coding Engine worktree cleanup preview was not eligible: $($cleanupPreview | ConvertTo-Json -Depth 8)"
}
$cleanupApplied = Invoke-CodingCli @(
    'cleanup',
    '--task-id', $SmokeCodingTaskId,
    '--confirm', $SmokeCodingTaskId
)
if (($cleanupApplied.status -ne 'removed') -or (-not $cleanupApplied.removed) -or ($cleanupApplied.paths_deleted -ne 1)) {
    throw "Coding Engine worktree cleanup did not remove exactly one owned path: $($cleanupApplied | ConvertTo-Json -Depth 8)"
}
$SmokeCleanupComplete = $true
Set-Location $Root
Write-Host "[PASS] Gateway request $requestId passed verification and independent semantic review in owned worktree $worktree; source, HEAD, remote config, and remote refs stayed unchanged"
} finally {
    Set-Location $Root
    if ($SmokeCleanupComplete) {
        $canonicalTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([char[]]'\/') + [IO.Path]::DirectorySeparatorChar
        $canonicalSmoke = [IO.Path]::GetFullPath($SmokeFixtureRoot)
        if ((-not $canonicalSmoke.StartsWith($canonicalTemp, [StringComparison]::OrdinalIgnoreCase)) -or
            (-not (Split-Path -Leaf $canonicalSmoke).StartsWith('local-agent-coding-smoke-', [StringComparison]::Ordinal))) {
            throw "Refusing unsafe smoke fixture cleanup: $canonicalSmoke"
        }
        Remove-Item -LiteralPath $canonicalSmoke -Recurse -Force
    } else {
        Write-Warning "Coding smoke fixture was preserved for diagnosis because owned-worktree cleanup did not complete: $SmokeFixtureRoot"
    }
}

Write-Host '[9/10] Scoped Knowledge Engine lifecycle'
$knowledgeSmokeRoot = Join-Path ([IO.Path]::GetTempPath()) ("local-agent-knowledge-smoke-" + [Guid]::NewGuid().ToString('N'))
try {
    $knowledgeProject = Join-Path $knowledgeSmokeRoot 'project'
    $knowledgeDocs = Join-Path $knowledgeProject 'docs'
    $knowledgeDatabase = Join-Path $knowledgeSmokeRoot 'knowledge.sqlite3'
    $memoryDatabase = Join-Path $knowledgeSmokeRoot 'memory.sqlite3'
    New-Item -ItemType Directory -Path $knowledgeDocs -Force | Out-Null
    [IO.File]::WriteAllText(
        (Join-Path $knowledgeProject 'README.md'),
        "# Disposable Knowledge Fixture`n`nThis repository exists only for the bounded smoke test.`n",
        [Text.UTF8Encoding]::new($false)
    )
    [IO.File]::WriteAllText(
        (Join-Path $knowledgeDocs 'guide.md'),
        "# Operational Guide`n`nThe knowledgepurgesentinel verifies retrieval and deletion.`n",
        [Text.UTF8Encoding]::new($false)
    )
    Set-Location $knowledgeProject
    git init -q
    git config user.email 'local-agent@localhost'
    git config user.name 'Local Agent Knowledge Smoke Test'
    git add -- README.md docs/guide.md
    git commit -qm 'knowledge smoke baseline'
    if ($LASTEXITCODE -ne 0) { throw 'Could not create disposable Knowledge Engine Git fixture' }
    Set-Location $Root

    $knowledgeBaseArguments = @('--database', $knowledgeDatabase, '--memory-database', $memoryDatabase)
    $indexResult = Invoke-KnowledgeCli ($knowledgeBaseArguments + @(
        'index', '--project', $knowledgeProject, '--owner', 'smoke-user', '--approved'
    ))
    if ($indexResult.dry_run -or $indexResult.unchanged -or
        (-not $indexResult.generation_id) -or
        ($indexResult.tracked_files -ne 2) -or
        ($indexResult.allowed_files -ne 2) -or
        ($indexResult.blocked_files -ne 0) -or
        ($indexResult.published_fragments -lt 2)) {
        throw "Knowledge index contract failed: $($indexResult | ConvertTo-Json -Depth 8)"
    }

    $mapResult = Invoke-KnowledgeCli ($knowledgeBaseArguments + @(
        'map', '--project', $knowledgeProject, '--owner', 'smoke-user'
    ))
    $mappedPaths = @($mapResult.files | ForEach-Object { $_.path })
    if (($mapResult.schema_version -ne '1.0') -or $mapResult.stale -or
        (-not $mapResult.untrusted) -or (-not $mapResult.local_only) -or
        ($mapResult.tracked_files_count -ne 2) -or
        ($mappedPaths -notcontains 'README.md') -or
        ($mappedPaths -notcontains 'docs/guide.md')) {
        throw "Knowledge repository map contract failed: $($mapResult | ConvertTo-Json -Depth 8)"
    }

    $retrieveResult = Invoke-KnowledgeCli ($knowledgeBaseArguments + @(
        'retrieve', '--project', $knowledgeProject, '--owner', 'smoke-user',
        '--query', 'knowledgepurgesentinel', '--token-budget', '512', '--max-fragments', '4'
    ))
    $retrieved = @($retrieveResult.fragments)
    if (($retrieved.Count -lt 1) -or
        ($retrieveResult.estimated_tokens -gt $retrieveResult.token_budget) -or
        $retrieveResult.degraded -or
        (-not $retrieved[0].untrusted) -or (-not $retrieved[0].local_only) -or
        ($retrieved[0].content -notmatch 'knowledgepurgesentinel') -or
        (-not $retrieved[0].provenance.source_id)) {
        throw "Knowledge retrieval contract failed: $($retrieveResult | ConvertTo-Json -Depth 10)"
    }
    $sourceId = [string]$retrieved[0].provenance.source_id

    $purgePreview = Invoke-KnowledgeCli ($knowledgeBaseArguments + @(
        'purge-source', '--project', $knowledgeProject, '--owner', 'smoke-user', '--source-id', $sourceId
    ))
    if ($purgePreview.apply -or ($purgePreview.counts.fragments -lt 1)) {
        throw "Knowledge purge preview contract failed: $($purgePreview | ConvertTo-Json -Depth 8)"
    }
    $purgeResult = Invoke-KnowledgeCli ($knowledgeBaseArguments + @(
        'purge-source', '--project', $knowledgeProject, '--owner', 'smoke-user',
        '--source-id', $sourceId, '--confirm', $sourceId
    ))
    if ((-not $purgeResult.apply) -or
        (-not $purgeResult.logical_purge_complete) -or
        (-not $purgeResult.physical_purge_complete) -or
        (-not $purgeResult.memory_invalidation_complete) -or
        (-not $purgeResult.complete)) {
        throw "Knowledge purge contract failed: $($purgeResult | ConvertTo-Json -Depth 10)"
    }

    $afterPurge = Invoke-KnowledgeCli ($knowledgeBaseArguments + @(
        'retrieve', '--project', $knowledgeProject, '--owner', 'smoke-user',
        '--query', 'knowledgepurgesentinel', '--token-budget', '512', '--max-fragments', '4'
    ))
    if (@($afterPurge.fragments).Count -ne 0) {
        throw "Purged Knowledge source remained retrievable: $($afterPurge | ConvertTo-Json -Depth 10)"
    }
    Write-Host '[PASS] Knowledge migration, scoped Git index, map, retrieval, preview, and physical purge'
} finally {
    Set-Location $Root
    Remove-Item -LiteralPath $knowledgeSmokeRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host '[10/10] Coding Engine operational health'
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
    throw "Coding Engine operational health failed: $($codingStatus | ConvertTo-Json -Depth 8)"
}
Write-Host '[PASS] Coding Engine store, event chain, owned registry, and orphan health'

Write-Host 'SMOKE_TEST_OK' -ForegroundColor Green
