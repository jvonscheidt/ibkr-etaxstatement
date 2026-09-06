param(
    [Parameter(Mandatory)]
    [string]$Destination
)

$ErrorActionPreference = 'Stop'
$version = '1.29.290'
$expectedHash = '6824B6E9484AB24687D99A0C829D2BCBCC7849A70A4D0F596FC43C89D20DFF15'
New-Item -ItemType Directory -Path $Destination | Out-Null
$bundlePath = Join-Path $Destination 'winget.msixbundle'
$packagePath = Join-Path $Destination 'AppInstaller_x64.msix'
$cliDirectory = Join-Path $Destination 'cli'

Invoke-WebRequest "https://github.com/microsoft/winget-cli/releases/download/v$version/Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle" -OutFile $bundlePath
if ((Get-FileHash $bundlePath -Algorithm SHA256).Hash -ne $expectedHash) {
    throw 'Official WinGet bundle checksum mismatch'
}

# Use the native validator without registering AppX packages on the hosted image.
$bundle = [IO.Compression.ZipFile]::OpenRead($bundlePath)
try {
    $package = $bundle.GetEntry('AppInstaller_x64.msix')
    if ($null -eq $package) { throw 'Official WinGet bundle has no x64 package' }
    [IO.Compression.ZipFileExtensions]::ExtractToFile($package, $packagePath)
} finally {
    $bundle.Dispose()
}
[IO.Compression.ZipFile]::ExtractToDirectory($packagePath, $cliDirectory)
$winget = Join-Path $cliDirectory 'winget.exe'
$actualVersion = & $winget --version
if ($LASTEXITCODE -ne 0 -or $actualVersion -ne "v$version") {
    throw "WinGet version mismatch: $actualVersion"
}
Write-Output $winget
