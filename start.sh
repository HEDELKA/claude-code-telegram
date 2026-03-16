#!/bin/bash
cd "$(dirname "$0")"
env -u ALL_PROXY -u all_proxy -u CLAUDECODE .venv/bin/python -m src.main
