$ErrorActionPreference = 'Stop'
$python = 'C:\app\algo_monitor\venv\Scripts\python.exe'
$root = 'C:\app\qmt_bridge_test'
$mappingName = 'Global\QmtAlgoMonitorBridgeTest'
$env:QMT_TEST_SHM_NAME = $mappingName
$gateway = $null
$worker = $null
try {
    $gateway = Start-Process -FilePath $python -ArgumentList @("$root\external_server.py", '--port', '18081', '--config', 'C:\server\server_config.json', '--shm-name', $mappingName) -WorkingDirectory $root -RedirectStandardOutput "$root\gateway_test_stdout.log" -RedirectStandardError "$root\gateway_test_stderr.log" -PassThru
    $worker = Start-Process -FilePath $python -ArgumentList @("$root\test_gateway_worker.py") -WorkingDirectory $root -RedirectStandardOutput "$root\worker_test_stdout.log" -RedirectStandardError "$root\worker_test_stderr.log" -PassThru
    Start-Sleep -Seconds 2
    & $python "$root\test_remote_gateway.py" --port 18081 --config C:\server\server_config.json
    if ($LASTEXITCODE -ne 0) {
        throw "integration test failed with exit code $LASTEXITCODE"
    }
} finally {
    if ($null -ne $worker -and -not $worker.HasExited) {
        Stop-Process -Id $worker.Id -Force
    }
    if ($null -ne $gateway -and -not $gateway.HasExited) {
        Stop-Process -Id $gateway.Id -Force
    }
}
