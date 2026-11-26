#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Package,
    [Parameter(Mandatory)][string]$PackageSha256,
    [string]$Root = (Join-Path $env:ProgramData 'NASDesignWorkbench')
)
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-Windows
$Root = (Resolve-Path -LiteralPath $Root).Path
$state = Read-Installation $Root
$previous = $state.release
$old = Join-Path $Root ('releases\' + $previous)
$nssm = Join-Path $Root 'tools\nssm.exe'
Assert-Sha $nssm $state.nssm_sha256
$python = Get-Python312
Write-Host ("当前版本 " + $previous)
$name = Expand-Release $python $Root $Package $PackageSha256
$release = Join-Path $Root ('releases\' + $name)
Install-PythonDependencies $python $release
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$backup = Join-Path $Root "backups\pre-upgrade-$timestamp.zip"
$log = Join-Path $Root 'logs\upgrades.log'
$prepared = $false
$switched = $false
$legacyConfig = Join-Path $Root 'config.yaml'
$modernConfig = Join-Path $Root 'configuration\config.yaml'
$relocating = !(Test-Path -LiteralPath $modernConfig -PathType Leaf)

$inbound = Get-NetFirewallRule -Name $FirewallName
$wasEnabled = $inbound.Enabled -eq 'True'
Stop-Workbench $old
try {
    if ($wasEnabled) { Disable-NetFirewallRule -InputObject $inbound -ErrorAction Stop }
    Invoke-Ops $old @('snapshot', '--root', $Root, '--package', $backup, '--service-stopped')
    $prepared = $true
    if ($relocating -and (Test-Path -LiteralPath (Join-Path $release 'backend\app\services\nas_settings\state.py'))) {
        Protect-Directories $Root
        Invoke-Ops $release @('relocate-config', '--root', $Root, '--service-stopped')
        Invoke-Native 'icacls.exe' @($modernConfig, '/grant', '*S-1-5-19:(M)')
    }
    Invoke-Ops $release @('migrate', '--root', $Root)
    if (!(Test-Path -LiteralPath (Join-Path $release 'backend\app\static\index.html') -PathType Leaf)) { throw '发布缺少静态首页。' }
    $switched = $true
    Set-ServiceRelease $Root $nssm $release
    Start-Service -Name $ServiceName
    Test-Workbench $Root $release
    if ($relocating -and (Test-Path -LiteralPath $modernConfig -PathType Leaf)) {
        Move-Item -LiteralPath $legacyConfig -Destination (Join-Path $Root "backups\legacy-config-$timestamp.yaml")
    }
    $state.release = $name
    Write-Installation $Root $state
    Add-Content -LiteralPath $log -Encoding UTF8 -Value "$timestamp 成功 $name"
    if ($wasEnabled) { Enable-NetFirewallRule -InputObject $inbound -ErrorAction Stop }
} catch {
    try {
        Add-Content -LiteralPath $log -Encoding UTF8 -Value "$timestamp 升级失败 $name" -ErrorAction SilentlyContinue
        if ($switched) { Stop-Workbench $release }
        if ($prepared) {
            if ($relocating -and (Test-Path -LiteralPath $modernConfig -PathType Leaf)) {
                Move-Item -LiteralPath $modernConfig -Destination (Join-Path $Root "backups\failed-config-$timestamp.yaml")
            }
            Invoke-Ops $old @('rollback', '--root', $Root, '--package', $backup, '--service-stopped')
            Invoke-Native 'icacls.exe' @((Join-Path $Root 'data\secret.key'), '/grant', '*S-1-5-19:(F)')
            if (Test-Path -LiteralPath $modernConfig -PathType Leaf) {
                Invoke-Native 'icacls.exe' @($modernConfig, '/grant', '*S-1-5-19:(M)')
            }
        }
        if ($switched) { Set-ServiceRelease $Root $nssm $old }
        $state.release = $previous
        Write-Installation $Root $state
        Start-Service -Name $ServiceName
        Test-Workbench $Root $old
        Add-Content -LiteralPath $log -Encoding UTF8 -Value "$timestamp 回滚到上一发布并验证健康"
        if ($wasEnabled) { Enable-NetFirewallRule -InputObject $inbound -ErrorAction Stop }
    } catch {
        throw "自动回滚需人工接手。请暂停服务并核对上一版本及本地备份 $backup。"
    }
    throw '升级失败，已恢复上一应用版本及升级前数据库。'
}
