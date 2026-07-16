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

Write-Host '[1/8] Python routing tests'
uv run pytest

Write-Host '[2/8] OpenAI-compatible automatic gateway'
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

Write-Host '[3/8] Automatic route matrix'
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

Write-Host '[4/8] Gateway tool calling and strict SSE stream'
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

Write-Host '[5/8] Playwright browser'
$browser = npm run --silent browser:health
if ($browser -notmatch 'PLAYWRIGHT_OK') { throw 'Playwright semantic test failed' }
Write-Host '[PASS] Playwright Chromium'

Write-Host '[6/8] Voice model load'
$voice = Invoke-RestMethod -Uri 'http://127.0.0.1:8788/health?load_model=true' -TimeoutSec 900
if (-not $voice.loaded) { throw 'faster-whisper model did not load' }
Write-Host "[PASS] faster-whisper $($voice.model) loaded on $($voice.device)"

Write-Host '[7/8] Full gateway-to-Qwen Code edit cycle'
$Smoke = Join-Path $Root 'smoke-workspace'
if (Test-Path $Smoke) { Remove-Item -Recurse -Force $Smoke }
New-Item -ItemType Directory -Path $Smoke | Out-Null
Set-Location $Smoke
git init -q
git config user.email 'local-agent@localhost'
git config user.name 'Local Agent Smoke Test'
Set-Content -Encoding utf8 'README.md' "Create result.txt containing exactly QWEN_CODE_OK and no extra characters."
git add README.md
git commit -qm 'smoke baseline'
$env:QWEN_CODE_SUPPRESS_YOLO_WARNING = '1'
$baselineCommit = git rev-parse HEAD
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
Set-Location $Smoke
if (-not (Test-Path 'result.txt')) { throw "Gateway/Qwen Code did not create result.txt. Response: $($agentResponse.Content)" }
$result = [IO.File]::ReadAllText((Join-Path $Smoke 'result.txt'))
if ($result -ne 'QWEN_CODE_OK') { throw "Qwen Code wrote unexpected content: $result" }
$gitChanges = git status --porcelain
if ($gitChanges -notmatch 'result\.txt') { throw 'Qwen Code change is not visible in git status' }
if ((git rev-parse HEAD) -ne $baselineCommit) { throw 'Qwen Code created an unexpected commit' }
Set-Location $Root
Write-Host '[PASS] Multiline repository request routed through gateway and edited the real workspace without a commit'

Write-Host '[8/8] Scoped Knowledge Engine lifecycle'
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

Write-Host 'SMOKE_TEST_OK' -ForegroundColor Green
