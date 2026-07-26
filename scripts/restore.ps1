param(
    [Parameter(Mandatory=$true)][string]$DatabaseBackup,
    [string]$SourceArchive
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $DatabaseBackup)) { throw "Database backup not found: $DatabaseBackup" }

$confirmation = Read-Host "This replaces the current database. Type RESTORE to continue"
if ($confirmation -ne "RESTORE") { throw "Restore cancelled." }

Write-Host "Restoring PostgreSQL from $DatabaseBackup"
Get-Content -Raw -LiteralPath $DatabaseBackup | docker compose exec -T db psql -U postgres -d qnsc_kb

if ($SourceArchive) {
    if (-not (Test-Path -LiteralPath $SourceArchive)) { throw "Source archive not found: $SourceArchive" }
    $volume = (docker volume ls --format '{{.Name}}' | Where-Object { $_ -like '*_source_data' } | Select-Object -First 1)
    if (-not $volume) { throw "Source storage volume was not found." }
    docker run --rm -v "${volume}:/source" -v "$(Resolve-Path $SourceArchive):/backup/archive.tar.gz:ro" alpine:3.20 sh -c "rm -rf /source/* && tar -xzf /backup/archive.tar.gz -C /source"
}

Write-Host "Restore completed."
