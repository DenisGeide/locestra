function ConvertTo-StrictPlatformBoolean {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Value,
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    if ($Value -is [bool]) {
        return [bool]$Value
    }

    $normalized = ([string]$Value).Trim()
    if (($normalized.StartsWith('"') -and $normalized.EndsWith('"')) -or
        ($normalized.StartsWith("'") -and $normalized.EndsWith("'"))) {
        $normalized = $normalized.Substring(1, $normalized.Length - 2).Trim()
    }
    if ($normalized -ieq 'true') {
        return $true
    }
    if ($normalized -ieq 'false') {
        return $false
    }
    throw "$Name must be exactly true or false"
}

function Get-PlatformBooleanSetting {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [bool]$Default
    )

    $resolved = $Default
    $configPath = Join-Path $Root 'config\platform.json'
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        $config = [IO.File]::ReadAllText($configPath) | ConvertFrom-Json
        $property = $config.settings.PSObject.Properties[$Name]
        if ($null -ne $property) {
            $resolved = ConvertTo-StrictPlatformBoolean -Value $property.Value -Name $Name
        }
    }

    $envPath = Join-Path $Root '.env'
    if (Test-Path -LiteralPath $envPath -PathType Leaf) {
        $escapedName = [Regex]::Escape($Name)
        foreach ($line in [IO.File]::ReadAllLines($envPath)) {
            if ($line -match "^\s*$escapedName\s*=\s*(.*?)\s*$") {
                $candidate = [string]$Matches[1]
                if (-not [string]::IsNullOrWhiteSpace($candidate)) {
                    $resolved = ConvertTo-StrictPlatformBoolean -Value $candidate -Name $Name
                }
            }
        }
    }

    $processValue = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if (-not [string]::IsNullOrWhiteSpace($processValue)) {
        $resolved = ConvertTo-StrictPlatformBoolean -Value $processValue -Name $Name
    }
    return $resolved
}
