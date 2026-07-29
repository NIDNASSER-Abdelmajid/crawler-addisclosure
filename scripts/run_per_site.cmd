@echo off
setlocal enabledelayedexpansion

:: Usage:
::   run_per_site.cmd [csv-file] [output-dir] [max-runs] [tracker-csv]
:: Example:
::   run_per_site.cmd resources\adLikelyUrls.csv results 50 results\crawled_sites.csv

set "CSV_FILE=%~1"
if "%CSV_FILE%"=="" set "CSV_FILE=resources\adLikelyUrls.csv"

set "OUTPUT_DIR=%~2"
if "%OUTPUT_DIR%"=="" set "OUTPUT_DIR=results"

set "MAX_RUNS=%~3"
if "%MAX_RUNS%"=="" set "MAX_RUNS=0"

set "TRACK_CSV=%~4"
if "%TRACK_CSV%"=="" set "TRACK_CSV=%OUTPUT_DIR%\crawled_sites.csv"

if not exist "%CSV_FILE%" (
  echo CSV file not found: %CSV_FILE%
  exit /b 1
)

if not exist "%OUTPUT_DIR%" (
  mkdir "%OUTPUT_DIR%"
)

:: Delegate execution safely to the Python script
python script.py "%CSV_FILE%" "%OUTPUT_DIR%" "%MAX_RUNS%" "%TRACK_CSV%"

