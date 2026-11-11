#!/bin/sh
set -eu
python -m compileall -q corpus_gate demo_support
python -m unittest discover -s tests -v
python -m corpus_gate --db :memory: demo --fixture fixtures/demo.json
python demo_support/check.py
