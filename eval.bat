@echo off
rem Evaluation dashboard (2.1, dev tool): golden-set breakdown dashboard.
rem NOTE: keep this file pure ASCII (see regress.bat for the reason).
cd /d "%~dp0"
python -m streamlit run app/eval_dashboard.py
