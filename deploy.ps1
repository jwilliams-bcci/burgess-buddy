Set-Location "S:\burgess-buddy"

Write-Host "Pulling latest changes from GitHub..." -ForegroundColor Cyan
git pull origin main

if (Test-Path "requirements.txt") {
    Write-Host "Syncing Python dependencies..." -ForegroundColor Cyan
    & "S:\burgess-buddy\.venv\Scripts\pip.exe" install -r requirements.txt --quiet
}

Write-Host "Ensuring IIS AppPool permissions on new files..." -ForegroundColor Cyan
icacls "S:\burgess-buddy" /grant "IIS AppPool\PortalLive2Backend:(OI)(CI)M" /T /Q

Write-Host "Deployment complete." -ForegroundColor Green