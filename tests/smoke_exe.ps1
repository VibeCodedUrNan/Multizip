param([string]$Exe, [string]$Py, [string]$Src)
# Launch the built exe on sample archives and read the result dialogs it shows.
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$work = Join-Path $env:TEMP ("mzsmoke_" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory $work | Out-Null
$code = @"
import sys, os
sys.path.insert(0, r'$Src')
import multizip as m
w = r'$work'
os.makedirs(os.path.join(w, 'in'))
open(os.path.join(w, 'in', 'a.txt'), 'w').write('hello' * 1000)
noop = lambda n=1, label=None: None
for fmt in ('7z', 'zip', 'tar.zst', 'tar.gz'):
    m.create_archive(os.path.join(w, 'sample.' + fmt), [os.path.join(w, 'in')], fmt, 6, None, noop, lambda: False)
"@
$code | & $Py -I -B -

function App-Pids($proc) {          # the one-file launcher runs the real app as its child process
    @($proc.Id) + @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($proc.Id)" | ForEach-Object { $_.ProcessId })
}

function Stop-App($proc) {
    foreach ($id in (App-Pids $proc)) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
}

function Read-Dialog($proc, $seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if ($proc.HasExited) { return "(process exited, code $($proc.ExitCode))" }
        $wins = foreach ($id in (App-Pids $proc)) {
            $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, [int]$id)
            [System.Windows.Automation.AutomationElement]::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
        }
        foreach ($w in $wins) {
            $texts = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Text)))
            $t = ($texts | ForEach-Object { $_.Current.Name } | Where-Object { $_ }) -join " | "
            if ($t) { return $t }
        }
        Start-Sleep -Milliseconds 500
    }
    return "(no dialog within $seconds s)"
}

foreach ($fmt in "7z", "zip", "tar.zst", "tar.gz") {
    $p = Start-Process -FilePath $Exe -ArgumentList "t", "`"$work\sample.$fmt`"" -PassThru
    "test sample.$fmt -> " + (Read-Dialog $p 40)
    Stop-App $p
}

# plain start: the main window (with drag-and-drop) must come up and stay up
$p = Start-Process -FilePath $Exe -PassThru
$deadline, $main = (Get-Date).AddSeconds(40), $null
while (-not $p.HasExited -and (Get-Date) -lt $deadline -and -not $main) {
    $main = App-Pids $p | ForEach-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue } | Where-Object { $_.MainWindowTitle } | Select-Object -First 1
    Start-Sleep -Milliseconds 500
}
Start-Sleep -Seconds 3
if (-not $main -or $main.HasExited) { "plain start -> no main window (launcher exited: $($p.HasExited))" }
else {
    "plain start -> window '$($main.MainWindowTitle)' up, still running after 3 s"
    [void]$main.CloseMainWindow()
    if ($p.WaitForExit(15000)) { "  closed cleanly from its X button" } else { Stop-App $p; "  (had to force-close)" }
}
Remove-Item -Recurse -Force $work
