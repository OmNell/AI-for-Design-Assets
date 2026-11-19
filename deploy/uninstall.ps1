#requires -Version 5.1
[CmdletBinding(SupportsShouldProcess, ConfirmImpact='High')]
param([string]$Root = (Join-Path $env:ProgramData 'NASDesignWorkbench'))
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-Windows
$Root = (Resolve-Path -LiteralPath $Root).Path
$state = Read-Installation $Root
$nssm = Join-Path $Root 'tools\nssm.exe'
Assert-Sha $nssm $state.nssm_sha256
if (!$PSCmdlet.ShouldProcess($Root, '停用工作台服务并移除托管应用程序')) { return }
$release = Join-Path $Root ('releases\' + $state.release)
Stop-Workbench $release
Invoke-Native $nssm @('remove', $ServiceName, 'confirm')
if (Get-NetFirewallRule -Name $FirewallName -ErrorAction SilentlyContinue) { Remove-NetFirewallRule -Name $FirewallName }
foreach ($child in @('releases', 'tools')) {
    $path = Join-Path $Root $child
    if ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "应用目录是联接点，请由 IT 手工核对：$path" }
    if (Get-ChildItem -LiteralPath $path -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } | Select-Object -First 1) {
        throw "应用目录内含联接点，请由 IT 手工核对：$path"
    }
    Remove-Item -LiteralPath $path -Recurse -Force
}
Remove-Item -LiteralPath (Join-Path $Root 'installation.json')
Write-Host "本机配置、数据库、密钥、日志和备份保留在 $Root。"
