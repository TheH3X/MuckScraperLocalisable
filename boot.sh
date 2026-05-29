#!/bin/sh
set -e

exec gunicorn -b 0.0.0.0:5000 aggregator.app:app
