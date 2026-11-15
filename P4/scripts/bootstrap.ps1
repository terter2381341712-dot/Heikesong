$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '../..')
# Keep prepared environments outside the checkout so they survive branch
# switches, new worktrees and moving the project directory.
if ($env:P4_RUNTIME_DIR) {
    $root = $env:P4_RUNTIME_DIR
} else {
    $root = Join-Path $env:LOCALAPPDATA 'EdgeFabric/runtime'
}
$lock = $null
$exitCode = 1
try {
    if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') {
        throw 'This release requires Windows x64.'
    }
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    $lock = [IO.File]::Open((Join-Path $root '.bootstrap.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $item = Import-Csv 'P4/runtime/installers.tsv' -Delimiter "`t" | Where-Object { $_.platform -eq 'win-64' }
    $manager = Join-Path $root 'micromamba.exe'
    if (-not (Test-Path $manager) -or (Get-FileHash $manager -Algorithm SHA256).Hash.ToLower() -ne $item.sha256) {
        Write-Host 'Downloading and verifying the application environment manager...'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $download = Join-Path $root 'micromamba.download'
        Invoke-WebRequest -UseBasicParsing -Uri $item.url -OutFile $download -TimeoutSec 600
        if ((Get-FileHash $download -Algorithm SHA256).Hash.ToLower() -ne $item.sha256) { throw 'Download checksum mismatch. Please retry.' }
        Move-Item -Force $download $manager
    }
    $prefix = Join-Path $root 'bootstrap'
    $python = Join-Path $prefix 'python.exe'
    $env:PATH = "$prefix;$prefix\Library\bin;$prefix\Scripts;" + $env:PATH
    $ready = $false
    if (Test-Path $python) {
        & $python -I -c "import os,sys; h=os.add_dll_directory(os.path.join(sys.prefix,'Library','bin')); import ssl; assert sys.version_info[:3]==(3,12,15)"
        $ready = $LASTEXITCODE -eq 0
    }
    if (-not $ready) {
        Write-Host 'Preparing the private application runtime...'
        $action = 'create'
        $repair = @()
        if (Test-Path (Join-Path $prefix 'conda-meta')) { $action = 'install'; $repair = @('--force-reinstall') }
        & $manager --no-rc -r (Join-Path $root 'cache') $action @repair -y -p $prefix --override-channels -c conda-forge python=3.12.15 pip
        if ($LASTEXITCODE -ne 0) { throw 'Runtime download or preparation failed. Check the messages above and retry.' }
    }
    $site = Join-Path $prefix 'Lib/site-packages'
    New-Item -ItemType Directory -Force -Path $site | Out-Null
    Set-Content -Encoding ASCII -Path (Join-Path $site '_edge_private_runtime.pth') -Value "import os,sys; os._edge_dll_handles = [os.add_dll_directory(os.path.join(sys.prefix,'Library','bin'))]"
    $lock.Dispose()
    $lock = $null
    & $python -I P4/scripts/managed_runtime.py @args
    $exitCode = $LASTEXITCODE
} catch {
    Write-Host ('Startup failed: ' + $_.Exception.Message) -ForegroundColor Red
} finally {
    if ($null -ne $lock) { $lock.Dispose() }
}
exit $exitCode
