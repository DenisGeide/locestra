$script:OwnershipSchemaVersion = 1

function Get-CanonicalOwnershipRoot {
    param([Parameter(Mandatory = $true)][string]$Root)

    return [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
}

function Get-OwnershipPaths {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $canonicalRoot = Get-CanonicalOwnershipRoot $Root
    return [pscustomobject]@{
        Pid = Join-Path $canonicalRoot "run\$Name.pid"
        Owner = Join-Path $canonicalRoot "run\$Name.owner.json"
    }
}

function Get-ProcessSnapshot {
    param([Parameter(Mandatory = $true)][int]$TargetProcessId)

    if ($TargetProcessId -le 0) { return $null }
    try {
        $cim = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $TargetProcessId" -ErrorAction Stop
        if (-not $cim) { return $null }
        $nativeProcess = Get-Process -Id $TargetProcessId -ErrorAction Stop
        return [pscustomobject]@{
            ProcessId = [int]$cim.ProcessId
            ParentProcessId = [int]$cim.ParentProcessId
            ExecutablePath = [string]$cim.ExecutablePath
            CommandLine = [string]$cim.CommandLine
            StartTimeUtc = $nativeProcess.StartTime.ToUniversalTime()
        }
    } catch {
        return $null
    }
}

function Get-ProcessIdentityText {
    param(
        [Parameter(Mandatory = $true)][int]$TargetProcessId,
        [int]$MaximumAncestors = 6
    )

    $parts = New-Object System.Collections.Generic.List[string]
    $seen = @{}
    $currentProcessId = $TargetProcessId
    $depth = 0
    while (($currentProcessId -gt 0) -and ($depth -le $MaximumAncestors) -and (-not $seen.ContainsKey($currentProcessId))) {
        $seen[$currentProcessId] = $true
        $snapshot = Get-ProcessSnapshot $currentProcessId
        if (-not $snapshot) { break }
        if ($snapshot.ExecutablePath) { $parts.Add($snapshot.ExecutablePath) }
        if ($snapshot.CommandLine) { $parts.Add($snapshot.CommandLine) }
        $currentProcessId = $snapshot.ParentProcessId
        $depth++
    }
    return ($parts -join "`n")
}

function Test-OwnershipBoundaryCharacter {
    param(
        [Parameter(Mandatory = $true)][char]$Character,
        [switch]$AllowPathSeparator
    )

    if ([char]::IsWhiteSpace($Character)) { return $true }
    $code = [int]$Character
    if ($AllowPathSeparator -and ($code -in @(47, 92))) { return $true }
    return $code -in @(34, 39, 40, 41, 44, 59, 61, 91, 93)
}

function Test-OwnershipRootInIdentityText {
    param(
        [Parameter(Mandatory = $true)][string]$IdentityText,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $canonicalRoot = Get-CanonicalOwnershipRoot $Root
    $offset = 0
    while ($offset -lt $IdentityText.Length) {
        $index = $IdentityText.IndexOf(
            $canonicalRoot,
            $offset,
            [StringComparison]::OrdinalIgnoreCase
        )
        if ($index -lt 0) { return $false }

        $afterIndex = $index + $canonicalRoot.Length
        $beforeIsBoundary = ($index -eq 0) -or
            (Test-OwnershipBoundaryCharacter $IdentityText[$index - 1])
        $afterIsBoundary = ($afterIndex -eq $IdentityText.Length) -or
            (Test-OwnershipBoundaryCharacter $IdentityText[$afterIndex] -AllowPathSeparator)
        if ($beforeIsBoundary -and $afterIsBoundary) { return $true }
        $offset = $index + 1
    }
    return $false
}

function Test-ProcessIdentity {
    param(
        [Parameter(Mandatory = $true)][int]$TargetProcessId,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string[]]$Fragments,
        [bool]$RequireRootIdentity = $true,
        [string]$ExpectedStartTimeUtc = ''
    )

    $snapshot = Get-ProcessSnapshot $TargetProcessId
    if (-not $snapshot) { return $false }
    $directIdentity = "$($snapshot.ExecutablePath)`n$($snapshot.CommandLine)"
    foreach ($fragment in $Fragments) {
        if ([string]::IsNullOrWhiteSpace($fragment)) { continue }
        if ($directIdentity.IndexOf($fragment, [StringComparison]::OrdinalIgnoreCase) -lt 0) { return $false }
    }
    if ($RequireRootIdentity) {
        $identity = Get-ProcessIdentityText $TargetProcessId
        if (-not (Test-OwnershipRootInIdentityText $identity $Root)) { return $false }
    }
    if ($ExpectedStartTimeUtc) {
        try {
            $expected = [DateTime]::Parse(
                $ExpectedStartTimeUtc,
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::RoundtripKind
            ).ToUniversalTime()
        } catch {
            return $false
        }
        if ([Math]::Abs(($snapshot.StartTimeUtc - $expected).TotalSeconds) -gt 0.05) { return $false }
    }
    return $true
}

function Get-ListenerProcessIds {
    param([Parameter(Mandatory = $true)][int]$Port)

    return @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object { [int]$_ }
    )
}

function Get-VerifiedListenerProcessId {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string[]]$Fragments,
        [bool]$RequireRootIdentity = $true
    )

    $listeners = @(Get-ListenerProcessIds $Port)
    if ($listeners.Count -eq 0) { return $null }
    if ($listeners.Count -ne 1) {
        throw "Port $Port has multiple listener owners; refusing to adopt it"
    }
    $listenerProcessId = [int]$listeners[0]
    if (-not (Test-ProcessIdentity $listenerProcessId $Root $Fragments $RequireRootIdentity)) {
        throw "Port $Port is owned by an unrecognized process; refusing to adopt it"
    }
    return $listenerProcessId
}

function Wait-VerifiedListenerProcessId {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string[]]$Fragments,
        [bool]$RequireRootIdentity = $true,
        [int]$LauncherProcessId = 0,
        [int]$Seconds = 120
    )

    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        $listeners = @(Get-ListenerProcessIds $Port)
        if ($listeners.Count -gt 0) {
            $listenerProcessId = Get-VerifiedListenerProcessId $Root $Port $Fragments $RequireRootIdentity
            if (($LauncherProcessId -gt 0) -and
                (-not (Test-ProcessDescendsFrom ([int]$listenerProcessId) $LauncherProcessId))) {
                throw "Verified listener on port $Port does not belong to launcher $LauncherProcessId"
            }
            return $listenerProcessId
        }
        if (($LauncherProcessId -gt 0) -and (-not (Get-ProcessSnapshot $LauncherProcessId))) {
            throw "Launcher process $LauncherProcessId exited before port $Port became ready"
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    throw "Timed out waiting for verified listener on port $Port"
}

function Test-ProcessDescendsFrom {
    param(
        [Parameter(Mandatory = $true)][int]$TargetProcessId,
        [Parameter(Mandatory = $true)][int]$AncestorProcessId,
        [int]$MaximumAncestors = 8
    )

    $currentProcessId = $TargetProcessId
    $seen = @{}
    for ($depth = 0; $depth -le $MaximumAncestors; $depth++) {
        if ($currentProcessId -eq $AncestorProcessId) { return $true }
        if (($currentProcessId -le 0) -or $seen.ContainsKey($currentProcessId)) { return $false }
        $seen[$currentProcessId] = $true
        $snapshot = Get-ProcessSnapshot $currentProcessId
        if (-not $snapshot) { return $false }
        $currentProcessId = $snapshot.ParentProcessId
    }
    return $false
}

function Find-VerifiedProcessId {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string[]]$Fragments,
        [bool]$RequireRootIdentity = $true,
        [int]$AncestorProcessId = 0
    )

    $matches = @()
    foreach ($candidate in @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue)) {
        $candidateProcessId = [int]$candidate.ProcessId
        if (($AncestorProcessId -gt 0) -and (-not (Test-ProcessDescendsFrom $candidateProcessId $AncestorProcessId))) {
            continue
        }
        if (Test-ProcessIdentity $candidateProcessId $Root $Fragments $RequireRootIdentity) {
            $matches += $candidateProcessId
        }
    }
    $matches = @($matches | Select-Object -Unique)
    if ($matches.Count -eq 0) { return $null }
    if ($matches.Count -gt 1) {
        $nonLauncherMatches = @($matches | Where-Object { $_ -ne $AncestorProcessId })
        if (($AncestorProcessId -gt 0) -and ($nonLauncherMatches.Count -eq 1)) {
            return [int]$nonLauncherMatches[0]
        }
        throw 'Multiple processes match the expected command; refusing ambiguous ownership'
    }
    return [int]$matches[0]
}

function Wait-VerifiedProcessId {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string[]]$Fragments,
        [Parameter(Mandatory = $true)][int]$LauncherProcessId,
        [bool]$RequireRootIdentity = $true,
        [int]$Seconds = 30
    )

    $deadline = (Get-Date).AddSeconds($Seconds)
    $launcherFallback = $null
    do {
        $match = Find-VerifiedProcessId $Root $Fragments $RequireRootIdentity $LauncherProcessId
        if ($null -ne $match) {
            if ([int]$match -ne $LauncherProcessId) { return [int]$match }
            $launcherFallback = [int]$match
        }
        if (-not (Get-ProcessSnapshot $LauncherProcessId)) {
            throw "Launcher process $LauncherProcessId exited before a verified worker appeared"
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    if ($null -ne $launcherFallback) { return [int]$launcherFallback }
    throw 'Timed out waiting for a verified worker process'
}

function Stop-VerifiedLaunchTree {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string[]]$Fragments,
        [Parameter(Mandatory = $true)][int]$LauncherProcessId,
        [bool]$RequireRootIdentity = $true,
        [int]$Seconds = 10
    )

    $candidates = @()
    foreach ($candidate in @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue)) {
        $candidateProcessId = [int]$candidate.ProcessId
        if ((Test-ProcessDescendsFrom $candidateProcessId $LauncherProcessId) -and
            (Test-ProcessIdentity $candidateProcessId $Root $Fragments $RequireRootIdentity)) {
            $candidates += $candidateProcessId
        }
    }
    $candidates = @($candidates | Select-Object -Unique)
    foreach ($candidateProcessId in @($candidates | Where-Object { $_ -ne $LauncherProcessId })) {
        Stop-Process -Id $candidateProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($candidates -contains $LauncherProcessId) {
        Stop-Process -Id $LauncherProcessId -Force -ErrorAction SilentlyContinue
    }
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        $remaining = @($candidates | Where-Object { Get-ProcessSnapshot ([int]$_) })
        if ($remaining.Count -eq 0) { return }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    throw "Verified launch tree rooted at $LauncherProcessId did not exit within $Seconds seconds"
}

function Remove-ProcessOwnershipFiles {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $paths = Get-OwnershipPaths $Root $Name
    Remove-Item -LiteralPath $paths.Owner -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $paths.Pid -Force -ErrorAction SilentlyContinue
}

function Write-ProcessOwnership {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][int]$TargetProcessId,
        [Parameter(Mandatory = $true)][string[]]$Fragments,
        [bool]$RequireRootIdentity = $true
    )

    $canonicalRoot = Get-CanonicalOwnershipRoot $Root
    if (-not (Test-ProcessIdentity $TargetProcessId $canonicalRoot $Fragments $RequireRootIdentity)) {
        throw "Process $TargetProcessId does not match ownership identity for $Name"
    }
    if ($Port -gt 0) {
        $listeners = @(Get-ListenerProcessIds $Port)
        if (($listeners.Count -ne 1) -or ([int]$listeners[0] -ne $TargetProcessId)) {
            throw "Process $TargetProcessId is not the unique listener on port $Port"
        }
    }
    $snapshot = Get-ProcessSnapshot $TargetProcessId
    if (-not $snapshot) { throw "Process $TargetProcessId exited before ownership registration" }
    $paths = Get-OwnershipPaths $canonicalRoot $Name
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $paths.Owner) | Out-Null
    $record = [ordered]@{
        version = $script:OwnershipSchemaVersion
        root = $canonicalRoot
        name = $Name
        port = $Port
        pid = $TargetProcessId
        fragments = @($Fragments)
        require_root_identity = $RequireRootIdentity
        process_start_time_utc = $snapshot.StartTimeUtc.ToString('o')
        timestamp_utc = [DateTime]::UtcNow.ToString('o')
    }
    $temporaryOwnerPath = "$($paths.Owner).$PID.tmp"
    $record | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $temporaryOwnerPath -Encoding UTF8
    Move-Item -LiteralPath $temporaryOwnerPath -Destination $paths.Owner -Force
    Set-Content -LiteralPath $paths.Pid -Encoding Ascii -Value $TargetProcessId
    return [pscustomobject]$record
}

function Test-FragmentRecord {
    param([object[]]$RecordedFragments, [string[]]$ExpectedFragments)

    if (@($RecordedFragments).Count -ne @($ExpectedFragments).Count) { return $false }
    for ($index = 0; $index -lt $ExpectedFragments.Count; $index++) {
        if (-not [string]::Equals(
            [string]$RecordedFragments[$index],
            [string]$ExpectedFragments[$index],
            [StringComparison]::OrdinalIgnoreCase
        )) { return $false }
    }
    return $true
}

function Get-ValidProcessOwnership {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string[]]$Fragments,
        [bool]$RequireRootIdentity = $true
    )

    $canonicalRoot = Get-CanonicalOwnershipRoot $Root
    $paths = Get-OwnershipPaths $canonicalRoot $Name
    if (-not (Test-Path -LiteralPath $paths.Owner)) { return $null }
    try {
        $record = Get-Content -LiteralPath $paths.Owner -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([int]$record.version -ne $script:OwnershipSchemaVersion) { throw 'schema mismatch' }
        if (-not [string]::Equals([string]$record.root, $canonicalRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'root mismatch' }
        if (-not [string]::Equals([string]$record.name, $Name, [StringComparison]::Ordinal)) { throw 'name mismatch' }
        if ([int]$record.port -ne $Port) { throw 'port mismatch' }
        if ([bool]$record.require_root_identity -ne $RequireRootIdentity) { throw 'root identity mismatch' }
        if (-not (Test-FragmentRecord @($record.fragments) $Fragments)) { throw 'fragment mismatch' }
        [void][DateTime]::Parse(
            [string]$record.timestamp_utc,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        )
        $ownedProcessId = [int]$record.pid
        if ($ownedProcessId -le 0) { throw 'invalid pid' }
        if (Test-Path -LiteralPath $paths.Pid) {
            $legacyValue = 0
            if (-not [int]::TryParse((Get-Content -LiteralPath $paths.Pid -Raw).Trim(), [ref]$legacyValue)) { throw 'invalid pid file' }
            if ($legacyValue -ne $ownedProcessId) { throw 'pid file mismatch' }
        }
        if (-not (Test-ProcessIdentity $ownedProcessId $canonicalRoot $Fragments $RequireRootIdentity ([string]$record.process_start_time_utc))) {
            throw 'process identity or start time mismatch'
        }
        if ($Port -gt 0) {
            $listeners = @(Get-ListenerProcessIds $Port)
            if (($listeners.Count -ne 1) -or ([int]$listeners[0] -ne $ownedProcessId)) { throw 'listener mismatch' }
        }
        return [pscustomobject]@{
            ProcessId = $ownedProcessId
            Record = $record
            Legacy = $false
        }
    } catch {
        Remove-ProcessOwnershipFiles $canonicalRoot $Name
        return $null
    }
}

function Get-LegacyProcessEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $paths = Get-OwnershipPaths $Root $Name
    if (-not (Test-Path -LiteralPath $paths.Pid)) { return $null }
    $legacyProcessId = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $paths.Pid -Raw).Trim(), [ref]$legacyProcessId)) {
        Remove-Item -LiteralPath $paths.Pid -Force -ErrorAction SilentlyContinue
        return $null
    }
    return [pscustomobject]@{
        ProcessId = $legacyProcessId
        WrittenAtUtc = (Get-Item -LiteralPath $paths.Pid).LastWriteTimeUtc
    }
}

function Test-LegacyProcessTimestamp {
    param(
        [Parameter(Mandatory = $true)][int]$TargetProcessId,
        [Parameter(Mandatory = $true)][DateTime]$WrittenAtUtc,
        [int]$MaximumDifferenceSeconds = 2
    )

    $snapshot = Get-ProcessSnapshot $TargetProcessId
    if (-not $snapshot) { return $false }
    return [Math]::Abs(($snapshot.StartTimeUtc - $WrittenAtUtc.ToUniversalTime()).TotalSeconds) -le $MaximumDifferenceSeconds
}

function Resolve-ProcessOwnership {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string[]]$Fragments,
        [bool]$RequireRootIdentity = $true,
        [switch]$AllowLegacy,
        [switch]$AllowMatchingAdoption
    )

    $paths = Get-OwnershipPaths $Root $Name
    $hadOwnerRecord = Test-Path -LiteralPath $paths.Owner
    $valid = Get-ValidProcessOwnership $Root $Name $Port $Fragments $RequireRootIdentity
    if ($valid) { return $valid }
    if ($hadOwnerRecord) {
        # Invalid owner metadata is treated as stale or tampered. Never fall back
        # to an unauthenticated PID from the same record.
        return $null
    }

    $candidateProcessId = $null
    $legacyEvidence = Get-LegacyProcessEvidence $Root $Name
    $legacyProcessId = if ($legacyEvidence) { [int]$legacyEvidence.ProcessId } else { $null }
    if ($AllowLegacy -and ($null -ne $legacyProcessId)) {
        if (-not (Test-LegacyProcessTimestamp ([int]$legacyProcessId) $legacyEvidence.WrittenAtUtc)) {
            Remove-Item -LiteralPath $paths.Pid -Force -ErrorAction SilentlyContinue
            return $null
        }
        if ($Port -gt 0) {
            try {
                $listenerProcessId = Get-VerifiedListenerProcessId $Root $Port $Fragments $RequireRootIdentity
                if (($null -ne $listenerProcessId) -and (
                    ([int]$listenerProcessId -eq [int]$legacyProcessId) -or
                    (Test-ProcessDescendsFrom ([int]$listenerProcessId) ([int]$legacyProcessId))
                ) -and (Test-LegacyProcessTimestamp ([int]$listenerProcessId) $legacyEvidence.WrittenAtUtc)) {
                    $candidateProcessId = [int]$listenerProcessId
                }
            } catch {
                Remove-Item -LiteralPath $paths.Pid -Force -ErrorAction SilentlyContinue
                throw
            }
        } else {
            try {
                $candidateProcessId = Find-VerifiedProcessId $Root $Fragments $RequireRootIdentity ([int]$legacyProcessId)
            } catch {
                Remove-Item -LiteralPath $paths.Pid -Force -ErrorAction SilentlyContinue
                throw
            }
            if (($null -eq $candidateProcessId) -and
                (Test-ProcessIdentity ([int]$legacyProcessId) $Root $Fragments $RequireRootIdentity)) {
                $candidateProcessId = [int]$legacyProcessId
            }
        }
        if (($null -ne $candidateProcessId) -and
            (-not (Test-LegacyProcessTimestamp ([int]$candidateProcessId) $legacyEvidence.WrittenAtUtc))) {
            Remove-Item -LiteralPath $paths.Pid -Force -ErrorAction SilentlyContinue
            return $null
        }
        if ($null -eq $candidateProcessId) {
            Remove-Item -LiteralPath $paths.Pid -Force -ErrorAction SilentlyContinue
            return $null
        }
    }

    if (($null -eq $candidateProcessId) -and $AllowMatchingAdoption) {
        if ($Port -gt 0) {
            $candidateProcessId = Get-VerifiedListenerProcessId $Root $Port $Fragments $RequireRootIdentity
        } else {
            $candidateProcessId = Find-VerifiedProcessId $Root $Fragments $RequireRootIdentity
        }
    }

    if ($null -eq $candidateProcessId) {
        if ($null -ne $legacyProcessId) {
            Remove-Item -LiteralPath $paths.Pid -Force -ErrorAction SilentlyContinue
        }
        return $null
    }
    $record = Write-ProcessOwnership $Root $Name $Port ([int]$candidateProcessId) $Fragments $RequireRootIdentity
    return [pscustomobject]@{
        ProcessId = [int]$candidateProcessId
        Record = $record
        Legacy = ($null -ne $legacyProcessId)
    }
}

function Stop-OwnedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string[]]$Fragments,
        [bool]$RequireRootIdentity = $true,
        [int]$Seconds = 20,
        [switch]$AllowLegacy
    )

    $ownership = Resolve-ProcessOwnership $Root $Name $Port $Fragments $RequireRootIdentity -AllowLegacy:$AllowLegacy
    if (-not $ownership) { return $false }
    $ownedProcessId = [int]$ownership.ProcessId
    $record = $ownership.Record
    if (-not (Test-ProcessIdentity $ownedProcessId $Root $Fragments $RequireRootIdentity ([string]$record.process_start_time_utc))) {
        Remove-ProcessOwnershipFiles $Root $Name
        return $false
    }
    if ($Port -gt 0) {
        $listeners = @(Get-ListenerProcessIds $Port)
        if (($listeners.Count -ne 1) -or ([int]$listeners[0] -ne $ownedProcessId)) {
            Remove-ProcessOwnershipFiles $Root $Name
            return $false
        }
    }

    try {
        Stop-Process -Id $ownedProcessId -Force -ErrorAction Stop
    } catch {
        if (-not (Get-ProcessSnapshot $ownedProcessId)) {
            Remove-ProcessOwnershipFiles $Root $Name
            if (($Port -gt 0) -and (@(Get-ListenerProcessIds $Port).Count -gt 0)) {
                throw "Owned process $Name exited, but port $Port was reoccupied; the new listener was not stopped"
            }
            return $true
        }
        throw
    }
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        if (-not (Get-ProcessSnapshot $ownedProcessId)) {
            Remove-ProcessOwnershipFiles $Root $Name
            if (($Port -gt 0) -and (@(Get-ListenerProcessIds $Port).Count -gt 0)) {
                throw "Owned process $Name exited, but port $Port was reoccupied; the new listener was not stopped"
            }
            return $true
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    throw "Owned process $Name ($ownedProcessId) did not exit within $Seconds seconds"
}
