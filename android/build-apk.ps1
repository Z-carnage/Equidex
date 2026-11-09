$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$toolsRoot = Join-Path $projectRoot '.android-tools'
$jdkDir = Get-ChildItem (Join-Path $toolsRoot 'jdk') -Directory | Select-Object -First 1
$env:JAVA_HOME = $jdkDir.FullName
$env:GRADLE_USER_HOME = Join-Path $toolsRoot 'gradle-cache'
$env:ANDROID_HOME = Join-Path $toolsRoot 'sdk'
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"
$sdkManager = Join-Path $env:ANDROID_HOME 'cmdline-tools\bin\sdkmanager.bat'
& $sdkManager "--sdk_root=$env:ANDROID_HOME" 'platforms;android-35' 'build-tools;35.0.0'
if ($LASTEXITCODE -ne 0) { throw 'Android SDK installation failed. Review and accept the SDK licences yourself.' }
$sourceDir = Join-Path $PSScriptRoot 'app\src\main\python'
foreach ($name in @('executive-oge-index.json','executive_oge.py','govcon-recipients.json','serve.py','order_book.py','rpo.py','equidex-mobile.html','screener-fundamentals.js','equidex-inconsolata-dark.png','equidex-inconsolata-light.png')) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $name) -Destination $sourceDir -Force
}
Push-Location $PSScriptRoot
try {
    & (Join-Path $toolsRoot 'gradle\gradle-8.9\bin\gradle.bat') assembleDebug
    if ($LASTEXITCODE -ne 0) { throw 'APK build failed' }
    Copy-Item -LiteralPath 'app\build\outputs\apk\debug\app-debug.apk' -Destination (Join-Path $projectRoot 'dist\EQUIDEX.apk') -Force
} finally { Pop-Location }
