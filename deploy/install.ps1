#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Package,
    [Parameter(Mandatory)][string]$PackageSha256,
    [Parameter(Mandatory)][string]$Nssm,
    [Parameter(Mandatory)][string]$NssmSha256,
    [Parameter(Mandatory)][string]$NssmVersion,
    [Parameter(Mandatory)][string[]]$AllowedCidrs,
    [string]$Root = (Join-Path $env:ProgramData 'NASDesignWorkbench'),
    [switch]$AcceptHttpRisk
)
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-Windows
if (!$AcceptHttpRisk) { throw 'HTTP 会在授权网段内明文传输密码和 Cookie。完成网络风险确认后使用 -AcceptHttpRisk。' }
$Root = [IO.Path]::GetFullPath($Root)
if ($Root -notmatch '^[A-Z]:\\.+') { throw '安装目录必须在本机数据盘内。' }
if (Test-Path -LiteralPath $Root) { throw '安装目录已存在。现有工作台请使用升级流程，保留的数据请使用恢复流程。' }
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) { throw '工作台服务已存在，请使用升级流程。' }
if (Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue) { throw 'TCP 8080 已被占用。' }
$python = Get-Python312
$Nssm = (Resolve-Path -LiteralPath $Nssm).Path
$Package = (Resolve-Path -LiteralPath $Package).Path
Assert-Sha $Package $PackageSha256
Assert-Sha $Nssm $NssmSha256
$present = [string](Get-Item -LiteralPath $Nssm).VersionInfo.ProductVersion
if ($present.Trim() -ne $NssmVersion) { throw "NSSM 产品版本与批准值不一致（实际 $present）。" }
New-Item $Root -ItemType Directory | Out-Null
Protect-Directories $Root
$releaseName = Expand-Release $python $Root $Package $PackageSha256
$release = Join-Path $Root ('releases\' + $releaseName)
Install-PythonDependencies $python $release
Invoke-Ops $release (@('networks', '--cidr') + $AllowedCidrs)
Invoke-Ops $release @('initialize', '--root', $Root)
Invoke-Ops $release @('migrate', '--root', $Root)
Invoke-Native 'icacls.exe' @((Join-Path $Root 'data\secret.key'), '/grant', '*S-1-5-19:(F)')
$tools = Join-Path $Root 'tools'
New-Item $tools -ItemType Directory | Out-Null
$installedNssm = Join-Path $tools 'nssm.exe'
Copy-Item -LiteralPath $Nssm -Destination $installedNssm
Assert-Sha $installedNssm $NssmSha256
$created = $false
$rule = $false
try {
    Invoke-Native $installedNssm @('install', $ServiceName, (Join-Path $release '.venv\Scripts\python.exe'))
    $created = $true
    Set-ServiceRelease $Root $installedNssm $release
    Set-IntranetFirewall $Root $AllowedCidrs
    $rule = $true
    Start-Service -Name $ServiceName
    Test-Workbench $Root $release
    Write-Installation $Root @{ service=$ServiceName; release=$releaseName; nssm_sha256=$NssmSha256; nssm_version=$present; cidrs=$AllowedCidrs }
} catch {
    if ($created) { Stop-Service -Name $ServiceName -ErrorAction SilentlyContinue; & $installedNssm remove $ServiceName confirm }
    if ($rule) { Remove-NetFirewallRule -Name $FirewallName }
    throw
}
