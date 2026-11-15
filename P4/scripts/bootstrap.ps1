$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '../..')
$root = [IO.Path]::GetFullPath((Join-Path (Get-Location) 'P4/var/runtime'))
$lock = $null
$exitCode = 1
try {
    if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') {
        throw 'This release requires Windows x64.'
    }
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    $lock = [IO.File]::Open((Join-Path $root '.bootstrap.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $prefix = Join-Path $root 'bootstrap'
    $python = Join-Path $prefix 'python.exe'
    $env:PATH = "$prefix;$prefix\Library\bin;$prefix\Scripts;" + $env:PATH
    $ready = $false
    if (Test-Path $python) {
        & $python -I -c "import sys,ssl; assert sys.version_info[:3]==(3,9,6)"
        $ready = $LASTEXITCODE -eq 0
    }
    if (-not $ready) {
        Write-Host 'Preparing verified portable Python 3.9.6 (no Conda solver)...'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $items = Import-Csv 'P4/runtime/portable-windows.tsv' -Delimiter "`t"
        $downloads = Join-Path $root 'downloads'
        New-Item -ItemType Directory -Force -Path $downloads | Out-Null
        $archives = @{}
        foreach ($component in @('python')) {
            $item = $items | Where-Object { $_.component -eq $component }
            $target = Join-Path $downloads ([IO.Path]::GetFileName(([Uri]$item.url).AbsolutePath))
            if (-not (Test-Path -LiteralPath $target) -or (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLower() -ne $item.sha256) {
                Write-Host "Downloading and verifying $component..."
                $temporary = $target + '.' + [Guid]::NewGuid().ToString('N') + '.download'
                Invoke-WebRequest -UseBasicParsing -Uri $item.url -OutFile $temporary -TimeoutSec 600
                if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLower() -ne $item.sha256) { throw 'Download checksum mismatch.' }
                Move-Item -LiteralPath $temporary -Destination $target -Force
            }
            $archives[$component] = $target
        }
        $stage = Join-Path $root ('bootstrap.prepare-' + [Guid]::NewGuid().ToString('N'))
        [IO.Compression.ZipFile]::ExtractToDirectory($archives['python'], $stage)
        $site = Join-Path $stage 'Lib/site-packages'
        New-Item -ItemType Directory -Force -Path $site | Out-Null
        New-Item -ItemType Directory -Force -Path (Join-Path $stage 'Library/bin') | Out-Null
        Set-Content -Encoding ASCII -LiteralPath (Join-Path $stage 'python39._pth') -Value "python39.zip`n.`nLib/site-packages`nimport site"
        & (Join-Path $stage 'python.exe') -I -c "import sys,ssl; assert sys.version_info[:3]==(3,9,6)"
        if ($LASTEXITCODE -ne 0) { throw 'Portable Python validation failed.' }
        if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($prefix)) -ne $root -or [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($stage)) -ne $root) { throw 'Invalid runtime destination.' }
        $backup = Join-Path $root ('bootstrap.backup-' + [Guid]::NewGuid().ToString('N'))
        if (Test-Path -LiteralPath $prefix) { Move-Item -LiteralPath $prefix -Destination $backup }
        try { Move-Item -LiteralPath $stage -Destination $prefix }
        catch {
            if ((Test-Path -LiteralPath $backup) -and -not (Test-Path -LiteralPath $prefix)) { Move-Item -LiteralPath $backup -Destination $prefix }
            throw
        }
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
