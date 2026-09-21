[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$BackendPort = 8766,
    [ValidateRange(1, 65535)][int]$FrontendPort = 3100,
    [ValidateRange(0, 86400)][int]$DurationSeconds = 0,
    [switch]$SmokeTest,
    [switch]$KeepArtifacts
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $repo 'backend\.venv\Scripts\python.exe'
$node = (Get-Command node -ErrorAction Stop).Source
$next = Join-Path $repo 'frontend\node_modules\next\dist\bin\next'
foreach ($file in @($python, $next)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw "Missing local dependency: $file. Install repository dependencies first; this script downloads nothing."
    }
}
if ($BackendPort -eq $FrontendPort) { throw 'Backend and frontend ports must differ.' }
foreach ($port in @($BackendPort, $FrontendPort)) {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $port)
    try { $listener.Start() }
    catch { throw "Port $port is unavailable. No existing process was stopped; choose another port." }
    finally { $listener.Stop() }
}

$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\')
$directory = Join-Path $tempRoot ('modelpilot-acceptance-' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $directory
$frontendParent = Join-Path $repo '.artifacts'
$null = New-Item -ItemType Directory -Path $frontendParent -Force
$frontend = Join-Path $frontendParent ('acceptance-frontend-' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $frontend
$children = @()
$junction = Join-Path $frontend 'node_modules'

function Start-IsolatedProcess([string]$Executable, [string[]]$Arguments, [string]$Name) {
    $info = [System.Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $Executable
    $info.Arguments = ($Arguments | ForEach-Object { '"' + $_.Replace('"', '\"') + '"' }) -join ' '
    $info.WorkingDirectory = $frontend
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    # Whitelist OS/runtime paths, never inspect or inherit provider credentials,
    # model settings, NODE_OPTIONS, or any existing NEXT_PUBLIC_* configuration.
    $info.EnvironmentVariables.Clear()
    foreach ($key in @('SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'PATH', 'PATHEXT',
                       'APPDATA', 'LOCALAPPDATA', 'USERPROFILE', 'COMSPEC',
                       'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE')) {
        $value = [Environment]::GetEnvironmentVariable($key, 'Process')
        if ($null -ne $value) { $info.EnvironmentVariables[$key] = $value }
    }
    $info.EnvironmentVariables['PYTHONPATH'] = Join-Path $repo 'backend\src'
    $info.EnvironmentVariables['PYTHONUNBUFFERED'] = '1'
    $info.EnvironmentVariables['NEXT_PUBLIC_MODELPILOT_API_URL'] = "http://127.0.0.1:$BackendPort"
    $info.EnvironmentVariables['NEXT_TELEMETRY_DISABLED'] = '1'
    $info.EnvironmentVariables['CI'] = '1'
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $info
    $null = $process.Start()
    return [pscustomobject]@{
        Process = $process; Name = $Name
        Output = $process.StandardOutput.ReadToEndAsync()
        Error = $process.StandardError.ReadToEndAsync()
    }
}

function Wait-ForHttp([string]$Url) {
    $deadline = [DateTime]::UtcNow.AddSeconds(100)
    while ([DateTime]::UtcNow -lt $deadline) {
        foreach ($child in $children) {
            if ($child.Process.HasExited) {
                throw "$($child.Name) exited early. See TEST DATA logs in $directory."
            }
        }
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) { return }
        } catch [System.Net.WebException] {
            if ($null -ne $_.Exception.Response -and [int]$_.Exception.Response.StatusCode -ge 500) {
                throw "Application error from $Url. See TEST DATA logs in $directory."
            }
        }
        Start-Sleep -Milliseconds 300
    }
    throw "Timed out waiting for $Url. See TEST DATA logs in $directory."
}

try {
    # Next loads .env files from its project directory: use a fresh source copy
    # with an explicit allowlist, never the user's actual frontend working tree.
    foreach ($name in @('app', 'components', 'lib', 'public', 'package.json',
                       'package-lock.json', 'tsconfig.json', 'next-env.d.ts', 'next.config.ts')) {
        $source = Join-Path $repo ('frontend\' + $name)
        if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $frontend -Recurse }
    }
    $null = New-Item -ItemType Junction -Path $junction -Target (Join-Path $repo 'frontend\node_modules')
    $children += Start-IsolatedProcess $python @(
        (Join-Path $PSScriptRoot 'dashboard_acceptance.py'), '--directory', $directory,
        '--port', "$BackendPort", '--frontend-port', "$FrontendPort"
    ) 'backend'
    Wait-ForHttp "http://127.0.0.1:$BackendPort/health"
    $children += Start-IsolatedProcess $node @(
        $next, 'dev', $frontend, '--webpack', '--hostname', '127.0.0.1', '--port', "$FrontendPort"
    ) 'frontend'
    Wait-ForHttp "http://127.0.0.1:$FrontendPort"
    Write-Host "TEST DATA ONLY - synthetic fake providers, NOT real model quality measurements."
    Write-Host "Dashboard: http://127.0.0.1:$FrontendPort"
    Write-Host "Backend:   http://127.0.0.1:$BackendPort"
    Write-Host "Resources: $directory"
    Write-Host 'Quality target: openai / test-model-a; mixed failures: gemini / test-model-b.'
    Write-Host 'Suite: test-data-independent-60 / 1; historical suite: test-data-smoke-3 / 1.'
    Write-Host 'Only owned child processes are stopped. No actual .env file or user database is loaded.'
    if ($SmokeTest) {
        $runs = Invoke-RestMethod "http://127.0.0.1:$BackendPort/v1/benchmarks/runs"
        $quality = Invoke-RestMethod "http://127.0.0.1:$BackendPort/v1/benchmarks/quality?provider=openai&model=test-model-a"
        if ($runs.Count -ne 3 -or $quality.quality_score -ne 1 -or $quality.confidence -ne 1) {
            throw 'TEST DATA smoke check failed.'
        }
        Write-Host 'TEST DATA startup / real API / quality smoke check: PASS (not browser acceptance).'
    } elseif ($DurationSeconds -gt 0) {
        Write-Host "Will stop automatically after $DurationSeconds seconds; wait for cleanup to complete."
        Write-Host 'Force-closing/cancelling the terminal can skip cleanup; prefer timed or interactive Enter shutdown.'
        Start-Sleep -Seconds $DurationSeconds
    } else {
        $null = Read-Host 'Press Enter to stop and clean up this acceptance session'
    }
} finally {
    foreach ($child in $children) {
        if (-not $child.Process.HasExited) {
            # Descendants belong to this process; never enumerate/stop by name or port.
            $ownedPid = $child.Process.Id
            & taskkill.exe /PID $ownedPid /T /F 2>$null | Out-Null
            $child.Process.WaitForExit(10000) | Out-Null
        }
        if ($child.Process.HasExited) {
            [System.IO.File]::WriteAllText((Join-Path $directory ($child.Name + '.test-data.log')),
                "TEST DATA ONLY`r`n" + $child.Output.Result + $child.Error.Result)
        }
        $child.Process.Dispose()
    }
    # Remove only the junction itself before considering any recursive cleanup.
    if (Test-Path -LiteralPath $junction) { [System.IO.Directory]::Delete($junction) }
    $resolvedFrontend = [System.IO.Path]::GetFullPath($frontend)
    if ((Split-Path $resolvedFrontend -Parent) -ne $frontendParent -or
        (Split-Path $resolvedFrontend -Leaf) -notmatch '^acceptance-frontend-[a-f0-9]{32}$') {
        throw 'Refusing cleanup: temporary frontend boundary validation failed.'
    }
    Remove-Item -LiteralPath $resolvedFrontend -Recurse -Force
    $resolved = [System.IO.Path]::GetFullPath($directory)
    if ((Split-Path $resolved -Parent).TrimEnd('\') -ne $tempRoot -or
        (Split-Path $resolved -Leaf) -notmatch '^modelpilot-acceptance-[a-f0-9]{32}$') {
        throw 'Refusing cleanup: temporary directory boundary validation failed.'
    }
    if ($KeepArtifacts) {
        Write-Host "TEST DATA logs and isolated SQLite retained at: $resolved"
        Write-Host "Cleanup after inspection: Remove-Item -LiteralPath '$resolved' -Recurse -Force"
    } else {
        Remove-Item -LiteralPath $resolved -Recurse -Force
        Write-Host 'Owned services stopped; isolated TEST DATA resources removed.'
    }
}
