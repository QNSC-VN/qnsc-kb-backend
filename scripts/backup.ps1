param(
    [string]$OutputDirectory = "./backups"
)

$ErrorActionPreference = "Stop"
$resolvedOutput = (New-Item -ItemType Directory -Force -Path $OutputDirectory).FullName
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dbFile = Join-Path $resolvedOutput "qnsc_kb-$stamp.sql"
$sourceArchive = Join-Path $resolvedOutput "sources-$stamp.tar.gz"

Write-Host "Backing up PostgreSQL to $dbFile"
docker compose exec -T db pg_dump -U postgres -d qnsc_kb --format=plain --no-owner --no-privileges | Out-File -FilePath $dbFile -Encoding utf8

$volume = (docker volume ls --format '{{.Name}}' | Where-Object { $_ -like '*_source_data' } | Select-Object -First 1)
if (-not $volume) {
    Write-Warning "Source storage volume was not found; database backup completed without file storage."
    exit 0
}

Write-Host "Backing up source storage volume $volume to $sourceArchive"
docker run --rm -v "${volume}:/source:ro" -v "${resolvedOutput}:/backup" alpine:3.20 tar -czf "/backup/$(Split-Path -Leaf $sourceArchive)" -C /source .
Write-Host "Backup completed."
