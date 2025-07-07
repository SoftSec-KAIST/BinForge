#!/bin/bash

ps aux | grep -E "(bash|python|py|make|lto1|gcc|clang|_gcc|_clang)" | tr -s ' ' | cut -d ' ' -f 2 | xargs kill -9
