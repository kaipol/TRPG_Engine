param(
    [Parameter(Mandatory = $true)]
    [string]$PythonPath,

    [Parameter(Mandatory = $true)]
    [string]$ScriptPath,

    [Parameter(Mandatory = $true)]
    [string]$WorkingDirectory,

    [Parameter(Mandatory = $true)]
    [string]$PidFile
)

$ErrorActionPreference = "Stop"

$PythonPath = [System.IO.Path]::GetFullPath($PythonPath)
$ScriptPath = [System.IO.Path]::GetFullPath($ScriptPath)
$WorkingDirectory = [System.IO.Path]::GetFullPath($WorkingDirectory).TrimEnd("\")
$PidFile = [System.IO.Path]::GetFullPath($PidFile)
$RuntimeDirectory = [System.IO.Path]::GetDirectoryName($PidFile)
$script:ServerProcess = $null

if (-not (Test-Path -LiteralPath $RuntimeDirectory)) {
    New-Item -ItemType Directory -Path $RuntimeDirectory | Out-Null
}

function Get-CommandLineForPid {
    param([int]$ProcessId)

    try {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
        return [string]$processInfo.CommandLine
    } catch {
        try {
            $processInfo = Get-WmiObject Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
            return [string]$processInfo.CommandLine
        } catch {
            return ""
        }
    }
}

function Test-ManagedServerProcess {
    param([int]$ProcessId)

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $process) {
        return $false
    }

    $commandLine = (Get-CommandLineForPid -ProcessId $ProcessId).ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($commandLine)) {
        return $false
    }

    return (
        $commandLine.Contains($PythonPath.ToLowerInvariant()) -and
        $commandLine.Contains($ScriptPath.ToLowerInvariant()) -and
        $commandLine.Contains($WorkingDirectory.ToLowerInvariant())
    )
}

function Stop-ManagedPid {
    param(
        [int]$ProcessId,
        [string]$Reason
    )

    if (-not (Test-ManagedServerProcess -ProcessId $ProcessId)) {
        return
    }

    Write-Host "[cleanup] Stopping $Reason Python process PID $ProcessId..."
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    try {
        Wait-Process -Id $ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    } catch {
        # The process may already be gone.
    }
}

function Stop-StaleLauncherProcess {
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return
    }

    $pidText = (Get-Content -LiteralPath $PidFile -Raw -ErrorAction SilentlyContinue).Trim()
    $oldPid = 0
    if ([int]::TryParse($pidText, [ref]$oldPid)) {
        Stop-ManagedPid -ProcessId $oldPid -Reason "previous launcher-managed"
    }

    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

function Remove-MatchingPidFile {
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return
    }

    $pidText = (Get-Content -LiteralPath $PidFile -Raw -ErrorAction SilentlyContinue).Trim()
    if ($script:ServerProcess -and $pidText -eq [string]$script:ServerProcess.Id) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    }
}

function Stop-CurrentServerProcess {
    if ($script:ServerProcess -and -not $script:ServerProcess.HasExited) {
        Stop-ManagedPid -ProcessId $script:ServerProcess.Id -Reason "current launcher-managed"
    }
}

try {
    Stop-StaleLauncherProcess

    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    $processInfo.FileName = $PythonPath
    $processInfo.Arguments = '"' + $ScriptPath.Replace('"', '\"') + '"'
    $processInfo.WorkingDirectory = $WorkingDirectory
    $processInfo.UseShellExecute = $false
    $processInfo.EnvironmentVariables["TRPG_LAUNCHER_PARENT_PID"] = [string]$PID
    $processInfo.EnvironmentVariables["TRPG_LAUNCHER_PID_FILE"] = $PidFile

    $script:ServerProcess = [System.Diagnostics.Process]::Start($processInfo)
    Set-Content -LiteralPath $PidFile -Value $script:ServerProcess.Id -Encoding ASCII

    Write-Host "[launcher] Server Python PID: $($script:ServerProcess.Id)"
    $script:ServerProcess.WaitForExit()
    exit $script:ServerProcess.ExitCode
} finally {
    Stop-CurrentServerProcess
    Remove-MatchingPidFile
}
