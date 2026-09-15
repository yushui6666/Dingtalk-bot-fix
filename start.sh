#!/bin/bash
# 快捷：等同 bash manage.sh start
exec bash "$(dirname "$0")/manage.sh" start "$@"
