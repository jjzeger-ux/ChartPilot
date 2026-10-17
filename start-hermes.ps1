$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONPATH = Join-Path $projectRoot '.runtime'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Python runtime not found. Follow README.md to install Python and run Hermes.'
}
$running = $false
try {
    $response = Invoke-WebRequest 'http://127.0.0.1:5056/static/hermes-mark.svg' -TimeoutSec 2
    $running = $response.StatusCode -eq 200 -and $response.Content -match '#ac8b52'
} catch { }
if (-not $running) {
    Start-Process -FilePath $pythonPath -ArgumentList '-m','flask','--app','app','run','--host','127.0.0.1','--port','5056' -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $projectRoot 'hermes-server.log') -RedirectStandardError (Join-Path $projectRoot 'hermes-error.log')
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 300
        try {
            $response = Invoke-WebRequest 'http://127.0.0.1:5056/static/hermes-mark.svg' -TimeoutSec 1
            if ($response.StatusCode -eq 200) { $running = $true; break }
        } catch { }
    }
}
if (-not $running) { throw 'Hermes could not start. See hermes-error.log.' }
Start-Process 'http://127.0.0.1:5056'
