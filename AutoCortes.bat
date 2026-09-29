@echo off
rem Abre o AutoCortes: prepara o Python na primeira vez e liga o painel no navegador.
rem Feche esta janela (ou use "Fechar o AutoCortes" no painel) para desligar.
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title AutoCortes

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Preparando o AutoCortes pela primeira vez. Isso leva um minuto...
  py -3 -m venv .venv >nul 2>nul
  if not exist "%PY%" python -m venv .venv >nul 2>nul
)
if not exist "%PY%" (
  echo.
  echo Não encontrei o Python. Instale o Python 3.11 ou mais novo pelo site
  echo https://www.python.org/downloads/ marcando "Add python.exe to PATH"
  echo e abra o AutoCortes.bat de novo.
  echo.
  pause
  exit /b 1
)

"%PY%" -c "import requests, tomlkit" >nul 2>nul
if errorlevel 1 (
  echo Instalando as bibliotecas do AutoCortes...
  "%PY%" -m pip install --disable-pip-version-check --quiet -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Não consegui instalar as bibliotecas. Confira a internet e tente de novo.
    pause
    exit /b 1
  )
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo Aviso: o FFmpeg não está no PATH do Windows. Instale com: winget install Gyan.FFmpeg
  echo ou informe o caminho dele no painel, em Configurações ^> Sistema.
  echo.
)

"%PY%" -m autocortes painel %*
if errorlevel 1 (
  echo.
  pause
)
endlocal
