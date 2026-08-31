# scripts/setup-weekly-triage.ps1
#
# Register a weekly Windows Task Scheduler job that runs `/triage` via
# scripts/run_triage.py. Idempotent — re-running replaces the entry.
# macOS/Linux have their own installer: scripts/setup-weekly-triage.sh.
#
# Usage (from this repo's root, normal PowerShell — elevation not required):
#   .\scripts\setup-weekly-triage.ps1
#   .\scripts\setup-weekly-triage.ps1 -DayOfWeek Monday -Time 9:00
#   .\scripts\setup-weekly-triage.ps1 -TimeoutHours 4
#   .\scripts\setup-weekly-triage.ps1 -Status
#   .\scripts\setup-weekly-triage.ps1 -Remove
#
# What this does:
#   - Registers a task named "tokenscope weekly triage" that invokes
#     `<python> -u scripts\run_triage.py` weekly at the given day + time,
#     running only when the user is logged in (Interactive logon — no stored
#     password, no service account).
#   - The runner logs to logs\triage-<timestamp>.log, keeps the 12 newest, and
#     kills a run that overshoots its own timeout so the scheduler never has to.
#   - Removes the pre-rename "claude-usage weekly triage" task if it is still
#     registered, so an upgrade does not leave two weekly runs behind.
#
# What this DOES NOT do:
#   - Does not grant any new permissions. Claude Code's settings govern
#     what the routine can do.
#   - Does not push to main. Per /triage workflow, only DEV is pushed.

[CmdletBinding(DefaultParameterSetName = 'Install')]
param(
    [Parameter(ParameterSetName = 'Install')]
    [ValidateSet('Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday')]
    [string]$DayOfWeek = 'Monday',

    [Parameter(ParameterSetName = 'Install')]
    [ValidatePattern('^\d{1,2}:\d{2}$')]
    [string]$Time = '09:00',

    # Hard ceiling for one run. The runner enforces it itself; the task's own
    # limit is set slightly higher so Task Scheduler is the last resort, not the
    # first — a scheduler kill can land mid-merge.
    [Parameter(ParameterSetName = 'Install')]
    [ValidateRange(1, 24)]
    [int]$TimeoutHours = 3,

    # Interpreter to use. Auto-detected when omitted.
    [Parameter(ParameterSetName = 'Install')]
    [string]$Python,

    [Parameter(ParameterSetName = 'Remove')]
    [switch]$Remove,

    [Parameter(ParameterSetName = 'Status')]
    [switch]$Status
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$TaskName  = 'tokenscope weekly triage'
# The task this project registered before it was renamed to TokenScope. It
# points at the retired scripts/run-triage.ps1, so leaving it behind means two
# weekly triage runs, one of them broken.
$LegacyTaskNames = @('claude-usage weekly triage')
$RepoRoot  = Split-Path -Parent $PSScriptRoot
$RunScript = Join-Path $PSScriptRoot 'run_triage.py'
$LogDir    = Join-Path $RepoRoot 'logs'

function Get-TriageTask {
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Remove-LegacyTriageTasks {
    foreach ($legacy in $LegacyTaskNames) {
        if (Get-ScheduledTask -TaskName $legacy -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $legacy -Confirm:$false
            Write-Output "Removed the pre-rename scheduled task '$legacy'."
        }
    }
}

# --------------------------------------------------------------------------- #
# Remove / Status
# --------------------------------------------------------------------------- #

if ($Remove) {
    if (Get-TriageTask) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed scheduled task '$TaskName'."
    } else {
        Write-Output "No scheduled task '$TaskName' found."
    }
    Remove-LegacyTriageTasks
    return
}

if ($Status) {
    $task = Get-TriageTask
    if (-not $task) {
        Write-Output "No scheduled task '$TaskName' installed."
        return
    }
    foreach ($legacy in $LegacyTaskNames) {
        if (Get-ScheduledTask -TaskName $legacy -ErrorAction SilentlyContinue) {
            Write-Output "Warning: the pre-rename task '$legacy' is still registered. Re-run this script (without -Status) to remove it."
        }
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    $action = $task.Actions[0]
    Write-Output "Scheduled task '$TaskName':"
    Write-Output "  State:       $($task.State)"
    Write-Output "  Command:     $($action.Execute) $($action.Arguments)"
    Write-Output "  Next run:    $($info.NextRunTime)"
    Write-Output "  Last run:    $($info.LastRunTime)"
    Write-Output "  Last result: $($info.LastTaskResult)"
    return
}

# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #

if (-not (Test-Path -LiteralPath $RunScript)) {
    throw "Runner script not found: $RunScript. Make sure scripts/run_triage.py is committed."
}

# `-At` must be a real DateTime. Parsing the string ourselves with an invariant
# culture avoids depending on the machine's locale for '09:00'.
$parsedTime = [datetime]::MinValue
$timeFormats = @('HH:mm', 'H:mm')
if (-not [datetime]::TryParseExact(
        $Time,
        $timeFormats,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::None,
        [ref]$parsedTime)) {
    throw "Invalid -Time '$Time'. Expected 24-hour HH:mm, e.g. 09:00 or 21:30."
}

function Resolve-PythonCommand {
    <#
      Returns @{ Exe = <path>; Prefix = @(<args before the script>) }.
      Tries the caller's -Python first, then python / python3 / py -3, and
      *runs* each candidate: on Windows a bare `python` is often the Microsoft
      Store stub, which resolves on PATH but cannot execute anything.
    #>
    $candidates = @()
    if ($Python) {
        $candidates += ,@{ Name = $Python; Prefix = @() }
    } else {
        $candidates += ,@{ Name = 'python';  Prefix = @() }
        $candidates += ,@{ Name = 'python3'; Prefix = @() }
        $candidates += ,@{ Name = 'py';      Prefix = @('-3') }
    }

    foreach ($candidate in $candidates) {
        $cmd = Get-Command $candidate.Name -CommandType Application -ErrorAction SilentlyContinue |
               Select-Object -First 1
        if (-not $cmd) { continue }

        # Check the probe's *output*, not just its exit status: an arbitrary
        # executable passed via -Python (or a Store stub) can exit 0 without
        # being a Python interpreter at all.
        $probe = @($candidate.Prefix) + @(
            '-c',
            'import sys; sys.stdout.write("tokenscope-ok" if sys.version_info[:2] >= (3, 8) else "tokenscope-old")'
        )
        try {
            $out = (& $cmd.Source @probe 2>$null | Out-String)
        } catch {
            continue
        }
        if ($out -and $out.Trim() -eq 'tokenscope-ok') {
            return @{ Exe = $cmd.Source; Prefix = @($candidate.Prefix) }
        }
    }

    if ($Python) {
        throw "-Python '$Python' is not a usable Python 3.8+ interpreter."
    }
    throw 'No Python 3.8+ found on PATH. Install Python, or pass -Python C:\path\to\python.exe.'
}

$python = Resolve-PythonCommand

if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# A scheduled task inherits the *persisted* user PATH from the registry, not
# anything a shell profile adds — so resolve claude here and pass the absolute
# path to the runner. That removes PATH from the equation for the one tool the
# routine cannot start without. (git and gh are looked up by claude itself and
# do live on the persisted PATH.)
$claude = Get-Command claude -CommandType Application -ErrorAction SilentlyContinue |
          Select-Object -First 1
if (-not $claude) {
    Write-Warning "claude was not found on PATH; the task will look it up at run time. If the run logs exit code 127, re-run this installer from a shell where 'claude' works."
}

# `-u` keeps the log live; the runner's own --timeout does the real enforcing.
$argumentList = @($python.Prefix) + @(
    '-u', ('"{0}"' -f $RunScript), '--timeout', ($TimeoutHours * 3600)
)
if ($claude) {
    $argumentList += @('--claude', ('"{0}"' -f $claude.Source))
}
$argumentString = ($argumentList -join ' ')

$action = New-ScheduledTaskAction `
    -Execute $python.Exe `
    -Argument $argumentString `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek $DayOfWeek `
    -At $parsedTime

# The task limit sits 30 minutes above the runner's, so the runner gets to shut
# claude down cleanly and rotate its logs before the scheduler intervenes.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours $TimeoutHours -Minutes 30)

# Interactive logon: task fires only when the user is signed in. No password stored.
# Full DOMAIN\User form works for both local and AD accounts.
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited

$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Run /triage on $RepoRoot every $DayOfWeek at $($parsedTime.ToString('HH:mm')). Pushes DEV, never main."

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Remove-LegacyTriageTasks

Write-Output "Registered scheduled task '$TaskName'."
Write-Output "  Repo:     $RepoRoot"
Write-Output "  Runner:   $($python.Exe) $argumentString"
Write-Output "  Cadence:  every $DayOfWeek at $($parsedTime.ToString('HH:mm'))"
Write-Output "  Timeout:  $TimeoutHours h (task limit $TimeoutHours h 30 m)"
Write-Output "  Runs as:  $userId (Interactive logon - only fires when you are signed in)"
Write-Output "  Logs:     $LogDir\triage-*.log (keeps the 12 newest)"
Write-Output ""
Write-Output "Smoke test (runs now):"
Write-Output "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Output ""
Write-Output "Status:"
Write-Output "  .\scripts\setup-weekly-triage.ps1 -Status"
Write-Output ""
Write-Output "Remove:"
Write-Output "  .\scripts\setup-weekly-triage.ps1 -Remove"
