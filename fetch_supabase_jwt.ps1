param(
    [string]$EnvPath = ".env",
    [string]$Email = "",
    [string]$Password = ""
)

function Get-EnvMap {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path $Path)) {
        throw "Could not find $Path"
    }

    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $idx = $line.IndexOf("=")
        if ($idx -lt 1) { return }
        $key = $line.Substring(0, $idx).Trim()
        $value = $line.Substring($idx + 1).Trim()
        $map[$key] = $value
    }
    return $map
}

function Set-EnvValueInFile {
    param(
        [string]$Path,
        [string]$Key,
        [string]$Value
    )

    $content = Get-Content $Path
    $found = $false
    $updated = $content | ForEach-Object {
        if ($_ -match "^\s*$Key\s*=") {
            $found = $true
            "$Key=$Value"
        }
        else {
            $_
        }
    }

    if (-not $found) {
        $updated += "$Key=$Value"
    }

    Set-Content -Path $Path -Value $updated -Encoding UTF8
}

try {
    $envMap = Get-EnvMap -Path $EnvPath

    $projectUrl = $envMap["TOPREP_API_URL"]
    if (-not $projectUrl) {
        throw "TOPREP_API_URL is missing in $EnvPath"
    }

    $anonKey = $envMap["SUPABASE_ANON_KEY"]
    if (-not $anonKey) {
        throw "SUPABASE_ANON_KEY is missing in $EnvPath"
    }

    if (-not $Email) {
        $Email = Read-Host "Supabase user email"
    }
    if (-not $Password) {
        $secure = Read-Host "Supabase user password" -AsSecureString
        $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        $Password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }

    if ($projectUrl -match "^(https://[^/]+\.supabase\.co)") {
        $authBase = $Matches[1]
    }
    elseif ($projectUrl -match "^(https://[^/]+\.functions\.supabase\.co)") {
        $candidate = $Matches[1] -replace "\.functions\.supabase\.co$", ".supabase.co"
        $authBase = $candidate
    }
    else {
        throw "TOPREP_API_URL must include a Supabase host (*.supabase.co or *.functions.supabase.co)"
    }

    $uri = "$authBase/auth/v1/token?grant_type=password"
    $body = @{ email = $Email; password = $Password } | ConvertTo-Json

    $resp = Invoke-RestMethod -Method Post -Uri $uri -Headers @{ apikey = $anonKey; "Content-Type" = "application/json" } -Body $body

    if (-not $resp.access_token) {
        throw "No access_token returned from Supabase"
    }

    Set-EnvValueInFile -Path $EnvPath -Key "TOPREP_AUTH_TOKEN" -Value $resp.access_token
    Write-Output "Updated TOPREP_AUTH_TOKEN in $EnvPath"
    Write-Output "Token prefix: $($resp.access_token.Substring(0, [Math]::Min(20, $resp.access_token.Length)))..."
}
catch {
    Write-Error $_
    exit 1
}
