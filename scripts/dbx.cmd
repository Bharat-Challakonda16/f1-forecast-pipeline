@echo off
rem Runs the project-local Databricks CLI against the project-local config.
rem It never reads or writes %USERPROFILE%\.databrickscfg, so work profiles stay untouched.
rem Bundle commands run from the databricks\ folder automatically.
setlocal
set "ROOT=%~dp0.."
set "DATABRICKS_CONFIG_FILE=%ROOT%\.databrickscfg"
set "DATABRICKS_CONFIG_PROFILE=f1forecast"
set "CLI=%ROOT%\.tools\databricks.exe"

if not exist "%CLI%" (
  echo Databricks CLI not found at %CLI%. See README "Local setup".
  exit /b 1
)
if not exist "%DATABRICKS_CONFIG_FILE%" (
  echo Missing %DATABRICKS_CONFIG_FILE%. See README "Local setup".
  exit /b 1
)

if /i not "%~1"=="bundle" goto run
pushd "%ROOT%\databricks"
:run
"%CLI%" %*
exit /b %ERRORLEVEL%
