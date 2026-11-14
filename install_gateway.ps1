param(
    [string]$ServiceName = 'algo-monitor-qmt-gateway',
    [string]$PythonPath = 'C:\app\algo_monitor\venv\Scripts\python.exe',
    [string]$NssmPath = 'C:\app\nssm\nssm.exe',
    [string]$GatewayPath = 'C:\server\external_server.py',
    [string]$MappingUser = 'logan'
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "Python not found: $PythonPath"
}
if (-not (Test-Path -LiteralPath $NssmPath)) {
    throw "NSSM not found: $NssmPath"
}
if (-not (Test-Path -LiteralPath $GatewayPath)) {
    throw "Gateway not found: $GatewayPath"
}

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($null -eq $service) {
    & $NssmPath install $ServiceName $PythonPath $GatewayPath | Out-Null
}
& $NssmPath set $ServiceName Application $PythonPath | Out-Null
& $NssmPath set $ServiceName AppParameters "$GatewayPath --mapping-user $MappingUser" | Out-Null
& $NssmPath set $ServiceName AppDirectory 'C:\server' | Out-Null
& $NssmPath set $ServiceName AppStdout 'C:\server\gateway_stdout.log' | Out-Null
& $NssmPath set $ServiceName AppStderr 'C:\server\gateway_stderr.log' | Out-Null
& $NssmPath set $ServiceName AppRotateFiles 1 | Out-Null
& $NssmPath set $ServiceName AppRotateBytes 10485760 | Out-Null
& $NssmPath set $ServiceName Start SERVICE_AUTO_START | Out-Null

if ($null -eq $service) {
    & $NssmPath start $ServiceName | Out-Null
} else {
    & $NssmPath restart $ServiceName | Out-Null
}
Start-Sleep -Seconds 2
Get-Service -Name $ServiceName | Select-Object Name, Status, StartType
