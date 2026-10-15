@echo off
rem Abre o AutoCortes: prepara o Python na primeira vez e liga o painel no navegador.
rem Feche esta janela (ou use "Fechar o AutoCortes" no painel) para desligar.
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title AutoCortes

set "PY=.venv\Scripts\python.exe"
rem cópia do requirements.txt da última instalação: quando ele muda, as bibliotecas são instaladas de novo
set "MARCA=.venv\autocortes-requisitos.txt"
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

rem a .venv pode ter vindo de um Python antigo, ou ter ficado sem o Python que a criou
"%PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 (
  echo.
  echo A pasta .venv foi criada com um Python antigo, ou o Python dela foi desinstalado.
  echo O AutoCortes precisa do Python 3.11 ou mais novo: instale pelo site
  echo https://www.python.org/downloads/, apague a pasta .venv que fica junto deste arquivo
  echo e abra o AutoCortes.bat de novo. Ela é refeita sozinha, e nada do AutoCortes se perde.
  echo.
  pause
  exit /b 1
)

set "INSTALAR="
fc /b requirements.txt "%MARCA%" >nul 2>nul || set "INSTALAR=1"
"%PY%" -c "import requests, tomlkit" >nul 2>nul || set "INSTALAR=1"
if defined INSTALAR (
  echo Instalando as bibliotecas do AutoCortes...
  "%PY%" -m pip --version >nul 2>nul || "%PY%" -m ensurepip --upgrade >nul 2>nul
  "%PY%" -m pip install --disable-pip-version-check --quiet -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Não consegui instalar as bibliotecas. Confira a internet e tente de novo.
    echo Se continuar, apague a pasta .venv que fica junto deste arquivo e abra o AutoCortes.bat de novo.
    echo.
    pause
    exit /b 1
  )
  copy /y requirements.txt "%MARCA%" >nul
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo Aviso: o FFmpeg não está no PATH do Windows. Instale com: winget install Gyan.FFmpeg
  echo ou informe o caminho dele no painel, em Configurações ^> Sistema.
  echo.
)

rem O cmd lê este arquivo aos pedaços, e a atualização pelo GitHub pode trocá-lo com o painel aberto:
rem por isso o fim fica numa linha só, que o cmd lê inteira antes de o painel abrir.
"%PY%" -m autocortes painel %* || (echo. & pause & endlocal & exit /b 1) & endlocal & exit /b 0
