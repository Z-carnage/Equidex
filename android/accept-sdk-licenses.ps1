$ErrorActionPreference = 'Stop'
$toolsRoot = Join-Path (Split-Path $PSScriptRoot -Parent) '.android-tools'
$env:JAVA_HOME = (Get-ChildItem (Join-Path $toolsRoot 'jdk') -Directory | Select-Object -First 1).FullName
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"
& (Join-Path $toolsRoot 'sdk\cmdline-tools\bin\sdkmanager.bat') "--sdk_root=$(Join-Path $toolsRoot 'sdk')" --licenses
