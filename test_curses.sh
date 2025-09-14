#!/bin/bash
# Test the curses UI with a simple bash session

echo "Testing curses UI with bash..."
echo "Commands to try:"
echo "  - Type 'ls' to list files"
echo "  - Use arrow keys to scroll"
echo "  - Press 'q' to quit"
echo ""
echo "Starting in 2 seconds..."
sleep 2

poetry run python listen-cli-curses.py /bin/bash