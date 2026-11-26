#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Version = 'v1.1.7-rc.1',
    [string]$OutputDirectory = (Join-Path (Split-Path $PSScriptRoot -Parent) 'dist')
)
$ErrorActionPreference = 'Stop'
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.12 (Join-Path $PSScriptRoot 'build_release.py') --version $Version --out $OutputDirectory
} else {
    & python (Join-Path $PSScriptRoot 'build_release.py') --version $Version --out $OutputDirectory
}
if ($LASTEXITCODE -ne 0) { throw '发布构建失败。' }
