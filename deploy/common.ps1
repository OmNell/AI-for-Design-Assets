#requires -Version 5.1
$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path $PSScriptRoot -Parent
$ServiceName = 'NASDesignWorkbench'
$FirewallName = 'NASDesignWorkbench-Intranet-8080'

function Invoke-Native {
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "命令失败，退出码 $LASTEXITCODE（$(Split-Path $File -Leaf)）。" }
}

function Assert-Windows {
    if ($env:OS -ne 'Windows_NT') { throw '此部署脚本只适用于 Windows。' }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (!$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw '请在提升权限的 PowerShell 中运行。' }
}

function Get-Python312 {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    $probe = 'import struct,sys; assert sys.version_info[:2] == (3,12) and struct.calcsize("P") == 8; print(sys.executable)'
    if ($launcher) { $answer = & $launcher.Source '-3.12' '-c' $probe }
    else { $answer = & python.exe '-c' $probe }
    if ($LASTEXITCODE -ne 0 -or !$answer) { throw '请安装 64 位 Python 3.12，并选择系统范围安装。' }
    $answer.Trim()
}

function Assert-Sha {
    param([string]$File, [string]$Expected)
    if ($Expected -notmatch '^[0-9a-fA-F]{64}$' -or (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash -ne $Expected) {
        throw 'SHA-256 与批准值不符。'
    }
}

function Protect-Directories {
    param([string]$Root)
    $admin = '*S-1-5-32-544:(OI)(CI)(F)'
    $system = '*S-1-5-18:(OI)(CI)(F)'
    $serviceRead = '*S-1-5-19:(OI)(CI)(RX)'
    $serviceModify = '*S-1-5-19:(OI)(CI)(M)'
    Invoke-Native 'icacls.exe' @($Root, '/inheritance:r', '/grant:r', $admin, $system, $serviceRead)
    foreach ($name in @('data', 'logs', 'backups', 'configuration')) {
        $dir = Join-Path $Root $name
        New-Item $dir -ItemType Directory -Force | Out-Null
        Invoke-Native 'icacls.exe' @($dir, '/inheritance:r', '/grant:r', $admin, $system, $serviceModify)
    }
}

function Read-Installation {
    param([string]$Root)
    $state = Get-Content -LiteralPath (Join-Path $Root 'installation.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($state.service -ne $ServiceName -or $state.release -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+(?:-rc\.[0-9]+)?-[a-f0-9]{8}$' -or
        $state.nssm_sha256 -notmatch '^[0-9a-fA-F]{64}$') { throw '安装状态文件不合法。' }
    $state
}

function Write-Installation {
    param([string]$Root, [object]$State)
    $file = Join-Path $Root 'installation.json'
    $temp = Join-Path $Root ('installation-' + [guid]::NewGuid().ToString('N') + '.tmp')
    $State | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temp -Encoding UTF8
    if (Test-Path -LiteralPath $file) { [IO.File]::Replace($temp, $file, $null) }
    else { [IO.File]::Move($temp, $file) }
}

function Expand-Release {
    param([string]$Python, [string]$Root, [string]$Package, [string]$Hash)
    Assert-Sha $Package $Hash
    $releaseDir = Join-Path $Root 'releases'
    $staging = Join-Path $releaseDir ('stage-' + [guid]::NewGuid().ToString('N'))
    New-Item $releaseDir -ItemType Directory -Force | Out-Null
    Invoke-Native $Python @((Join-Path $RepoRoot 'scripts\build_release.py'), '--verify', $Package, '--expected-sha', $Hash, '--extract', $staging) | Out-Host
    $manifest = Get-Content -LiteralPath (Join-Path $staging 'RELEASE.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $name = $manifest.version + '-' + $Hash.Substring(0, 8).ToLowerInvariant()
    $release = Join-Path $releaseDir $name
    if (Test-Path -LiteralPath $release) { throw '相同发布包已存在，禁止重复覆盖。' }
    Move-Item -LiteralPath $staging -Destination $release
    $name
}

function Install-PythonDependencies {
    param([string]$Python, [string]$Release)
    Invoke-Native $Python @('-m', 'venv', (Join-Path $Release '.venv'))
    $venv = Join-Path $Release '.venv\Scripts\python.exe'
    Invoke-Native $venv @('-m', 'pip', '--disable-pip-version-check', 'install', (Join-Path $Release 'backend'))
}

function Invoke-Ops {
    param([string]$Release, [string[]]$Arguments)
    Invoke-Native (Join-Path $Release '.venv\Scripts\python.exe') (@((Join-Path $Release 'deploy\ops.py')) + $Arguments)
}

function Set-ServiceRelease {
    param([string]$Root, [string]$Nssm, [string]$Release)
    $python = Join-Path $Release '.venv\Scripts\python.exe'
    $config = Join-Path $Root 'configuration\config.yaml'
    if (!(Test-Path -LiteralPath $config -PathType Leaf)) { $config = Join-Path $Root 'config.yaml' }
    Invoke-Native $Nssm @('set', $ServiceName, 'Application', $python)
    Invoke-Native $Nssm @('set', $ServiceName, 'AppDirectory', (Join-Path $Release 'backend'))
    Invoke-Native $Nssm @('set', $ServiceName, 'AppParameters', '-m uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1 --no-proxy-headers')
    Invoke-Native $Nssm @('set', $ServiceName, 'AppEnvironmentExtra', "NASWB_CONFIG=$config")
    Invoke-Native $Nssm @('set', $ServiceName, 'ObjectName', 'NT AUTHORITY\LocalService')
    Invoke-Native $Nssm @('set', $ServiceName, 'Start', 'SERVICE_AUTO_START')
    Invoke-Native $Nssm @('set', $ServiceName, 'AppExit', 'Default', 'Restart')
    Invoke-Native $Nssm @('set', $ServiceName, 'AppRestartDelay', '5000')
    Invoke-Native $Nssm @('set', $ServiceName, 'AppStopMethodConsole', '30000')
    Invoke-Native $Nssm @('set', $ServiceName, 'AppStdout', (Join-Path $Root 'logs\service.stdout.log'))
    Invoke-Native $Nssm @('set', $ServiceName, 'AppStderr', (Join-Path $Root 'logs\service.stderr.log'))
    Invoke-Native $Nssm @('set', $ServiceName, 'AppRotateFiles', '1')
    Invoke-Native $Nssm @('set', $ServiceName, 'AppRotateOnline', '1')
    Invoke-Native $Nssm @('set', $ServiceName, 'AppRotateBytes', '10485760')
}

function Set-IntranetFirewall {
    param([string]$Root, [string[]]$Cidrs)
    Write-Host ("仅在 Domain 和 Private 允许授权来源访问 TCP 8080：" + ($Cidrs -join ', '))
    $existing = Get-NetFirewallRule -Name $FirewallName -ErrorAction SilentlyContinue
    if ($existing) { throw '存在同名防火墙规则，请由 IT 核对旧安装的归属。' }
    $rule = @{ Name=$FirewallName; DisplayName='NAS 设计资产工作台（授权内网）'; Direction='Inbound'; Action='Allow';
               LocalPort=8080; Protocol='TCP'; Profile=@('Domain','Private'); RemoteAddress=$Cidrs; Description="安装目录 $Root" }
    New-NetFirewallRule @rule | Out-Null
}

function Stop-Workbench {
    param([string]$Release)
    $service = Get-Service -Name $ServiceName
    if ($service.Status -ne 'Stopped') {
        $service.Stop()
        $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(60))
    }
    $python = Join-Path $Release '.venv\Scripts\python.exe'
    $remaining = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.ExecutablePath -eq $python }
    if ($remaining) { throw '旧服务工作进程仍在运行，不能操作 SQLite。' }
    if (Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue) {
        throw '端口 8080 尚有监听进程，不能安全切换服务。'
    }
}

function Test-Workbench {
    param([string]$Root, [string]$Release)
    $manifest = Get-Content -LiteralPath (Join-Path $Release 'RELEASE.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    for ($attempt = 0; $attempt -lt 25; $attempt++) {
        try {
            $value = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/api/health' -TimeoutSec 2
            $page = Invoke-WebRequest -Uri 'http://127.0.0.1:8080/' -TimeoutSec 2 -UseBasicParsing
            if ($value.version -eq $manifest.version -and $value.mode -eq 'prod' -and $value.database_available -eq $true -and $page.Content -match '/assets/') {
                Write-Host '发布版本、生产模式、SQLite 与 Vite 首页健康检查通过。'
                return
            }
        } catch { Start-Sleep -Seconds 1 }
    }
    throw "健康检查失败。请查看 $(Join-Path $Root 'logs') 下的应用日志及服务错误日志。"
}
